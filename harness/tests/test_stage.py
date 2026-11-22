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
from mia_harness.check import check_stage
from mia_harness.cache import DatasetCache
from mia_harness.io import IO
from mia_harness.plan import LevelPlan, PlanInput, PlanRule, derive_samples, planned_bytes, stage_plan
from mia_harness.provenance import HARNESS_URI, VoxelAxis
from mia_harness.pyramid import open_pyramid
from test_provenance import convention_errors
from mia_harness import plan as plan_module
from mia_harness.png import content_ok
from mia_harness.stage import keeper, stage

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
    assert rep["bytes_fetched"] == source_bytes   # this reader reads each source through, once
    attrs = zarr.open_group(tmp_path / "out" / "v0.zarr" / "raw", mode="r").attrs.asdict()
    mia = attrs["mia"]
    assert convention_errors(attrs) == []
    step, = attrs["provenance:steps"]           # how the group was made: the convention
    assert step["software"] == {"name": "mia-harness", "version": "c" * 40, "uri": HARNESS_URI}
    assert step["outputs"] == [{"id": "a" * 16, "name": "artifact"}] and step["started"] <= step["ended"]
    assert step["parameters"]["plan"] == json.loads(json.dumps(plan.rule.document()))
    assert step["parameters"]["binding"]["record_commit"] == "0" * 40
    assert step["parameters"]["voxel_size"]["y"] == {"nm": 8.0, "source": "assumed", "basis": "test", "range": None}
    snippet = {"uri": f"{HARNESS_URI}/blob/{'1' * 40}/snippets/npy-local/snippet.py", "name": "snippet"}
    assert step["inputs"] == [{"id": "r", "name": "record"}, {"uri": server.url("/raw0.npy")}, snippet]
    assert mia["schema"] == 2 and set(mia) == {"schema", "segmentation_type", "geometry", "plan", "statistics", "checks"}
    assert mia["geometry"] == {"raw_shape": list(SHAPE), "source_axes": "zyx", "axes": "zyx"}
    assert sum(len(v) for v in mia["plan"]["samples"].values()) == 6
    assert mia["checks"]["geometry"]["mismatches"] == [] and mia["checks"]["geometry"]["samples"] == 6
    assert len(mia["statistics"]["intensity_sample"]) == 1
    lab_attrs = zarr.open_group(tmp_path / "out" / "v0.zarr" / "labels" / "gt", mode="r").attrs.asdict()
    lab = lab_attrs["mia"]
    assert lab["geometry"] == mia["geometry"] and lab["statistics"] is None and lab["segmentation_type"]
    assert lab_attrs["provenance:steps"][0]["inputs"] == [{"id": "r", "name": "record"}, {"uri": server.url("/labels0.npy")}, snippet]
    ms = zarr.open_group(tmp_path / "out" / "v0.zarr" / "raw", mode="r").attrs["ome"]["multiscales"][0]
    assert ms["metadata"] == {"method": "mean", "factors": [1, 2, 4], "module": "mia_harness.pyramid",
                              "commit": "c" * 40, "edge": "clip", "nearest_offset": "(L-1)//2"}
    assert ms["coordinateSystems"][0]["name"] == "r.v0"
    text = (tmp_path / "out" / "stage.json").read_text()
    assert "\n  " not in text   # compact: the consumer reads stage.json within a size limit
    sj = json.loads(text)
    assert sj["artifact"]["id"] == "a" * 16 and sj["volumes"][0]["samples"].keys() == {"train", "val", "test"}
    assert sj["volumes"][0]["voxel_size_nm"] == {"z": 300.0, "y": 8.0, "x": 8.0} and sj["volumes"][0]["shape"] == list(SHAPE)
    assert sj["volumes"][0]["labels"] == {"gt": "labels/gt"} and sj["zarr_json_sha256"] == {}
    assert sj["checks"]["geometry"]["mismatches"] == [] and sj["checks"]["geometry"]["samples"] == 12
    level = sj["volumes"][0]["levels"][2]
    assert level["factor"] == 4 and "extent" in level
    assert level["origin"] == open_pyramid(tmp_path / "out" / "v0.zarr" / "raw").levels[2].origin   # from the OME transforms
    assert sj["artifact"]["binding_hash"] == binding_hash(b) and sj["artifact"]["harness_commit"] == "c" * 40
    root = zarr.open_group(tmp_path / "out" / "v0.zarr", mode="r").attrs.asdict()
    assert convention_errors(root) == [] and root["ome"]["labels"] == ["gt"]
    assert root["mia"] == {"schema": 2, "checks": {"geometry": {"samples": 12, "levels": 6, "mismatches": []}},
                           "crops": {"path": "../v0-crops", "format": "neuroglancer_annotations_v1"}}
    crops = json.loads((tmp_path / "out" / "v0-crops" / "info").read_text())   # the planned crops as boxes
    assert crops["@type"] == "neuroglancer_annotations_v1" and crops["spatial"][0]["limit"] == 6 * 3
    assert [i.get("uri", i.get("id")) for i in root["provenance:steps"][0]["inputs"]] == [
        "r", server.url("/raw0.npy"), server.url("/labels0.npy"), snippet["uri"]]      # the root's inputs are every group's


