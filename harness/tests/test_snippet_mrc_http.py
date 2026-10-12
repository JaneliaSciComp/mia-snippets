"""snippets/mrc-http, loaded from the repo's own snippets/ through the loader, over the range server."""
import struct
from pathlib import Path

import mrcfile
import numpy as np
import pytest
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness.api import Source, check_array
from mia_harness.cache import DatasetCache
from mia_harness.io import BLOCK, IO
from mia_harness.loader import load_snippet, withdrawn_names

ROOT = Path(__file__).resolve().parents[2]      # the repo is the snapshot
RNG = np.random.default_rng(0)
MODES = {0: "int8", 1: "int16", 2: "float32", 6: "uint16", 12: "float16"}


@pytest.fixture
def io(tmp_path):
    i = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30,
           {"127.0.0.1"})
    yield i
    i.close()


def mrc(data, mode, order="<", nsymbt=0, cell=(30.0, 40.0, 50.0)) -> bytes:
    """A minimal MRC2014 file: header, `nsymbt` bytes of extended header, data in `order`."""
    nz, ny, nx = data.shape
    h = bytearray(1024)
    struct.pack_into(order + "4i", h, 0, nx, ny, nz, mode)
    struct.pack_into(order + "3i3f", h, 28, nx, ny, nz, *cell)       # mx my mz, cell a b c (Å)
    struct.pack_into(order + "3i", h, 64, 1, 2, 3)                   # mapc mapr maps
    struct.pack_into(order + "i", h, 92, nsymbt)
    h[208:214] = b"MAP " + (b"\x44\x44" if order == "<" else b"\x11\x11")
    return bytes(h) + b"\x07" * nsymbt + data.astype(np.dtype(data.dtype).newbyteorder(order)).tobytes()


def open_(io, url):
    mod = load_snippet(ROOT, "mrc-http", withdrawn_names(ROOT), {})
    a = mod.open_array(Source((url,), {}), io)
    check_array(a)
    return a


@pytest.mark.parametrize("order", ["<", ">"])
@pytest.mark.parametrize("mode, dtype", MODES.items())
def test_modes_byte_orders_and_extended_header(server, io, mode, dtype, order):
    data = (RNG.random((3, 40, 30)) * 100).astype(dtype)
    server.files["/m.mrc"] = mrc(data, mode, order, nsymbt=100)
    a = open_(io, server.url("/m.mrc"))
    assert (a.axes, a.shape, a.dtype, a.access) == ("zyx", (3, 40, 30), np.dtype(dtype), "chunk")
    assert a.voxel_size_nm == pytest.approx({"z": 5 / 3, "y": 0.1, "x": 0.1})   # cell / m (m = n here), Å -> nm
    assert np.array_equal(a.read({"z": 2, "y": slice(5, 17), "x": slice(3, 29)}), data[2, 5:17, 3:29])
    assert np.array_equal(a.read({"z": slice(None), "y": 39, "x": slice(None)}), data[:, 39])


def test_a_patch_is_one_range_of_rows(server, io):
    data = RNG.random((4, 600, 500)).astype(np.float32)               # 1.2 MB a plane
    server.files["/big.mrc"] = body = mrc(data, 2)
    a = open_(io, server.url("/big.mrc"))
    gets = len(server.gets())
    assert np.array_equal(a.read({"z": 3, "y": slice(100, 356), "x": slice(10, 266)}), data[3, 100:356, 10:266])
    assert len(server.gets()) == gets + 1                             # rows 100..356 of plane 3, one request
    assert io.bytes_fetched <= 3 * BLOCK < len(body)


def test_agrees_with_mrcfile(server, io, tmp_path):
    data = RNG.random((5, 32, 48)).astype(np.float32)
    with mrcfile.new(tmp_path / "ref.mrc") as m:
        m.set_data(data)
        m.voxel_size = (6.802, 6.802, 6.802)
    server.files["/ref.mrc"] = (tmp_path / "ref.mrc").read_bytes()
    a = open_(io, server.url("/ref.mrc"))
    assert a.shape == (5, 32, 48) and a.voxel_size_nm == pytest.approx({"z": 0.6802, "y": 0.6802, "x": 0.6802})
    assert np.array_equal(a.read({"z": 4, "y": slice(None), "x": slice(None)}), data[4])


@pytest.mark.parametrize("patch, match", [
    ({"mode": 4}, "mode"),                                             # complex64
    ({"mapc": 3}, "axis order"),
    ({"truncate": True}, "short"),
])
def test_bad_headers_are_refused(server, io, patch, match):
    body = bytearray(mrc(np.zeros((2, 8, 8), np.float32), patch.get("mode", 2)))
    if "mapc" in patch:
        struct.pack_into("<3i", body, 64, 3, 2, 1)
    server.files["/bad.mrc"] = bytes(body[:-10] if patch.get("truncate") else body)
    with pytest.raises(ValueError, match=match):
        open_(io, server.url("/bad.mrc"))
