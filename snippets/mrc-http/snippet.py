"""An MRC/REC volume over range requests: the 1024-byte header parsed here, rows of one plane per read."""
import struct

import numpy as np

SPEC_VERSION = 1

MODES = {0: "i1", 1: "i2", 2: "f4", 6: "u2", 12: "f2"}   # MRC2014 mode -> dtype (mode 0 is signed)


class _Mrc:
    axes, access = "zyx", "chunk"

    # ponytail: own header parser, not mrcfile (which wants a local file); no extended-header parsing, no IMOD
    # unsigned mode 0, mapc/mapr/maps 1 2 3 only. A new snippet with mrcfile when a dataset needs those.
    def __init__(self, fh):
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
        if min(nx, ny, nz) < 1 or nsymbt < 0:
            raise ValueError(f"bad MRC header: n = {nx, ny, nz}, nsymbt = {nsymbt}")
        if maps != (1, 2, 3):
            raise ValueError(f"unsupported axis order mapc/mapr/maps = {maps}, only 1 2 3")
        self._file_dtype = np.dtype(order + MODES[mode])
        self.dtype = self._file_dtype.newbyteorder("=")
        self.shape = (nz, ny, nx)
        self._data = 1024 + nsymbt
        end = self._data + nx * ny * nz * self.dtype.itemsize
        if fh.seek(0, 2) < end:
            raise ValueError(f"file too short: {fh.tell()} bytes, header says data ends at {end}")
        # cell is in Å over m samples (MRC2014); fall back to n when m is unset
        n = [m if m > 0 else k for m, k in zip((mx, my, mz), (nx, ny, nz))]
        self.voxel_size_nm = ({a: c / k / 10 for a, c, k in zip("xyz", cell, n)}
                              if all(c > 0 for c in cell) else None)

    def read(self, region: dict) -> np.ndarray:
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


def open_array(src, io):
    if len(src.urls) != 1:
        raise ValueError(f"mrc-http reads one file, got {len(src.urls)} urls")
    return _Mrc(io.open(src.urls[0]))