def test_stage_refuses_a_plan_over_the_byte_cap(tmp_path, io, server):
    b, r = write(tmp_path, (3, 600, 700))
    source_bytes = served(server, b)
    plan = plan_input((1, 2, 4), 64, {"train": 50})
    samples = derive_samples(plan.rule, "r", 0, "zyx", (3, 600, 700), {a: v.nm for a, v in VOXEL.items()},
                             {"train": 50})
    budget = planned_bytes(stage_plan(samples, "zyx", (3, 600, 700), (1, 2, 4), 64), 2, 1)
    rep = stage(b, SNAP, r, io, plan=plan, voxel=VOXEL, out=tmp_path / "out", stage_bytes=1000)
    assert not rep["ok"] and rep["errors"][0] == f"plan exceeds stage cap: {budget} > 1000 bytes"
    assert not (tmp_path / "out" / "v0.zarr" / "raw" / "s0").exists()
    assert 0 < io.bytes_fetched <= source_bytes   # what opening the arrays read, never twice


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
    raw = open_pyramid(tmp_path / "out" / "v0.zarr" / "raw")
    lab = open_pyramid(tmp_path / "out" / "v0.zarr" / "labels" / "gt")
    assert raw.axes == "cyx" and lab.axes == "yx" and len(rep["volumes"][0]["intensity_sample"]) == 2

    def chunks(p):
        return [sum(1 for f in (p.path / lv.path).rglob("*") if f.is_file() and f.name != "zarr.json") for lv in p.levels]
    assert chunks(raw) == [2 * n for n in chunks(lab)]        # one chunk per channel over the same boxes
    assert [lv.origin for lv in raw.levels] == [lv.origin for lv in lab.levels]


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
    assert rep["errors"] == ["no samples planned"]


def write_two(tmp_path, first, second):
    """Binding + record for two npy-local raws of these shapes; returns (binding, record)."""
    b, r = write(tmp_path, first)
    d = yaml.safe_load(b.read_text())
    path = tmp_path / "raw_b.npy"
    np.save(path, np.arange(np.prod(second), dtype=np.uint16).reshape(second))
    d["raw"].append({"snippet": "npy-local", "urls": [], "params": {"path": str(path)}})
    b.write_text(yaml.safe_dump(d))
    return b, r


def test_stage_shares_a_records_crops_among_its_volumes(tmp_path, io, server):
    b, r = write_two(tmp_path, (3, 600, 700), (30, 600, 700))
    served(server, b)
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 40}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    train = {volume["path"]: len(volume["samples"]["train"]) for volume in rep["volumes"]}
    assert sum(train.values()) == 40 and train["v1.zarr"] > train.get("v0.zarr", 0)   # 18 train planes against 2


def test_stage_writes_only_volumes_that_drew_a_crop(tmp_path, io, server):
    b, r = write_two(tmp_path, (3, 600, 700), (30, 600, 700))
    served(server, b)
    out = tmp_path / "out"
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 1}), voxel=VOXEL, out=out)
    assert rep["errors"] == [] and len(rep["volumes"]) == 1
    assert [d.name for d in sorted(out.glob("v*.zarr"))] == [rep["volumes"][0]["path"]]
    assert check_stage(out)["ok"]


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


