import json
from pathlib import Path

import numpy as np
import pytest
from test_smoke import SNAP, write

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
    assert rep["volumes"][0]["labels"] == {"gt": "labels/gt"} and rep["checks"] == {"s0_chunks": 8, "level_blocks": 4}
    assert rep["volumes"][0]["path"] == "v0.zarr" and rep["volumes"][0]["axes"] == "zyx" and rep["volumes"][0]["voxel_size_nm"] == VOX
    assert open_pyramid(tmp_path / "out" / "v0.zarr" / "raw").levels[1].shape == (2, 150, 150)
    assert open_pyramid(tmp_path / "out" / "v0.zarr" / "labels" / "gt").attrs["mia"]["label_key"] == "gt"
    assert json.loads((tmp_path / "out" / "stage.json").read_text())["ok"]


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
