import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest
import yaml
import zarr
from test_io import make_io  # noqa: F401  (fixture)
from test_smoke import SNAP, write
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness import cli
from mia_harness.binding import binding_hash
from mia_harness.cache import DatasetCache
from mia_harness.io import IO
from mia_harness.plan import LevelPlan, PlanInput, PlanRule, derive_samples, planned_bytes, stage_plan
from mia_harness.provenance import VoxelAxis
from mia_harness.stage import stage

VOXEL = {"z": VoxelAxis(300.0, "record"), "y": VoxelAxis(8.0, "assumed", "test"), "x": VoxelAxis(8.0, "assumed", "test")}
SPLIT = {"policy": "fractions", "fractions": {"train": 0.6, "val": 0.2, "test": 0.2}, "seed": 0}


def plan_input(levels, patch, samples) -> PlanInput:
    """The plan-rule.json the worker writes, for these levels, patch and per-split counts."""
    return PlanInput(rule=PlanRule(seed=0, patch=patch, levels=tuple(levels), samples=samples, split=SPLIT,
                                   channel_recipe=None), artifact_id="a" * 16, levels_key="p64-l1-2-4")


def write_plan(path: Path, plan: PlanInput, **extra) -> Path:
    path.write_text(json.dumps({"artifact_id": plan.artifact_id, "levels_key": plan.levels_key,
                                "rule": dataclasses.asdict(plan.rule), **extra}))
    return path


@pytest.fixture
def io(tmp_path):
    i = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30, {"127.0.0.1"})
    yield i
    i.close()


def served(server, binding: Path) -> int:
    """Serve every array of the binding over HTTP and point the binding at it, as production bindings are (URLs
    are what derived_from records). Returns the bytes served."""
    d = yaml.safe_load(binding.read_text())
    total = 0
    for role in ("raw", "labels"):
        for n, entry in enumerate(d[role]):
            body = Path(entry["params"].pop("path")).read_bytes()
            server.files[f"/{role}{n}.npy"] = body
            entry["urls"] = [server.url(f"/{role}{n}.npy")]
            total += len(body)
    binding.write_text(yaml.safe_dump(d))
    return total


# five planes, so every split of 0.6/0.2/0.2 gets one (three planes leave test none)
SHAPE = (5, 600, 700)


