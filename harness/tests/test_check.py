import hashlib
import inspect
import json
import re
import shutil
import subprocess
import sys

import pytest
import zarr
from rangeserver import server  # noqa: F401  (fixture)
from test_smoke import SNAP, write
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
    mia = zarr.open_group(staged_out / "v0.zarr" / "raw", mode="r").attrs["mia"]
    assert mia["verification"]["ok"] and mia["verification"]["schemas_commit"] == ngff.SCHEMAS_COMMIT
    assert mia["verification"]["harness_commit"] == "h" * 40 and mia["verification"]["checks"]["geometry"]["samples"]
    for group in ("v0.zarr", "v0.zarr/labels/gt"):
        assert zarr.open_group(staged_out / group, mode="r").attrs["mia"]["verification"] == mia["verification"]
    sj = json.loads((staged_out / "stage.json").read_text())
    raw_json = (staged_out / "v0.zarr/raw/zarr.json").read_bytes()
    assert sj["zarr_json_sha256"]["v0.zarr/raw/zarr.json"] == hashlib.sha256(raw_json).hexdigest()
    assert "v0.zarr/raw/s0/zarr.json" in sj["zarr_json_sha256"] and sj["ok"] and sj["verification"]["ok"]
    assert sj["verification"] == mia["verification"]


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
    assert zarr.open_group(staged_out / "v0.zarr/raw", mode="r").attrs["mia"]["verification"]["ok"] is None  # untouched


def test_check_stage_reports_a_partly_written_volume(staged_out):
    (staged_out / "v0.zarr/raw/zarr.json").unlink()
    rep = check_stage(staged_out)
    assert not rep["ok"] and any(e.startswith("v0.zarr/raw/zarr.json: cannot read: FileNotFoundError")
                                 for e in rep["errors"])
    sj = json.loads((staged_out / "stage.json").read_text())
    assert not sj["ok"] and "v0.zarr/raw/zarr.json" not in sj["zarr_json_sha256"]
    assert zarr.open_group(staged_out / "v0.zarr/labels/gt", mode="r").attrs["mia"]["verification"]["ok"] is False


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
