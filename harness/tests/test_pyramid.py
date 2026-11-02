import json

import numpy as np
import pytest
import zarr

from mia_harness.plan import dense_plan, stage_plan, start_index
from mia_harness.pyramid import LevelInfo, block_mean, open_pyramid, stride, write_pyramid, write_volume_groups
from mia_harness.schemas import ngff

VOX = {"z": 300.0, "y": 10.0, "x": 10.0}


class NpArray:
    """A minimal Array (SPEC §2) over an in-memory ndarray, axes in the given order."""

    access = "chunk"

    def __init__(self, axes: str, data: np.ndarray, voxel_size_nm=None):
        self.axes, self.data, self.voxel_size_nm = axes, data, voxel_size_nm
        self.shape, self.dtype = data.shape, data.dtype

    def read(self, region):
        return self.data[tuple(region.get(a, slice(None)) for a in self.axes)]


def volume(z=3, y=600, x=700, dtype=np.uint16):
    return NpArray("zyx", np.arange(z * y * x, dtype=dtype).reshape(z, y, x))


def plan_for(shape, levels=(1, 2, 4), patch=64, centres=((3000.0, 3500.0),), z=(1,)):
    """A stage plan for zyx `shape` at 10 nm on y/x: one sample per centre on each listed plane."""
    samples = {"train": [{"plane": {"z": zz}, "centre_nm": list(c), "levels": list(levels), "patch": patch,
                          "starts": {str(L): [start_index(c[0], 10.0, L, patch, shape[-2]), start_index(c[1], 10.0, L, patch, shape[-1])] for L in levels}}
                         for zz in z for c in centres]}
    return stage_plan(samples, "zyx", shape, tuple(levels), patch)


def test_writes_0_6_sparse_levels(tmp_path):
    arr, plan = volume(), plan_for((3, 600, 700))
    info = write_pyramid(arr, tmp_path / "raw", plan=plan, voxel_size_nm=VOX, kind="raw", coordinate_system="r.v0", mia={"schema": 1}, downsampling_metadata={"factors": [1, 2, 4]})
    g = zarr.open_group(tmp_path / "raw", mode="r")
    ome = g.attrs["ome"]
    assert ome["version"] == "0.6" and "version" not in ome["multiscales"][0]
    ms = ome["multiscales"][0]
    assert ms["coordinateSystems"] == [{"name": "r.v0", "axes": [{"name": a, "type": "space", "unit": "nanometer"} for a in "zyx"]}]
    o = info.levels[1].origin
    assert ms["datasets"][1]["coordinateTransformations"] == [{"type": "sequence", "input": {"path": "s1"}, "output": {"name": "r.v0"},
        "transformations": [{"type": "scale", "scale": [300.0, 20.0, 20.0]},
                            {"type": "translation", "translation": [o["z"] * 300.0, (o["y"] + 0.5) * 10.0, (o["x"] + 0.5) * 10.0]}]}]
    assert ms["type"] == "mean" and ms["metadata"] == {"factors": [1, 2, 4]}
    assert g["s0"].shape == (1, 64, 64) and g["s0"].chunks == (1, 64, 64)
    y0, x0 = info.levels[0].origin["y"], info.levels[0].origin["x"]
    np.testing.assert_array_equal(g["s0"][0], arr.data[1, y0:y0 + 64, x0:x0 + 64])
    y2, x2 = info.levels[2].origin["y"], info.levels[2].origin["x"]
    np.testing.assert_array_equal(g["s2"][0], block_mean(arr.data[1, y2:y2 + 256, x2:x2 + 256], 4))
    assert ngff.errors("image", {"ome": ome}) == [] and ngff.errors("strict_image", {"ome": ome}) == []
    assert g.attrs["mia"] == {"schema": 1}