def test_stage_writes_sparse_0_6_volume_with_provenance(tmp_path, io, server, monkeypatch):
    monkeypatch.setenv("MIA_HARNESS_COMMIT", "c" * 40)    # plan-rule.json names none: the environment's is recorded
    b, r = write(tmp_path, SHAPE, SHAPE)
    source_bytes = served(server, b)
    raw_bytes = len(server.files["/raw0.npy"])
    plan = plan_input((1, 2, 4), 64, {"train": 4, "val": 1, "test": 1})
    rep = stage(b, SNAP, r, io, plan=plan, voxel=VOXEL, out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    assert rep["bytes_fetched"] == source_bytes   # each source fetched once, then read from the cache
    mia = zarr.open_group(tmp_path / "out" / "v0.zarr" / "raw", mode="r").attrs["mia"]
    assert mia["schema"] == 1 and mia["artifact"]["id"] == "a" * 16 and mia["artifact"]["plan_digest"] == plan.rule.digest()
    assert mia["artifact"]["binding_hash"] == binding_hash(b) and mia["artifact"]["record_commit"] == "0" * 40
    assert mia["derived_from"][0]["snippet"] == {"name": "npy-local", "commit": "1" * 40}
    assert mia["derived_from"][0]["access"] == "chunk" and mia["derived_from"][0]["validators"]["size"] == raw_bytes
    assert mia["voxel_size"]["y"] == {"nm": 8.0, "source": "assumed", "basis": "test", "range": None}
    assert sum(len(v) for v in mia["plan"]["samples"].values()) == 6 and mia["geometry"]["raw_shape"] == list(SHAPE)
    assert mia["verification"]["checks"]["geometry"]["mismatches"] == []
    assert mia["verification"]["checks"]["geometry"]["samples"] == 6 and len(mia["statistics"]["intensity_sample"]) == 1
    lab = zarr.open_group(tmp_path / "out" / "v0.zarr" / "labels" / "gt", mode="r").attrs["mia"]
    assert lab["kind"] == "label" and lab["geometry"]["levels"] == mia["geometry"]["levels"]   # same boxes, same chunks
    assert lab["label_key"] == "gt" and lab["statistics"] is None
    ms = zarr.open_group(tmp_path / "out" / "v0.zarr" / "raw", mode="r").attrs["ome"]["multiscales"][0]
    assert ms["metadata"] == {"method": "mean", "factors": [1, 2, 4], "module": "mia_harness.pyramid",
                              "commit": "c" * 40, "edge": "clip", "nearest_offset": "(L-1)//2"}
    assert mia["artifact"]["harness_commit"] == "c" * 40
    assert ms["coordinateSystems"][0]["name"] == "r.v0"
    text = (tmp_path / "out" / "stage.json").read_text()
    assert "\n  " not in text   # compact: the consumer reads stage.json within a size limit
    sj = json.loads(text)
    assert sj["artifact"]["id"] == "a" * 16 and sj["volumes"][0]["samples"].keys() == {"train", "val", "test"}
    assert sj["volumes"][0]["voxel_size_nm"] == {"z": 300.0, "y": 8.0, "x": 8.0} and sj["volumes"][0]["shape"] == list(SHAPE)
    assert sj["volumes"][0]["labels"] == {"gt": "labels/gt"} and sj["zarr_json_sha256"] == {}
    assert sj["checks"]["geometry"]["mismatches"] == [] and sj["checks"]["geometry"]["samples"] == 12
    level = sj["volumes"][0]["levels"][2]
    assert level["factor"] == 4 and level["origin"] == mia["geometry"]["levels"][2]["origin"] and "extent" in level
    root = zarr.open_group(tmp_path / "out" / "v0.zarr", mode="r").attrs
    assert root["mia"]["plan"] == {"digest": plan.rule.digest()} and root["ome"]["labels"] == ["gt"]


def test_stage_refuses_a_plan_over_the_byte_cap(tmp_path, io, server):
    b, r = write(tmp_path, (3, 600, 700))
    source_bytes = served(server, b)
    plan = plan_input((1, 2, 4), 64, {"train": 50})
    samples = derive_samples(plan.rule, "r", 0, "zyx", (3, 600, 700), {a: v.nm for a, v in VOXEL.items()})
    budget = planned_bytes(stage_plan(samples, "zyx", (3, 600, 700), (1, 2, 4), 64), 2, 1)
    rep = stage(b, SNAP, r, io, plan=plan, voxel=VOXEL, out=tmp_path / "out", stage_bytes=1000)
    assert not rep["ok"] and rep["errors"][0] == f"plan exceeds stage cap: {budget} > 1000 bytes"
    assert not (tmp_path / "out" / "v0.zarr" / "raw" / "s0").exists()
    assert 0 < io.bytes_fetched <= source_bytes   # the sources once (kept for a rebuild), never twice


def test_stage_byte_cap_defaults_to_the_environment(tmp_path, io, server, monkeypatch):
    b, r = write(tmp_path, (3, 600, 700))
    served(server, b)
    monkeypatch.setenv("MIA_HARNESS_STAGE_BYTES", "1000")
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 4}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["errors"][0].startswith("plan exceeds stage cap: ") and rep["errors"][0].endswith("> 1000 bytes")


def test_stage_pairs_a_channel_raw_with_a_label_without_channels(tmp_path, io, server):
    b, r = write(tmp_path, (2, 600, 700), (600, 700))
    d = yaml.safe_load(b.read_text())
    d["raw"][0]["params"]["axes"] = "cyx"
    b.write_text(yaml.safe_dump(d))
    served(server, b)
    # one plane lands in one split; asking every split keeps the test off which one it is
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2), 64, {"train": 3, "val": 3, "test": 3}), voxel=VOXEL,
                out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    raw = zarr.open_group(tmp_path / "out" / "v0.zarr" / "raw", mode="r").attrs["mia"]["geometry"]
    lab = zarr.open_group(tmp_path / "out" / "v0.zarr" / "labels" / "gt", mode="r").attrs["mia"]["geometry"]
    assert raw["axes"] == "cyx" and lab["axes"] == "yx" and len(rep["volumes"][0]["intensity_sample"]) == 2
    assert [lv["chunks_written"] for lv in raw["levels"]] == [2 * lv["chunks_written"] for lv in lab["levels"]]
    assert [lv["origin"] for lv in raw["levels"]] == [lv["origin"] for lv in lab["levels"]]


def test_stage_refuses_a_binding_without_source_urls(tmp_path, io):
    b, r = write(tmp_path, (3, 600, 700), (3, 600, 700))     # params.path only: nothing for derived_from
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2), 64, {"train": 2}), voxel=VOXEL, out=tmp_path / "out")
    assert not rep["ok"] and rep["errors"] == ["raw 0: no source URLs to record",
                                               "raw 0: label gt: no source URLs to record"]
    assert not (tmp_path / "out" / "v0.zarr").exists()


