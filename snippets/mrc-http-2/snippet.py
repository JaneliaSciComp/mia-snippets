"""An MRC/REC volume over range requests, like mrc-http, plus unsigned mode 0 (IMOD flag or `mode0` param)."""
import struct
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

MODES = {0: "i1", 1: "i2", 2: "f4", 6: "u2", 12: "f2"}   # MRC2014 mode -> dtype (mode 0: see _mode0_dtype)
MODE0 = {"int8": "i1", "uint8": "u1"}
IMOD_STAMP = 1146047817                                   # "IMOD" in the header's imodStamp word (byte 152)


def _mode0_dtype(h: bytes, order: str, param: str | None) -> str:
    """Mode 0 is signed by MRC2014; IMOD wrote unsigned bytes unless bit 0 of imodFlags says signed."""
    if param is not None:
        if param not in MODE0:
            raise ValueError(f"mode0 must be one of {sorted(MODE0)}, got {param!r}")
        return MODE0[param]
    stamp, flags = struct.unpack_from(order + "2i", h, 152)
    return "u1" if stamp == IMOD_STAMP and not flags & 1 else "i1"


class _Mrc:
    axes: str = "zyx"
    access: str = "chunk"

    def __init__(self, fh: BinaryIO, mode0: str | None = None) -> None:
        self._fh = fh
        h = fh.read(1024)
        if len(h) < 1024:
            raise ValueError(f"file is {len(h)} bytes, too short for an MRC header")
        order = ">" if h[212] == 0x11 else "<"                     # MACHST: 0x44 little, 0x11 big
        nx, ny, nz, mode = struct.unpack_from(order + "4i", h, 0)
        mx, my, mz, *cell = struct.unpack_from(order + "3i3f", h, 28)
        maps = struct.unpack_from(order + "3i", h, 64)
        (nsymbt,) = struct.unpack_from(order + "i", h, 92)
        if mode not in MODES:
            raise ValueError(f"unsupported MRC mode {mode} (supported: {sorted(MODES)})")
        if mode0 is not None and mode != 0:
            raise ValueError(f"mode0 = {mode0!r} was given, but the file is MRC mode {mode}")
        if min(nx, ny, nz) < 1 or nsymbt < 0:
            raise ValueError(f"bad MRC header: n = {nx, ny, nz}, nsymbt = {nsymbt}")
        if maps != (1, 2, 3):
            raise ValueError(f"unsupported axis order mapc/mapr/maps = {maps}, only 1 2 3")
        code = _mode0_dtype(h, order, mode0) if mode == 0 else MODES[mode]
        self._file_dtype: np.dtype = np.dtype(order + code)
        self.dtype: np.dtype = self._file_dtype.newbyteorder("=")
        self.shape: tuple[int, ...] = (nz, ny, nx)
        self._data: int = 1024 + nsymbt
        end = self._data + nx * ny * nz * self.dtype.itemsize
        if fh.seek(0, 2) < end:
            raise ValueError(f"file too short: {fh.tell()} bytes, header says data ends at {end}")
        # cell is in Å over m samples (MRC2014); fall back to n when m is unset
        n = [m if m > 0 else k for m, k in zip((mx, my, mz), (nx, ny, nz))]
        self.voxel_size_nm: dict[str, float] | None = (
            {a: c / k / 10 for a, c, k in zip("xyz", cell, n)} if all(c > 0 for c in cell) else None)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nz, ny, nx = self.shape
        z, y, x = (region.get(a, slice(None)) for a in "zyx")
        if not isinstance(z, int):
            return np.stack([self.read({"z": k, "y": y, "x": x}) for k in range(nz)[z]])
        k, rows = range(nz)[z], range(ny)[y]
        if isinstance(rows, int):
            return self.read({"z": k, "y": slice(rows, rows + 1), "x": x})[0]
        if not rows:
            return np.empty((0, nx), self.dtype)[:, x]
        lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
        row = nx * self.dtype.itemsize
        self._fh.seek(self._data + (k * ny + lo) * row)           # rows lo..hi of plane k: one range
        block = np.frombuffer(self._fh.read((hi - lo) * row), self._file_dtype).reshape(hi - lo, nx)
        return block[np.asarray(rows) - lo][:, x].astype(self.dtype)


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"mrc-http-2 reads one file, got {len(src.urls)} urls")
    return _Mrc(io.open(src.urls[0]), src.params.get("mode0"))