def test_unplanned_plane_chunks_are_absent_and_background_label_chunks_are_present(tmp_path):
    plan = plan_for((5, 600, 700), levels=(1,), z=(0, 4))
    write_pyramid(volume(z=5), tmp_path / "raw", plan=plan, voxel_size_nm=VOX, kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    assert (tmp_path / "raw" / "s0" / "c" / "0" / "0" / "0").is_file() and (tmp_path / "raw" / "s0" / "c" / "4" / "0" / "0").is_file()
    assert not (tmp_path / "raw" / "s0" / "c" / "2").exists()
    zeros = NpArray("zyx", np.zeros((5, 600, 700), dtype=np.uint8))
    write_pyramid(zeros, tmp_path / "lab", plan=plan, voxel_size_nm=VOX, kind="label", coordinate_system="r.v0", mia={"segmentation_type": "binary"}, downsampling_metadata={})
    assert (tmp_path / "lab" / "s0" / "c" / "0" / "0" / "0").is_file()   # write_empty_chunks=True
    lab = zarr.open_group(tmp_path / "lab", mode="r").attrs["ome"]
    assert lab["image-label"]["source"] == {"image": "../../raw"} and [c["label-value"] for c in lab["image-label"]["colors"]] == [0, 1]
    assert lab["multiscales"][0]["type"] == "nearest"
    assert ngff.errors("label", {"ome": lab}) == []


def test_plane_access_reads_each_planned_plane_once(tmp_path):
    class PlaneArray(NpArray):
        access = "plane"

        def read(self, region):
            self.reads.append(region)
            return super().read(region)

    arr = PlaneArray("zyx", volume(z=5).data)
    arr.reads = []
    plan = plan_for((5, 600, 700), z=(0, 4))
    info = write_pyramid(arr, tmp_path / "raw", plan=plan, voxel_size_nm=VOX, kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    assert [r["z"] for r in arr.reads] == [0, 4]
    y2, x2 = info.levels[2].origin["y"], info.levels[2].origin["x"]
    np.testing.assert_array_equal(zarr.open_group(tmp_path / "raw", mode="r")["s2"][4], block_mean(arr.data[4, y2:y2 + 256, x2:x2 + 256], 4))


def test_xy_stored_source_is_transposed(tmp_path):
    data = volume().data
    arr = NpArray("zxy", np.ascontiguousarray(data.transpose(0, 2, 1)))
    info = write_pyramid(arr, tmp_path / "raw", plan=plan_for((3, 600, 700)), voxel_size_nm=VOX, kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    y0, x0 = info.levels[0].origin["y"], info.levels[0].origin["x"]
    np.testing.assert_array_equal(zarr.open_group(tmp_path / "raw", mode="r")["s0"][0], data[1, y0:y0 + 64, x0:x0 + 64])


def test_2d_yx_volume_writes_without_a_plane_axis(tmp_path):
    arr = NpArray("yx", np.arange(600 * 700, dtype=np.uint16).reshape(600, 700))
    samples = {"train": [{"plane": {}, "centre_nm": [3000.0, 3500.0], "levels": [1], "patch": 64, "starts": {"1": [268, 318]}}]}
    info = write_pyramid(arr, tmp_path / "yx", plan=stage_plan(samples, "yx", (600, 700), (1,), 64), voxel_size_nm={"y": 10.0, "x": 10.0}, kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    assert info.levels[0].shape == (64, 64) and zarr.open_group(tmp_path / "yx", mode="r")["s0"].chunks == (64, 64)
    np.testing.assert_array_equal(zarr.open_group(tmp_path / "yx", mode="r")["s0"][:], arr.data[268:332, 318:382])


def test_bool_label_becomes_uint8_and_float_label_is_refused(tmp_path):
    plan = plan_for((3, 600, 700), levels=(1,))
    info = write_pyramid(NpArray("zyx", np.ones((3, 600, 700), dtype=bool)), tmp_path / "b", plan=plan, voxel_size_nm=VOX, kind="label", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    assert info.dtype == "uint8"
    assert "colors" not in zarr.open_group(tmp_path / "b", mode="r").attrs["ome"]["image-label"]
    with pytest.raises(ValueError, match="integer"):
        write_pyramid(NpArray("zyx", np.ones((3, 600, 700), dtype=np.float32)), tmp_path / "f", plan=plan, voxel_size_nm=VOX, kind="label", coordinate_system="r.v0", mia={}, downsampling_metadata={})


def test_volume_root_is_one_0_6_image(tmp_path):
    plan = plan_for((3, 600, 700))
    write_pyramid(volume(), tmp_path / "v0.zarr" / "raw", plan=plan, voxel_size_nm=VOX, kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    write_pyramid(volume(dtype=np.uint8), tmp_path / "v0.zarr" / "labels" / "gt", plan=plan, voxel_size_nm=VOX, kind="label", coordinate_system="r.v0", mia={"label_key": "gt"}, downsampling_metadata={})
    assert zarr.open_group(tmp_path / "v0.zarr" / "labels" / "gt", mode="r").attrs["ome"]["multiscales"][0]["name"] == "gt"
    write_volume_groups(tmp_path / "v0.zarr", ["gt"], {"schema": 1})
    root = json.loads((tmp_path / "v0.zarr" / "zarr.json").read_text())["attributes"]
    assert root["ome"]["labels"] == ["gt"] and [d["path"] for d in root["ome"]["multiscales"][0]["datasets"]] == ["raw/s0", "raw/s1", "raw/s2"]
    assert root["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["input"] == {"path": "raw/s0"}
    assert ngff.errors("image", {"ome": root["ome"]}) == [] and root["mia"] == {"schema": 1}
    labels = json.loads((tmp_path / "v0.zarr" / "labels" / "zarr.json").read_text())["attributes"]
    assert labels == {"ome": {"version": "0.6", "labels": ["gt"]}}


def test_remainder_is_trimmed_consistently(tmp_path):
    raw, label = volume(y=601, x=701), volume(y=601, x=701, dtype=np.uint8)
    plan = dense_plan("zyx", (3, 601, 701), (1, 2, 4), 64)
    a = write_pyramid(raw, tmp_path / "raw", plan=plan, voxel_size_nm=VOX, kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    b = write_pyramid(label, tmp_path / "lab", plan=plan, voxel_size_nm=VOX, kind="label", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    assert [lv.shape for lv in a.levels] == [(3, 601, 701), (3, 300, 350), (3, 150, 175)]
    assert [lv.shape for lv in b.levels] == [lv.shape for lv in a.levels]
    g = zarr.open_group(tmp_path / "lab", mode="r")
    np.testing.assert_array_equal(g["s1"][2], stride(label.data[2], 2))
    np.testing.assert_array_equal(zarr.open_group(tmp_path / "raw", mode="r")["s0"][:], raw.data)


def test_channels_axis_and_2d(tmp_path):
    arr = NpArray("cyx", np.arange(3 * 100 * 120, dtype=np.uint16).reshape(3, 100, 120))
    info = write_pyramid(arr, tmp_path / "c", plan=dense_plan("cyx", (3, 100, 120), (1, 2), 32), voxel_size_nm={"y": 45.0, "x": 45.0},
                         kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    g = zarr.open_group(tmp_path / "c", mode="r")
    assert g["s0"].chunks == (1, 32, 32) and info.levels[1].shape == (3, 50, 60)
    ms = g.attrs["ome"]["multiscales"][0]
    assert ms["coordinateSystems"][0]["axes"][0] == {"name": "c", "type": "channel"}
    assert ms["datasets"][0]["coordinateTransformations"][0]["transformations"] == [
        {"type": "scale", "scale": [1.0, 45.0, 45.0]}, {"type": "translation", "translation": [0.0, 0.0, 0.0]}]
    np.testing.assert_array_equal(g["s1"][2], block_mean(arr.data[2], 2))
    flat = NpArray("yx", np.zeros((70, 80), dtype=np.uint8))
    info2 = write_pyramid(flat, tmp_path / "f", plan=dense_plan("yx", (70, 80), (1,), 32), voxel_size_nm={"y": 1.0, "x": 1.0},
                          kind="raw", coordinate_system="r.v0", mia={}, downsampling_metadata={})
    assert info2.levels == [LevelInfo("s0", 1, (70, 80), {"y": 1.0, "x": 1.0}, {"y": 0.0, "x": 0.0}, {"y": 0, "x": 0})]


def _lead_layout(tmp_path):
    """The lead's crop layout: storage axes x, y, z; half-pixel translation at s1."""
    data = np.arange(40 * 30 * 5, dtype=np.uint8).reshape(40, 30, 5)
    g = zarr.open_group(tmp_path / "crop.zarr" / "raw", mode="w", zarr_format=3)
    g.create_array(name="s0", shape=data.shape, chunks=(8, 8, 1), dtype=data.dtype)[:] = data
    g.create_array(name="s1", shape=(20, 15, 5), chunks=(8, 8, 1), dtype=data.dtype)[:] = data[::2, ::2, :]
    g.attrs["ome"] = {"version": "0.5", "multiscales": [{"axes": [
        {"name": "x", "type": "space", "unit": "nanometer"}, {"name": "y", "type": "space", "unit": "nanometer"},
        {"name": "z", "type": "space", "unit": "nanometer"}], "datasets": [
        {"path": "s0", "coordinateTransformations": [{"type": "scale", "scale": [4.3, 4.3, 45.0]}]},
        {"path": "s1", "coordinateTransformations": [{"type": "scale", "scale": [8.6, 8.6, 45.0]}, {"type": "translation", "translation": [2.15, 2.15, 0.0]}]}]}]}
    return data


def test_open_pyramid_reads_canonical_order_from_xyz_storage(tmp_path):
    data = _lead_layout(tmp_path)
    p = open_pyramid(tmp_path / "crop.zarr" / "raw")
    assert p.axes == "zyx" and p.factor_of(1) == 2 and p.levels[0].shape == (5, 30, 40)
    assert p.levels[1].translation_nm == {"z": 0.0, "y": 2.15, "x": 2.15} and p.levels[1].scale_nm == {"z": 45.0, "y": 8.6, "x": 8.6}
    np.testing.assert_array_equal(p.read(0, {"z": 2, "y": slice(0, 30), "x": slice(0, 40)}), data[:, :, 2].T)
    np.testing.assert_array_equal(p.read(1, {"z": 4, "y": slice(3, 10), "x": slice(2, 9)}), data[::2, ::2, :][2:9, 3:10, 4].T)
    assert p.dtype == np.uint8


def test_open_pyramid_roundtrip(tmp_path):
    arr = volume()
    write_pyramid(arr, tmp_path / "p", plan=dense_plan("zyx", arr.shape, (1, 2, 4), 64), voxel_size_nm=VOX, kind="raw",
                  coordinate_system="r.v0", mia={"record": "r"}, downsampling_metadata={})
    p = open_pyramid(tmp_path / "p")
    assert p.axes == "zyx" and [lv.factor for lv in p.levels] == [1, 2, 4] and p.attrs["mia"]["record"] == "r"
    np.testing.assert_array_equal(p.read(1, {"z": 1, "y": slice(10, 20), "x": slice(0, 5)}), block_mean(arr.data[1], 2)[10:20, 0:5])


def test_block_mean_rounds_back_to_dtype():
    plane = np.array([[1, 2, 10], [3, 4, 10]], dtype=np.uint8)
    np.testing.assert_array_equal(block_mean(plane, 2), np.array([[2]], dtype=np.uint8))  # mean 2.5 → rint → 2
    assert block_mean(plane, 2).dtype == np.uint8 and stride(plane, 2).shape == (1, 1)
    with pytest.raises(ValueError):
        block_mean(plane, 0)


def test_block_mean_does_not_copy_the_plane_as_float64():
    import tracemalloc

    plane = np.arange(1024 * 1024, dtype=np.uint16).reshape(1024, 1024)
    tracemalloc.start()
    out = block_mean(plane, 2)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert out.shape == (512, 512) and peak < 3 * plane.nbytes   # a float64 copy of the plane alone would be 4×
