import json
from pathlib import Path

import numpy as np
import pytest
from test_io import make_io  # noqa: F401  (fixture)
from test_smoke import SNAP, write
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness import cli
from mia_harness.cache import DatasetCache
from mia_harness.io import IO
from mia_harness.pyramid import open_pyramid
from mia_harness.stage import stage

VOX = {"z": 100.0, "y": 8.0, "x": 8.0}


@pytest.fixture
def io(tmp_path):
    i = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30, {"127.0.0.1"})
    yield i
    i.close()


def test_stage_writes_raw_and_label_pyramids(tmp_path, io):
    b, r = write(tmp_path, (2, 300, 300), (2, 300, 300))
    rep = stage(b, SNAP, r, io, levels=[1, 2], patch=64, out=tmp_path / "out", voxel_size_nm=VOX)
    assert rep["errors"] == [] and rep["ok"]
    assert rep["volumes"][0]["labels"] == {"gt": "labels/gt"} and rep["checks"] == {"s0_chunks": 16, "level_blocks": 8}
    assert rep["volumes"][0]["path"] == "v0.zarr" and rep["volumes"][0]["axes"] == "zyx" and rep["volumes"][0]["voxel_size_nm"] == VOX
    assert open_pyramid(tmp_path / "out" / "v0.zarr" / "raw").levels[1].shape == (2, 150, 150)
    assert open_pyramid(tmp_path / "out" / "v0.zarr" / "labels" / "gt").attrs["mia"]["label_key"] == "gt"
    assert json.loads((tmp_path / "out" / "stage.json").read_text())["ok"]
    # the volume is one OME-Zarr image: root multiscales over raw/s*, the labels group listing its labels
    root = json.loads((tmp_path / "out" / "v0.zarr" / "zarr.json").read_text())["attributes"]["ome"]
    assert root["labels"] == ["gt"] and root["multiscales"][0]["type"] == "image"
    assert [d["path"] for d in root["multiscales"][0]["datasets"]] == ["raw/s0", "raw/s1"]
    assert root["multiscales"][0]["axes"] == [{"name": a, "type": "space", "unit": "nanometer"} for a in "zyx"]
    labels = json.loads((tmp_path / "out" / "v0.zarr" / "labels" / "zarr.json").read_text())["attributes"]["ome"]
    assert labels == {"version": "0.5", "labels": ["gt"]}
    import zarr
    assert sorted(zarr.open_group(tmp_path / "out" / "v0.zarr", mode="r").keys()) == ["labels", "raw"]


def test_stage_without_voxel_size_fails(tmp_path, io):
    b, r = write(tmp_path, (2, 300, 300))
    rep = stage(b, SNAP, r, io, levels=[1, 2], patch=64, out=tmp_path / "out")
    assert rep["errors"] == ["raw 0: voxel size unknown"] and not rep["ok"]


def test_stage_cli(tmp_path, monkeypatch):
    b, r = write(tmp_path, (2, 300, 300))
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", "1000000")
    monkeypatch.setenv("MIA_HARNESS_ALLOWED_HOSTS", "127.0.0.1")
    args = ["stage", str(b), "--snippets", str(SNAP), "--record", str(r), "--work", str(tmp_path / "work"),
            "--levels", "1,2", "--patch", "64", "--out", str(tmp_path / "out")]
    assert cli.main(args) == 1  # no voxel size anywhere
    assert cli.main(args + ["--voxel-size-nm", "100,8,8"]) == 0
    assert json.loads((tmp_path / "out" / "stage.json").read_text())["volumes"][0]["levels"][1]["factor"] == 2


def test_stage_verification_catches_corruption(tmp_path, io, monkeypatch):
    import mia_harness.pyramid as pyramid

    b, r = write(tmp_path, (2, 300, 300))
    monkeypatch.setattr(pyramid, "block_mean", lambda plane, f: plane if f == 1 else np.zeros((plane.shape[0] // f, plane.shape[1] // f), plane.dtype))
    rep = stage(b, SNAP, r, io, levels=[1, 2], patch=64, out=tmp_path / "out", voxel_size_nm=VOX)
    assert not rep["ok"] and any("s1" in e and "differs" in e for e in rep["errors"])


def test_voxel_size_merges_file_and_fallback_per_axis():
    from mia_harness.stage import _voxel_size

    class A:
        axes, voxel_size_nm = "zcyx", {"y": 45.0718, "x": 45.0718}

    assert _voxel_size(A(), None) is None
    assert _voxel_size(A(), {"z": 1.0, "y": 40.0, "x": 40.0}) == {"z": 1.0, "y": 45.0718, "x": 45.0718}


def test_stage_verification_covers_labels(tmp_path, io, monkeypatch):
    import mia_harness.pyramid as pyramid

    b, r = write(tmp_path, (2, 300, 300), (2, 300, 300))
    monkeypatch.setattr(pyramid, "stride", lambda plane, f: plane if f == 1 else np.zeros((plane.shape[0] // f, plane.shape[1] // f), plane.dtype))
    rep = stage(b, SNAP, r, io, levels=[1, 2], patch=64, out=tmp_path / "out", voxel_size_nm=VOX)
    assert not rep["ok"] and any("v0/gt s1" in e and "differs" in e for e in rep["errors"])


def test_stage_records_a_sorted_intensity_sample_per_channel(tmp_path, io):
    b, r = write(tmp_path, (2, 300, 300), (2, 300, 300))
    rep = stage(b, SNAP, r, io, levels=[1, 2], patch=64, out=tmp_path / "out", voxel_size_nm=VOX)
    sample = rep["volumes"][0]["intensity_sample"]
    assert len(sample) == 1 and 1 <= len(sample[0]) <= 4096 and sample[0] == sorted(sample[0])
    assert all(isinstance(v, (int, float)) and 0 <= v < 2 * 300 * 300 for v in sample[0])
    assert json.loads((tmp_path / "out" / "stage.json").read_text())["volumes"][0]["intensity_sample"] == sample


def test_intensity_sample_is_per_channel():
    from mia_harness.stage import intensity_sample

    class FakePyramid:
        axes = "czyx"
        levels = [type("L", (), {"shape": (2, 3, 100, 100)})()]

        def read(self, level, region):
            c = region["c"]
            ys, xs = region["y"], region["x"]
            return np.full((ys.stop - ys.start, xs.stop - xs.start), 10 * (c + 1), dtype=np.uint8)

    sample = intensity_sample(FakePyramid(), 64, np.random.default_rng(0))
    assert len(sample) == 2 and set(sample[0]) == {10} and set(sample[1]) == {20} and len(sample[0]) == 4096


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
