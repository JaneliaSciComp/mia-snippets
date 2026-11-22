import json
import struct

from mia_harness.boxes import COLORS, write_crop_boxes


def read_cell(path, rank):
    raw = path.read_bytes()
    n = struct.unpack_from("<Q", raw)[0]
    size = -(-(rank * 8 + 5) // 4) * 4
    assert len(raw) == 8 + n * (size + 8)
    ids = struct.unpack_from(f"<{n}Q", raw, 8 + n * size)
    return {i: raw[8 + k * size: 8 + (k + 1) * size] for k, i in enumerate(ids)}


def test_crop_boxes_one_per_sample_and_level_in_nm(tmp_path):
    samples = {"train": [{"plane": {"z": 3}, "centre_nm": [0, 0], "levels": [1, 2], "patch": 4,
                          "starts": {"1": [10, 20], "2": [4, 9]}}],
               "val": [{"plane": {"z": 7}, "centre_nm": [0, 0], "levels": [1, 2], "patch": 4,
                        "starts": {"1": [0, 0], "2": [0, 0]}}]}
    assert write_crop_boxes(tmp_path / "crops", samples, {"z": 30.0, "y": 2.0, "x": 2.0}) == 4
    info = json.loads((tmp_path / "crops" / "info").read_text())
    assert info["dimensions"] == {"z": [1e-9, "m"], "y": [1e-9, "m"], "x": [1e-9, "m"]}
    assert [p["id"] for p in info["properties"]] == ["level", "split", "color"]
    assert info["spatial"] == [{"key": "spatial0", "grid_shape": [1, 1, 1], "limit": 4,
                                "chunk_size": [u - l for l, u in zip(info["lower_bound"], info["upper_bound"])]}]
    cell = read_cell(tmp_path / "crops" / "spatial0" / "0_0_0", 3)   # one coordinate per axis
    assert set(cell) == {1, 2, 3, 4}
    # train sample, level 1: raw pixels [10, 14) × [20, 24) on plane 3, half a voxel in from the centres
    assert struct.unpack("<6f", cell[1][:24]) == (2.5 * 30, 9.5 * 2, 19.5 * 2, 3.5 * 30, 13.5 * 2, 23.5 * 2)
    assert struct.unpack("<5B", cell[1][24:29]) == (1, 0, *COLORS[1]) and cell[1][29:] == b"\0\0\0"
    # level 2: start 4 at factor 2 is raw pixel 8, the window 8 raw pixels wide
    assert struct.unpack("<6f", cell[2][:24]) == (2.5 * 30, 7.5 * 2, 17.5 * 2, 3.5 * 30, 15.5 * 2, 25.5 * 2)
    assert struct.unpack("<5B", cell[2][24:29])[:2] == (2, 0)
    assert struct.unpack("<5B", cell[3][24:29])[:2] == (1, 1)     # the val sample
    for i, record in cell.items():
        assert (tmp_path / "crops" / "by_id" / str(i)).read_bytes() == record
    assert info["lower_bound"] == [2.5 * 30, -0.5 * 2, -0.5 * 2] and info["upper_bound"][0] == 7.5 * 30


def test_crop_boxes_of_a_plane_have_no_z(tmp_path):
    samples = {"train": [{"plane": {}, "centre_nm": [0, 0], "levels": [1], "patch": 2, "starts": {"1": [1, 1]}}]}
    assert write_crop_boxes(tmp_path / "crops", samples, {"y": 1.0, "x": 1.0}) == 1
    info = json.loads((tmp_path / "crops" / "info").read_text())
    assert list(info["dimensions"]) == ["y", "x"] and info["lower_bound"] == [0.5, 0.5]
    cell = read_cell(tmp_path / "crops" / "spatial0" / "0_0", 2)
    assert struct.unpack("<4f", cell[1][:16]) == (0.5, 0.5, 2.5, 2.5) and len(cell[1]) == 24


def test_no_samples_write_no_layer(tmp_path):
    assert write_crop_boxes(tmp_path / "crops", {"train": []}, {"y": 1.0, "x": 1.0}) == 0
    assert not (tmp_path / "crops").exists()
