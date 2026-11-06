import hashlib
import inspect
import json
import re
import shutil
import subprocess
import sys

import pytest
import zarr
from mia_harness.provenance import HARNESS_URI
from rangeserver import server  # noqa: F401  (fixture)
from test_smoke import SNAP, write
from test_provenance import SCHEMA_1, convention_errors
from test_stage import SHAPE, VOXEL, io, plan_input, served  # noqa: F401  (io is a fixture)

import mia_harness.check
from mia_harness import cli
from mia_harness.check import check_stage
from mia_harness.schemas import ngff
from mia_harness.stage import stage


@pytest.fixture
def staged_out(tmp_path, io, server):  # noqa: F811
    """A staged artifact as test_stage_writes_sparse_0_6_volume_with_provenance makes it: one raw, an instance label."""
    b, r = write(tmp_path, SHAPE, SHAPE)
    served(server, b)
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 4, "val": 1, "test": 1}), voxel=VOXEL,
                out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    return tmp_path / "out"


def test_check_stage_passes_a_good_artifact_and_records_verification(staged_out):
    rep = check_stage(staged_out, harness_commit="h" * 40)
    assert rep["ok"] and rep["checks"]["schemas"]["errors"] == [] and rep["checks"]["coverage"]["missing"] == []
    assert rep["checks"]["strict"]["label"]  # instance label fails strict_label (no colors): advisory, recorded
    assert rep["checks"]["coverage"]["samples"] == 12 and rep["checks"]["coverage"]["levels"] == 6
    attrs = zarr.open_group(staged_out / "v0.zarr" / "raw", mode="r").attrs.asdict()
    assert convention_errors(attrs) == []
    stage_, check = attrs["provenance:steps"]          # the staging step stays; the check step follows it
    assert stage_["name"] == "Sparse OME-Zarr staging" and check["name"] == "check-stage"
    assert "status" not in check and check["parameters"] == {"schemas_commit": ngff.SCHEMAS_COMMIT}
    assert check["software"]["version"] == "h" * 40 and check["started"] <= check["ended"]
    mia = attrs["mia"]
    assert mia["checks"] == rep["checks"] and mia["checks"]["geometry"]["samples"]
    for group in ("v0.zarr", "v0.zarr/labels/gt"):
        other = zarr.open_group(staged_out / group, mode="r").attrs.asdict()
        assert other["provenance:steps"][-1] == check and other["mia"]["checks"] == mia["checks"]
    text = (staged_out / "stage.json").read_text()
    assert "\n  " not in text   # the rewrite stays compact
    sj = json.loads(text)
    raw_json = (staged_out / "v0.zarr/raw/zarr.json").read_bytes()
    assert sj["zarr_json_sha256"]["v0.zarr/raw/zarr.json"] == hashlib.sha256(raw_json).hexdigest()
    assert "v0.zarr/raw/s0/zarr.json" in sj["zarr_json_sha256"] and sj["ok"] and sj["verification"]["ok"]
    assert sj["checks"] == mia["checks"] and sj["verification"]["harness_commit"] == "h" * 40


