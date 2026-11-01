import numpy as np
import pytest
import zarr

from mia_harness.pyramid import LevelInfo, block_mean, open_pyramid, stride, write_pyramid

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


def test_writes_levels_scales_and_translation(tmp_path):
    arr = volume()
    info = write_pyramid(arr, tmp_path / "v0.zarr" / "raw", levels=[1, 2, 4], patch=256, voxel_size_nm=VOX, kind="raw", attrs={"record": "r"})
    assert [lv.shape for lv in info.levels] == [(3, 600, 700), (3, 300, 350), (3, 150, 175)]
    assert info.levels[2].scale_nm == {"z": 300.0, "y": 40.0, "x": 40.0}
    assert info.levels[1].translation_nm == {"z": 0.0, "y": 5.0, "x": 5.0}
    assert info.axes == "zyx" and info.dtype == "uint16"
    g = zarr.open_group(tmp_path / "v0.zarr" / "raw", mode="r")
    ms = g.attrs["ome"]["multiscales"][0]
    assert g.attrs["ome"]["version"] == "0.5" and [a["name"] for a in ms["axes"]] == ["z", "y", "x"]
    assert ms["axes"][1] == {"name": "y", "type": "space", "unit": "nanometer"}
    assert ms["datasets"][1]["coordinateTransformations"] == [
        {"type": "scale", "scale": [300.0, 20.0, 20.0]},
        {"type": "translation", "translation": [0.0, 5.0, 5.0]},
    ]
    assert g["s0"].chunks == (1, 256, 256) and g["s0"].dtype == np.uint16
    np.testing.assert_array_equal(g["s0"][:], arr.data)
    np.testing.assert_array_equal(g["s1"][1], block_mean(arr.data[1], 2))
    np.testing.assert_array_equal(g["s2"][2], block_mean(arr.data[2], 4))
    assert g.attrs["mia"]["record"] == "r" and g.attrs["mia"]["levels"] == [1, 2, 4] and g.attrs["mia"]["source_axes"] == "zyx"


def test_label_levels_are_nearest(tmp_path):
    arr = volume(dtype=np.uint8)
    write_pyramid(arr, tmp_path / "l", levels=[1, 2], patch=64, voxel_size_nm=VOX, kind="label", attrs={})
    g = zarr.open_group(tmp_path / "l", mode="r")
    np.testing.assert_array_equal(g["s1"][0], stride(arr.data[0], 2))
    assert g.attrs["ome"]["image-label"]["version"] == "0.5"


def test_remainder_is_trimmed_consistently(tmp_path):
    raw, label = volume(y=601, x=701), volume(y=601, x=701, dtype=np.uint8)
    a = write_pyramid(raw, tmp_path / "raw", levels=[1, 2, 4], patch=64, voxel_size_nm=VOX, kind="raw", attrs={})
    b = write_pyramid(label, tmp_path / "lab", levels=[1, 2, 4], patch=64, voxel_size_nm=VOX, kind="label", attrs={})
    assert [lv.shape for lv in a.levels] == [(3, 601, 701), (3, 300, 350), (3, 150, 175)]
    assert [lv.shape for lv in b.levels] == [lv.shape for lv in a.levels]


def test_channels_axis_and_2d(tmp_path):
    arr = NpArray("cyx", np.zeros((3, 100, 120), dtype=np.uint16))
    info = write_pyramid(arr, tmp_path / "c", levels=[1, 2], patch=32, voxel_size_nm={"y": 45.0, "x": 45.0}, kind="raw", attrs={})
    g = zarr.open_group(tmp_path / "c", mode="r")
    assert g["s0"].chunks == (1, 32, 32) and info.levels[1].shape == (3, 50, 60)
    assert g.attrs["ome"]["multiscales"][0]["axes"][0] == {"name": "c", "type": "channel"}
    assert g.attrs["ome"]["multiscales"][0]["datasets"][0]["coordinateTransformations"] == [{"type": "scale", "scale": [1.0, 45.0, 45.0]}]
    flat = NpArray("yx", np.zeros((70, 80), dtype=np.uint8))
    info2 = write_pyramid(flat, tmp_path / "f", levels=[1], patch=32, voxel_size_nm={"y": 1.0, "x": 1.0}, kind="raw", attrs={})
    assert info2.levels == [LevelInfo("s0", 1, (70, 80), {"y": 1.0, "x": 1.0}, {"y": 0.0, "x": 0.0})]


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
    write_pyramid(arr, tmp_path / "p", levels=[1, 2, 4], patch=64, voxel_size_nm=VOX, kind="raw", attrs={"record": "r"})
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