def test_stage_without_voxel_size_fails(tmp_path, io):
    b, r = write(tmp_path, (3, 600, 700))
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2), 64, {"train": 2}), voxel={}, out=tmp_path / "out")
    assert rep["errors"] == ["raw 0: voxel size unknown for axes ['z', 'y', 'x']"] and not rep["ok"]


def test_stage_refuses_a_label_of_another_shape(tmp_path, io):
    b, r = write(tmp_path, (3, 600, 700), (3, 600, 699))
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2), 64, {"train": 2}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["errors"] == ["raw 0: label gt shape [3, 600, 699] differs from raw [3, 600, 700]"]


def test_stage_with_nothing_to_sample_says_so(tmp_path, io):
    b, r = write(tmp_path, (3, 200, 200))
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 2}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["errors"] == ["raw 0: no samples planned"]


def test_stage_cli(tmp_path, monkeypatch, server, capsys):
    b, r = write(tmp_path, SHAPE)
    served(server, b)
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", str(1 << 24))   # the 4.2 MB source, fetched once
    monkeypatch.setenv("MIA_HARNESS_ALLOWED_HOSTS", "127.0.0.1")
    monkeypatch.delenv("MIA_HARNESS_STAGE_BYTES", raising=False)
    plan = write_plan(tmp_path / "plan-rule.json", plan_input((1, 2), 64, {"train": 2, "val": 1, "test": 1}),
                      harness_commit="h" * 40, env_digest="e" * 64)
    unknown = tmp_path / "unknown.json"
    unknown.write_text("{}")
    voxel = tmp_path / "voxel_size.json"
    voxel.write_text(json.dumps({"z": {"nm": 300.0, "source": "record"},
                                 "y": {"nm": 8.0, "source": "assumed", "basis": "test"},
                                 "x": {"nm": 8.0, "source": "assumed", "basis": "test"}}))
    args = ["stage", str(b), "--snippets", str(SNAP), "--record", str(r), "--work", str(tmp_path / "work"),
            "--plan", str(plan), "--out", str(tmp_path / "out")]
    assert cli.main(args + ["--voxel-size", str(unknown)]) == 1  # no voxel size anywhere
    malformed = tmp_path / "bad_voxel.json"
    malformed.write_text(json.dumps({"y": 8.0}))
    assert cli.main(args + ["--voxel-size", str(malformed)]) == 1
    assert capsys.readouterr().err.startswith(f"{malformed}: ")
    bad_plan = tmp_path / "bad-plan.json"
    bad_plan.write_text(json.dumps({"artifact_id": "a" * 16, "rule": {}}))
    assert cli.main([str(bad_plan) if arg == str(plan) else arg for arg in args] + ["--voxel-size", str(voxel)]) == 1
    assert capsys.readouterr().err.startswith(f"{bad_plan}: ")
    assert cli.main(args + ["--voxel-size", str(voxel)]) == 0
    sj = json.loads((tmp_path / "out" / "stage.json").read_text())
    assert sj["volumes"][0]["levels"][1]["factor"] == 2 and sj["artifact"]["levels_key"] == "p64-l1-2-4"
    assert sj["artifact"]["harness_commit"] == "h" * 40 and sj["artifact"]["env_digest"] == "e" * 64
    with pytest.raises(SystemExit, match="2"):
        cli.main(args + ["--voxel-size", str(voxel), "--levels", "1,2"])


def test_stage_verification_catches_corruption(tmp_path, io, server, monkeypatch):
    import mia_harness.pyramid as pyramid

    b, r = write(tmp_path, (3, 600, 700))
    served(server, b)
    monkeypatch.setattr(pyramid, "block_mean", lambda plane, f: plane if f == 1 else np.zeros((plane.shape[0] // f, plane.shape[1] // f), plane.dtype))
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2), 64, {"train": 2}), voxel=VOXEL, out=tmp_path / "out")
    assert not rep["ok"] and any("v0 s1" in e and "differs" in e for e in rep["errors"])
    mismatches = rep["checks"]["geometry"]["mismatches"]
    assert mismatches and all(m["level"] == 1 and m["max_abs_diff"] > 0 for m in mismatches)