def test_add_provenance_upgrades_schema_1_in_place_and_leaves_schema_2_alone(staged_out, tmp_path, capsys):
    """An artifact written under schema 1: `mia` carries everything, no convention; check-stage had recorded digests."""
    old = tmp_path / "old" / "v0.zarr"
    (old / "raw").mkdir(parents=True)
    raw = {"schema": 1, **SCHEMA_1}
    root = {k: v for k, v in SCHEMA_1.items() if k in ("schema", "artifact", "derived_from", "voxel_size", "verification")}
    root["plan"] = {"digest": "d" * 64}
    (old / "zarr.json").write_text(json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {"ome": {}, "mia": root}}))
    (old / "raw" / "zarr.json").write_text(json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {"ome": {}, "mia": raw}}))
    (old / "raw" / "s0").mkdir()
    (old / "raw" / "s0" / "zarr.json").write_text(json.dumps({"zarr_format": 3, "node_type": "array"}))
    (old.parent / "stage.json").write_text(json.dumps({"ok": True, "zarr_json_sha256": {"stale": "0"}}))
    assert cli.main(["add-provenance", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"{old.parent}: written 2, present 0, skipped 0" in out and f"{staged_out}: written 0, present 3, skipped 0" in out
    for group in (old, old / "raw"):
        attrs = json.loads((group / "zarr.json").read_text())["attributes"]
        assert convention_errors(attrs) == [] and attrs["mia"]["schema"] == 2 and "artifact" not in attrs["mia"]
        assert attrs["provenance:steps"][0]["parameters"]["plan"] == {"seed": 0, "patch": 64}   # the root took raw's rule
    sj = json.loads((old.parent / "stage.json").read_text())
    assert set(sj["zarr_json_sha256"]) == {"v0.zarr/zarr.json", "v0.zarr/raw/zarr.json", "v0.zarr/raw/s0/zarr.json"}
    for rel, digest in sj["zarr_json_sha256"].items():
        assert hashlib.sha256((old.parent / rel).read_bytes()).hexdigest() == digest
    assert cli.main(["add-provenance", str(old.parent)]) == 0 and "written 0, present 2" in capsys.readouterr().out
    # a schema-2 group written before the convention renamed `modules`: the key is renamed, the digest refreshed
    doc = json.loads((old / "raw" / "zarr.json").read_text())
    step = doc["attributes"]["provenance:steps"][0]
    step["inputs"] = [i for i in step["inputs"] if i.get("name") != "snippet"]
    step["runtime-dependencies"] = [{"name": "npy-local", "version": "1" * 40, "uri": HARNESS_URI}]
    step["status"] = "ok"
    (old / "raw" / "zarr.json").write_text(json.dumps(doc))
    assert cli.main(["add-provenance", str(old.parent)]) == 0 and "written 1, present 1" in capsys.readouterr().out
    attrs = json.loads((old / "raw" / "zarr.json").read_text())["attributes"]
    assert set(attrs["provenance:steps"][0]) & {"modules", "runtime-dependencies", "runtime_dependencies", "status"} == set()
    assert attrs["provenance:steps"][0]["inputs"][-1] == {"uri": f"{HARNESS_URI}/blob/{'1' * 40}/snippets/npy-local/snippet.py",
                                                         "name": "snippet"}
    sj = json.loads((old.parent / "stage.json").read_text())
    assert hashlib.sha256((old / "raw" / "zarr.json").read_bytes()).hexdigest() == sj["zarr_json_sha256"]["v0.zarr/raw/zarr.json"]


def test_check_stage_is_idempotent_and_its_digests_describe_the_files_on_disk(staged_out):
    first = check_stage(staged_out)
    second = check_stage(staged_out)
    assert first["ok"] == second["ok"] is True and second["errors"] == []
    for rel, digest in second["zarr_json_sha256"].items():
        assert hashlib.sha256((staged_out / rel).read_bytes()).hexdigest() == digest
    assert set(second["zarr_json_sha256"]) == {p.relative_to(staged_out).as_posix()
                                               for p in staged_out.rglob("zarr.json")}
    assert json.loads((staged_out / "stage.json").read_text())["errors"] == []


def test_check_stage_fails_on_a_removed_chunk_and_on_bad_metadata(staged_out):
    chunk = next(p for p in (staged_out / "v0.zarr/raw/s0/c").rglob("*") if p.is_file())
    chunk.unlink()
    rep = check_stage(staged_out)
    assert not rep["ok"] and rep["checks"]["coverage"]["missing"]
    assert rep["checks"]["coverage"]["missing"][0]["group"] == "v0.zarr/raw"
    deleted = chunk.relative_to(staged_out / "v0.zarr/raw/s0").as_posix()
    assert any(deleted in miss["keys"] for miss in rep["checks"]["coverage"]["missing"] if miss["level"] == 0)
    sj = json.loads((staged_out / "stage.json").read_text())
    assert not sj["ok"] and any(e.startswith("check-stage: ") for e in sj["errors"])
    z = staged_out / "v0.zarr/raw/zarr.json"
    d = json.loads(z.read_text())
    d["attributes"]["ome"]["version"] = "0.5"
    z.write_text(json.dumps(d))
    rep = check_stage(staged_out)
    assert not rep["ok"]
    assert any(e.startswith("v0.zarr/raw/zarr.json: image: ") for e in rep["checks"]["schemas"]["errors"])
    assert json.loads(z.read_text())["attributes"]["ome"]["version"] == "0.5"   # the rewrite keeps the document


def test_check_stage_flags_a_sample_located_off_its_planned_start(staged_out):
    z = staged_out / "v0.zarr/raw/zarr.json"
    d = json.loads(z.read_text())
    sample = d["attributes"]["mia"]["plan"]["samples"]["train"][0]
    planned = sample["starts"]["2"] = [sample["starts"]["2"][0] + 1, sample["starts"]["2"][1]]
    z.write_text(json.dumps(d))
    rep = check_stage(staged_out)
    misses = [m for m in rep["checks"]["coverage"]["missing"] if m["reason"] == "starts"]
    assert not rep["ok"] and [(m["group"], m["sample"], m["level"]) for m in misses] == [("v0.zarr/raw", 0, 1)]
    assert misses[0]["planned"] == planned and misses[0]["located"] == [planned[0] - 1, planned[1]]
    assert f"v0.zarr/raw: sample 0 level 1 located at {misses[0]['located']}, planned at {planned}" in rep["errors"]


def test_check_stage_flags_an_artifact_id_that_disagrees_with_stage_json(staged_out):
    sj = json.loads((staged_out / "stage.json").read_text())
    sj["artifact"]["id"] = "b" * 16
    (staged_out / "stage.json").write_text(json.dumps(sj))
    rep = check_stage(staged_out)
    assert not rep["ok"] and f"v0.zarr/raw: artifact id {'a' * 16} != stage.json {'b' * 16}" in rep["errors"]


def test_check_stage_reports_a_missing_stage_json_without_writing(staged_out):
    (staged_out / "stage.json").unlink()
    rep = check_stage(staged_out)
    assert not rep["ok"] and rep["errors"][0].startswith("stage.json: cannot read: FileNotFoundError")
    assert not (staged_out / "stage.json").exists()
    steps = zarr.open_group(staged_out / "v0.zarr/raw", mode="r").attrs["provenance:steps"]
    assert [s["name"] for s in steps] == ["Sparse OME-Zarr staging"]        # untouched


def test_check_stage_reports_a_partly_written_volume(staged_out):
    (staged_out / "v0.zarr/raw/zarr.json").unlink()
    rep = check_stage(staged_out)
    assert not rep["ok"] and any(e.startswith("v0.zarr/raw/zarr.json: cannot read: FileNotFoundError")
                                 for e in rep["errors"])
    sj = json.loads((staged_out / "stage.json").read_text())
    assert not sj["ok"] and "v0.zarr/raw/zarr.json" not in sj["zarr_json_sha256"]
    steps = zarr.open_group(staged_out / "v0.zarr/labels/gt", mode="r").attrs["provenance:steps"]
    assert [s["name"] for s in steps] == ["Sparse OME-Zarr staging"]      # a failed check leaves no step


def test_check_stage_says_why_when_the_stage_failed_or_left_no_volumes(staged_out):
    sj = json.loads((staged_out / "stage.json").read_text())
    sj["errors"], sj["ok"] = ["raw 0: boom"], False
    (staged_out / "stage.json").write_text(json.dumps(sj))
    assert "stage.json reports 1 errors" in check_stage(staged_out)["errors"]
    sj = json.loads((staged_out / "stage.json").read_text())
    assert sj["errors"][0] == "raw 0: boom" and "check-stage: stage.json reports 1 errors" in sj["errors"]
    shutil.rmtree(staged_out / "v0.zarr")
    assert "no volumes" in check_stage(staged_out)["errors"]


def test_check_stage_cli(staged_out, monkeypatch, capsys):
    monkeypatch.setenv("MIA_HARNESS_COMMIT", "c" * 40)
    assert cli.main(["check-stage", str(staged_out)]) == 0
    assert json.loads(capsys.readouterr().out)["harness_commit"] == "c" * 40
    (staged_out / "v0.zarr/raw/s0/zarr.json").unlink()
    assert cli.main(["check-stage", str(staged_out)]) == 1


def test_check_stage_imports_no_snippet_code():
    source = inspect.getsource(mia_harness.check)
    assert not re.search(r"from \.(loader|run|io|fetch|stage) import|import mia_harness\.(loader|run|io|fetch|stage)",
                         source)


def test_check_stage_loads_no_snippet_module_transitively():
    loaded = subprocess.run([sys.executable, "-c", "import sys, mia_harness.check; print(*sys.modules)"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert not {f"mia_harness.{m}" for m in ("loader", "run", "io", "fetch", "stage")} & set(loaded)