def test_stage_transfers_what_its_crops_touch_and_keeps_no_whole_file(tmp_path, io, server):
    """A reader that reads by range decides what a stage transfers: one crop of a 10 MB source costs the blocks
    of its plane, and nothing lands in the dataset cache but the validators."""
    b, r = write(tmp_path, (12, 600, 700))
    source_bytes = served(server, b)
    d = yaml.safe_load(b.read_text())
    d["raw"][0]["params"]["lazy"] = True
    b.write_text(yaml.safe_dump(d))
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 1}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    assert 0 < rep["bytes_fetched"] < source_bytes // 2
    assert io._local(d["raw"][0]["urls"][0]) is None
    assert [p.name for p in (tmp_path / "shared").iterdir()] == ["files.json"]


def test_stage_takes_a_file_whole_where_ranges_will_not_do(tmp_path, io, server):
    b, r = write(tmp_path, SHAPE)
    source_bytes = served(server, b)
    server.ignore_range = True
    url = yaml.safe_load(b.read_text())["raw"][0]["urls"][0]
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 1}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    assert rep["bytes_fetched"] == source_bytes and io._local(url).stat().st_size == source_bytes
    assert url in json.loads((tmp_path / "shared" / "files.json").read_text())


def test_a_whole_file_that_fails_part_way_leaves_no_cache_entry(tmp_path, io, server, monkeypatch):
    monkeypatch.setattr("mia_harness.io.time.sleep", lambda seconds: None)   # the retries' waits
    b, r = write(tmp_path, SHAPE)
    served(server, b)
    server.ignore_range, server.fail = True, 99        # every body is cut short
    url = yaml.safe_load(b.read_text())["raw"][0]["urls"][0]
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 1}), voxel=VOXEL, out=tmp_path / "out")
    assert not rep["ok"] and rep["errors"] and rep["bytes_fetched"] == 0
    assert io._local(url) is None
    assert [p.name for p in (tmp_path / "shared").iterdir()] == ["files.json"]   # no half-written file left