def test_stage_verification_covers_labels(tmp_path, io, server, monkeypatch):
    import mia_harness.pyramid as pyramid

    b, r = write(tmp_path, (3, 600, 700), (3, 600, 700))
    served(server, b)
    monkeypatch.setattr(pyramid, "stride", lambda plane, f: plane if f == 1 else np.zeros((plane.shape[0] // f, plane.shape[1] // f), plane.dtype))
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2), 64, {"train": 2}), voxel=VOXEL, out=tmp_path / "out")
    assert not rep["ok"] and any("v0/gt s1" in e and "differs" in e for e in rep["errors"])


def test_voxel_size_merges_file_and_argument_per_axis():
    from mia_harness.stage import _voxel_size

    class A:
        axes, voxel_size_nm = "zcyx", {"y": 45.0718, "x": 45.0718}

    assert _voxel_size(A(), {}) == ({"y": VoxelAxis(45.0718, "file"), "x": VoxelAxis(45.0718, "file")}, ["z"])
    merged, missing = _voxel_size(A(), {"z": VoxelAxis(1.0, "synthetic"), "y": VoxelAxis(40.0, "record")})
    assert missing == [] and merged == {"z": VoxelAxis(1.0, "synthetic"), "y": VoxelAxis(45.0718, "file"),
                                        "x": VoxelAxis(45.0718, "file")}


def test_stage_records_a_sorted_intensity_sample_per_channel(tmp_path, io, server):
    b, r = write(tmp_path, (3, 600, 700))
    served(server, b)
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2), 64, {"train": 2}), voxel=VOXEL, out=tmp_path / "out")
    sample = rep["volumes"][0]["intensity_sample"]
    assert len(sample) == 1 and 1 <= len(sample[0]) <= 4096 and sample[0] == sorted(sample[0])
    assert all(isinstance(v, (int, float)) and 0 <= v < 3 * 600 * 700 for v in sample[0])
    assert json.loads((tmp_path / "out" / "stage.json").read_text())["volumes"][0]["intensity_sample"] == sample


def test_intensity_sample_is_per_channel_and_reads_only_written_chunks():
    from mia_harness.stage import intensity_sample

    written = {(0, z, y, x) for z in range(3) for y in range(2) for x in range(2)}
    level = LevelPlan(factor=1, origin={"z": 0, "y": 0, "x": 0}, extent={"z": 3, "y": 100, "x": 100},
                      shape=(1, 3, 100, 100), planned_planes=(0, 1, 2), chunks=frozenset(written))
    reads = []

    class FakePyramid:
        axes = "czyx"
        levels = [type("L", (), {"shape": (2, 3, 100, 100)})()]

        def read(self, level, region):
            reads.append(region)
            c = region["c"]
            ys, xs = region["y"], region["x"]
            return np.full((ys.stop - ys.start, xs.stop - xs.start), 10 * (c + 1), dtype=np.uint8)

    sample = intensity_sample(FakePyramid(), level, 64, np.random.default_rng(0))
    assert len(sample) == 2 and set(sample[0]) == {10} and set(sample[1]) == {20} and len(sample[0]) == 4096
    assert len(reads) == 16 and all(r["y"].stop <= 100 and r["x"].stop <= 100 for r in reads)
    assert {(r["z"], r["y"].start // 64, r["x"].start // 64) for r in reads} <= {k[1:] for k in written}


def test_prefetch_expands_member_globs_so_an_offline_stage_reads_the_cache(tmp_path, make_io, server):
    """The stage job's prefetch takes a `<zip>::<glob>` url the way `harness fetch` does: every member, offline,
    from the directory and members the fetch cached; a glob no fetch cached fails naming the zip."""
    import zipfile
    from mia_harness.binding import load_binding
    from mia_harness.fetch import fetch
    from mia_harness.stage import prefetch
    from test_fetch import write_binding
    from test_io import zip_bytes

    members = {f"train/{i}.tif": (bytes([i]) * 8, zipfile.ZIP_STORED) for i in range(3)}
    server.files["/g.zip"] = zip_bytes(members)
    glob_url = server.url("/g.zip") + "::train/*.tif"
    binding, record = write_binding(tmp_path, [glob_url], [])
    assert fetch(binding, record, make_io())["ok"]
    gets = len(server.gets())
    offline = make_io(hosts=())
    prefetch(load_binding(binding), offline)   # the stage job: no hosts, every member a cache hit
    assert offline.bytes_fetched == 0 and len(server.gets()) == gets
    other, _ = write_binding(tmp_path, [server.url("/g.zip") + "::val/*.tif"], [])
    with pytest.raises(ValueError, match="no member"):
        prefetch(load_binding(other), offline)
