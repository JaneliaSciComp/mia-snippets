"""snippets/tiff-zip-member, loaded from the repo's own snippets/ through the loader, over the range server."""
import io as stdio
import zipfile
from pathlib import Path

import numpy as np
import pytest
import tifffile
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness.api import Source, check_array
from mia_harness.cache import DatasetCache
from mia_harness.io import BLOCK, IO
from mia_harness.loader import load_snippet, withdrawn_names

ROOT = Path(__file__).resolve().parents[2]      # the repo is the snapshot
RNG = np.random.default_rng(0)
STACK = RNG.integers(0, 4096, (16, 512, 512), dtype=np.uint16)  # 8 MiB: many 1 MiB blocks


@pytest.fixture
def io(tmp_path):
    i = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30,
           {"127.0.0.1"})
    yield i
    i.close()


def tiff(data, **kw) -> bytes:
    b = stdio.BytesIO()
    tifffile.imwrite(b, data, **{"photometric": "minisblack", **kw})
    return b.getvalue()


def imagej(data=STACK) -> bytes:
    return tiff(data, imagej=True, resolution=(1 / 0.065, 1 / 0.065),
                metadata={"axes": "ZYX", "spacing": 0.29, "unit": "micron"})


def zipped(name, data, method) -> bytes:
    b = stdio.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, data, compress_type=method)
    return b.getvalue()


def open_(io, url, **params):
    mod = load_snippet(ROOT, "tiff-zip-member", withdrawn_names(ROOT), params)
    a = mod.open_array(Source((url,), params), io)
    check_array(a)
    return a


def test_stored_member_is_read_plane_by_plane(server, io):
    data = imagej()
    server.files["/s.zip"] = zipped("a/stack.tif", data, zipfile.ZIP_STORED)
    a = open_(io, server.url("/s.zip") + "::a/stack.tif")
    assert (a.axes, a.shape, a.dtype, a.access) == ("zyx", (16, 512, 512), np.dtype("uint16"), "plane")
    assert a.voxel_size_nm == pytest.approx({"z": 290.0, "y": 65.0, "x": 65.0})
    region = {"z": 5, "y": slice(10, 300), "x": slice(7, 263)}
    assert np.array_equal(a.read(region), STACK[5, 10:300, 7:263])
    assert io.bytes_fetched <= 4 * BLOCK < len(data)   # zip directory, tiff header, one plane


def test_deflated_member_is_fetched_whole(server, io):
    data = imagej()
    server.files["/d.zip"] = zipped("stack.tif", data, zipfile.ZIP_DEFLATED)
    a = open_(io, server.url("/d.zip") + "::stack.tif")
    assert (a.axes, a.shape, a.access) == ("zyx", (16, 512, 512), "file")
    assert np.array_equal(a.read({"z": 7, "y": slice(None), "x": slice(0, 100)}), STACK[7, :, :100])


def test_plain_url_and_ome_voxel_size(server, io):
    server.files["/o.ome.tif"] = tiff(STACK[:3], ome=True, metadata={
        "axes": "ZYX", "PhysicalSizeX": 0.1, "PhysicalSizeY": 0.2, "PhysicalSizeZ": 0.5})
    a = open_(io, server.url("/o.ome.tif"))
    assert (a.axes, a.shape, a.access) == ("zyx", (3, 512, 512), "plane")
    assert a.voxel_size_nm == pytest.approx({"z": 500.0, "y": 200.0, "x": 100.0})
    assert np.array_equal(a.read({"z": 2, "y": slice(0, 5), "x": slice(0, 5)}), STACK[2, :5, :5])


def test_server_ignoring_range_falls_back_to_fetch(server, io):
    server.files["/p.tif"] = tiff(STACK[:2])
    server.ignore_range = True
    a = open_(io, server.url("/p.tif"))
    assert (a.axes, a.access, a.voxel_size_nm) == ("zyx", "file", None)   # tifffile says Q: z
    assert np.array_equal(a.read({"z": 1, "y": slice(0, 9), "x": slice(3, 9)}), STACK[1, :9, 3:9])


@pytest.mark.parametrize("data, kw, axes, region, want", [
    (STACK[0, :, :300].reshape(512, 100, 3).astype(np.uint8), {"photometric": "rgb"}, "yxc",
     {"c": 2, "y": slice(1, 4), "x": slice(0, 2)}, lambda d: d[1:4, 0:2, 2]),
    (STACK[:4].reshape(2, 2, 512, 512), {"imagej": True, "metadata": {"axes": "ZCYX"}}, "zcyx",
     {"z": 1, "c": 0, "y": slice(0, 3), "x": slice(0, 3)}, lambda d: d[1, 0, :3, :3]),
    (STACK[:2].reshape(1, 2, 512, 512), {"metadata": {"axes": "CZYX"}}, "zyx",     # size-1 c is dropped
     {"z": 1, "y": slice(0, 3), "x": slice(0, 3)}, lambda d: d[0, 1, :3, :3]),
    (STACK[0], {}, "yx", {"y": slice(500, 512), "x": slice(0, 4)}, lambda d: d[500:, :4]),
])
def test_axes_map_to_czyx(server, io, data, kw, axes, region, want):
    server.files["/x.tif"] = tiff(data, **kw)
    a = open_(io, server.url("/x.tif"))
    assert a.axes == axes and a.read(region).tolist() == want(data).tolist()


def test_unknown_axis_needs_the_axes_param(server, io):
    server.files["/t.tif"] = tiff(STACK[:3], metadata={"axes": "TYX"})
    with pytest.raises(ValueError, match="axes"):
        open_(io, server.url("/t.tif"))
    a = open_(io, server.url("/t.tif"), axes="zyx")
    assert a.axes == "zyx" and np.array_equal(a.read({"z": 2, "y": 0, "x": slice(0, 4)}), STACK[2, 0, :4])
    with pytest.raises(ValueError, match="axes"):
        open_(io, server.url("/t.tif"), axes="yx")    # one letter per (non-singleton) axis



@pytest.mark.parametrize("kw", [
    {"imagej": True, "resolution": (2, 2), "metadata": {"axes": "ZYX", "unit": "\\u00B5m", "spacing": 3.0}},
    {"imagej": True, "resolution": (2, 2), "metadata": {"axes": "ZYX", "unit": "micron", "spacing": 3.0}},
    {"ome": True, "metadata": {"axes": "ZYX", **{f"PhysicalSize{a}": v for a, v in zip("XYZ", (0.5, 0.5, 3))},
                               **{f"PhysicalSize{a}Unit": "\u03bcm" for a in "XYZ"}}},   # Greek mu
])
def test_micron_spellings(server, io, kw):
    server.files["/u.tif"] = tiff(STACK[:2], **kw)
    assert open_(io, server.url("/u.tif")).voxel_size_nm == pytest.approx({"z": 3000.0, "y": 500.0, "x": 500.0})