def test_an_offline_stage_reads_the_cache(tmp_path, make_io, server):
    """A stage with no hosts (a record whose files are in the raw cache already) makes no request."""
    from mia_harness.fetch import fetch

    b, r = write(tmp_path, SHAPE, SHAPE)
    served(server, b)
    assert fetch(b, r, make_io())["ok"]                # `harness fetch`: how a person fills a cache by hand
    requests = len(server.requests)
    offline = make_io(hosts=())
    rep = stage(b, SNAP, r, offline, plan=plan_input((1, 2, 4), 64, {"train": 1}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    assert rep["bytes_fetched"] == 0 and len(server.requests) == requests


# ---- filters: a sample is staged only if its full-resolution box passes each (sample-filters spec §5.4) ----

BOTH = ({"name": "content"}, {"name": "labelled", "min_fraction": 0.05})
TISSUE = (slice(None), slice(200, 500), slice(300, 650))   # where the fixture's raw has texture and its label ids


def filtered(levels, patch, samples, filters=BOTH) -> PlanInput:
    plan = plan_input(levels, patch, samples)
    return dataclasses.replace(plan, rule=dataclasses.replace(plan.rule, filters=filters))


def write_tissue(tmp_path, shape=SHAPE, label=True, blank=False):
    """`write`'s binding and record, with a raw that is one value outside TISSUE and noise inside (nowhere when
    `blank`), and a label that is 3 inside and 0 outside. Returns (binding, record, raw, label)."""
    b, r = write(tmp_path, shape, shape if label else None)
    raw = np.full(shape, 500, np.uint16)
    if not blank:
        raw[TISSUE] = np.random.default_rng(0).integers(0, 4000, raw[TISSUE].shape, dtype=np.uint16)
    lab = np.zeros(shape, np.uint16)
    if not blank:
        lab[TISSUE] = 3
    np.save(tmp_path / "raw.npy", raw)
    if label:
        np.save(tmp_path / "label.npy", lab)
    return b, r, raw, lab


def test_stage_without_filters_writes_no_selection(tmp_path, io, server):
    b, r = write(tmp_path, SHAPE, SHAPE)
    served(server, b)
    rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 4}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["ok"] and "unstaged" not in rep and all("selection" not in volume for volume in rep["volumes"])
    step, = zarr.open_group(tmp_path / "out" / "v0.zarr" / "raw", mode="r").attrs["provenance:steps"]
    assert "filters" not in step["parameters"]["plan"]


def test_filters_keep_only_crops_on_content_and_labels(tmp_path, io, server):
    b, r, raw, lab = write_tissue(tmp_path)
    served(server, b)
    out = tmp_path / "out"
    rep = stage(b, SNAP, r, io, plan=filtered((1, 2, 4), 64, {"train": 12, "val": 4, "test": 4}), voxel=VOXEL,
                out=out)
    assert rep["ok"], rep["errors"]
    volume, = rep["volumes"]
    assert {split: len(kept) for split, kept in volume["samples"].items()} == {"train": 12, "val": 4, "test": 4}
    for kept in volume["samples"].values():
        for sample in kept:
            (y, x), z = sample["starts"]["1"], sample["plane"]["z"]
            assert (lab[z, y:y + 64, x:x + 64] != 0).mean() >= 0.05
            assert content_ok(raw[z, y:y + 64, x:x + 64])
    rejected = 0
    for split, counts in volume["selection"].items():
        assert counts["staged"] == len(volume["samples"][split]) == counts["wanted"]
        assert counts["staged"] + sum(counts["rejected"].values()) == counts["draws"]
        assert set(counts["rejected"]) <= {"repeat", "labelled", "content"}
        rejected += sum(counts["rejected"].values())
    assert rejected > 0    # the tissue is a sixth of the plane: some draws fell outside it
    step, = zarr.open_group(out / "v0.zarr" / "raw", mode="r").attrs["provenance:steps"]
    assert step["parameters"]["plan"]["filters"] == list(BOTH)
    assert check_stage(out)["ok"]


def test_a_volume_without_labels_passes_labelled(tmp_path, io, server):
    b, r, raw, _ = write_tissue(tmp_path, label=False)
    served(server, b)
    rep = stage(b, SNAP, r, io, plan=filtered((1, 2, 4), 64, {"train": 6}), voxel=VOXEL, out=tmp_path / "out")
    assert rep["ok"], rep["errors"]
    selection = rep["volumes"][0]["selection"]["train"]
    assert selection["staged"] == 6 and "labelled" not in selection["rejected"]


def test_a_record_with_nothing_passable_fails_naming_the_filters(tmp_path, io, server, monkeypatch):
    monkeypatch.setattr(plan_module, "GIVE_UP", 30)
    b, r, _, _ = write_tissue(tmp_path, blank=True)
    served(server, b)
    out = tmp_path / "out"
    rep = stage(b, SNAP, r, io, plan=filtered((1, 2, 4), 64, {"train": 4}), voxel=VOXEL, out=out)
    assert not rep["ok"] and rep["volumes"] == [] and not list(out.glob("v*.zarr"))
    assert rep["errors"] == ["no crop passed the filters (content, labelled): "
                             "v0 train: 30 draws, rejected labelled 30"]
    assert rep["unstaged"] == [{"volume": "v0.zarr", "planes": 5, "selection": {
        "train": {"wanted": 4, "staged": 0, "draws": 30, "rejected": {"labelled": 30}},
        "val": {"wanted": 0, "staged": 0, "draws": 0, "rejected": {}},
        "test": {"wanted": 0, "staged": 0, "draws": 0, "rejected": {}}}}]


def test_a_volume_that_accepted_nothing_is_unstaged_and_check_stage_passes(tmp_path, io, server, monkeypatch):
    monkeypatch.setattr(plan_module, "GIVE_UP", 30)
    b, r = write_two(tmp_path, (30, 600, 700), (30, 600, 700))
    np.save(tmp_path / "raw.npy", np.full((30, 600, 700), 500, np.uint16))    # the first raw is blank slide
    served(server, b)
    out = tmp_path / "out"
    rep = stage(b, SNAP, r, io, plan=filtered((1, 2, 4), 64, {"train": 40}, ({"name": "content"},)), voxel=VOXEL,
                out=out)
    assert rep["ok"], rep["errors"]
    assert [volume["path"] for volume in rep["volumes"]] == ["v1.zarr"] == [d.name for d in out.glob("v*.zarr")]
    short, = rep["unstaged"]
    assert short["volume"] == "v0.zarr" and short["selection"]["train"]["rejected"] == {"content": 30}
    assert short["planes"] == 30    # what registration needs to keep the written volume's share of the record
    assert check_stage(out)["ok"]


def test_an_unknown_filter_is_a_stage_error(tmp_path, io, server):
    b, r = write(tmp_path, SHAPE, SHAPE)
    served(server, b)
    rep = stage(b, SNAP, r, io, plan=filtered((1, 2, 4), 64, {"train": 4}, ({"name": "sharp"},)), voxel=VOXEL,
                out=tmp_path / "out")
    assert rep["errors"] == ["ValueError: unknown filter 'sharp'"]


class Counted:
    """A reader over an array in memory that counts its reads."""
    voxel_size_nm = None

    def __init__(self, data: np.ndarray, axes: str, access: str = "chunk"):
        self.data, self.axes, self.shape, self.dtype, self.access = data, axes, data.shape, data.dtype, access
        self.reads = 0

    def read(self, region):
        self.reads += 1
        return self.data[tuple(region.get(axis, slice(None)) for axis in self.axes)]


def sample_at(y: int, x: int, z: int | None = None) -> dict:
    return {"plane": {} if z is None else {"z": z}, "starts": {"1": [y, x]}}


def test_a_rejected_label_draw_reads_no_raw():
    raw = Counted(np.random.default_rng(0).integers(0, 255, (3, 200, 200), dtype=np.uint8), "cyx")
    label = Counted(np.zeros((200, 200), np.uint16), "yx")
    label.data[:64, :64] = 9
    keep = keeper(filtered((1,), 64, {}).rule, raw, [label])
    assert keep(sample_at(100, 100)) == "labelled" and (label.reads, raw.reads) == (1, 0)
    assert keep(sample_at(0, 0)) is None and (label.reads, raw.reads) == (2, 3)   # one read per channel
    raw.data[:, :64, :64] = 255
    assert keep(sample_at(0, 0)) == "content"


def test_a_filter_that_reads_no_label_reads_none():
    raw, label = Counted(np.zeros((200, 200), np.uint8), "yx"), Counted(np.ones((200, 200), np.uint16), "yx")
    assert keeper(filtered((1,), 64, {}, ({"name": "content"},)).rule, raw, [label])(sample_at(0, 0)) == "content"
    assert label.reads == 0
    assert keeper(plan_input((1,), 64, {}).rule, raw, [label]) is None


def test_a_plane_reader_is_read_once_per_plane_in_a_row():
    noise = np.random.default_rng(0).integers(0, 255, (4, 200, 200), dtype=np.uint8)
    raw, label = Counted(noise, "zyx", "plane"), Counted(np.ones((4, 200, 200), np.uint16), "zyx", "plane")
    keep = keeper(filtered((1,), 64, {}).rule, raw, [label])
    assert keep(sample_at(0, 0, z=1)) is None and keep(sample_at(100, 100, z=1)) is None
    assert (label.reads, raw.reads) == (1, 1)
    assert keep(sample_at(0, 0, z=2)) is None and (label.reads, raw.reads) == (2, 2)


def test_a_plane_reader_with_channels_is_read_once_per_channel():
    noise = np.random.default_rng(0).integers(0, 255, (3, 200, 200), dtype=np.uint8)
    raw, label = Counted(noise, "cyx", "plane"), Counted(np.ones((2, 200, 200), np.uint16), "cyx", "plane")
    keep = keeper(filtered((1,), 64, {}).rule, raw, [label])
    for corner in (0, 40, 80, 120):
        assert keep(sample_at(corner, corner)) is None
    assert (label.reads, raw.reads) == (2, 3)   # each channel's plane once, however many draws
