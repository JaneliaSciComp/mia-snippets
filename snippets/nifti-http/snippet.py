"""One single-file NIfTI-1 volume (.nii.gz or .nii) at a plain URL, as zyx in the file's own voxel order.

The 348-byte header is parsed here (no nibabel). `open_array` reads the header only: the first block of the
file, gunzipped as far as the header. A gzip file cannot be read by range, so the first `read` stages it whole
with `io.fetch` (access `file`) and inflates it as a stream up to the plane asked for; an uncompressed .nii is
read by row range (access `chunk`).
"""
import gzip
import math
import struct
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

HEADER = 348
# NIfTI-1 datatype code -> numpy type; RGB (128, 2304), complex (32, 1792, 2048), binary (1) and float128 are refused
DATATYPES = {2: "u1", 4: "i2", 8: "i4", 16: "f4", 64: "f8", 256: "i1", 512: "u2", 768: "u4", 1024: "i8", 1280: "u8"}
UNIT_CODES = {1: "m", 2: "mm", 3: "um"}             # xyzt_units & 7; 0 is "unknown"
UNIT_NM = {"m": 1e9, "mm": 1e6, "um": 1e3}


class _Nifti:
    def __init__(self, io: IO, url: str, stored_values: bool, pixdim_unit: str | None):
        self._io, self._url = io, url
        try:
            fh, ranged = io.open(url), True
            h, self._gz = self._header(fh)
        except NeedsFetch:      # no Content-Length, or a server without Range: stage the whole file
            fh, ranged = open(io.fetch(url), "rb"), False
            h, self._gz = self._header(fh)
        if len(h) < HEADER:
            raise ValueError(f"{url}: {len(h)} bytes, too short for a NIfTI-1 header")
        order = next((o for o in "<>" if struct.unpack_from(o + "i", h, 0)[0] == HEADER), None)
        if order is None:
            two = 540 in (struct.unpack_from("<i", h, 0)[0], struct.unpack_from(">i", h, 0)[0])
            raise ValueError(f"{url}: " + ("NIfTI-2 is not supported" if two else "not a NIfTI-1 file (sizeof_hdr)"))
        if h[344:348] != b"n+1\0":
            raise ValueError(f"{url}: magic {h[344:348]!r}, only single-file NIfTI-1 ('n+1') is read")
        dim = struct.unpack_from(order + "8h", h, 40)
        datatype, bitpix = struct.unpack_from(order + "2h", h, 70)
        pixdim = struct.unpack_from(order + "8f", h, 76)
        vox_offset, slope, inter = struct.unpack_from(order + "3f", h, 108)
        if not 2 <= dim[0] <= 7 or min(dim[1:dim[0] + 1]) < 1:
            raise ValueError(f"{url}: bad dim {dim}")
        if any(n != 1 for n in dim[4:dim[0] + 1]):
            raise ValueError(f"{url}: dim {dim} has more than x, y, z; time and vector dimensions are not read")
        if datatype not in DATATYPES:
            raise ValueError(f"{url}: unsupported datatype {datatype} (supported: {sorted(DATATYPES)})")
        self._stored = np.dtype(order + DATATYPES[datatype])
        if bitpix != self._stored.itemsize * 8:
            raise ValueError(f"{url}: bitpix {bitpix} does not fit datatype {datatype}")
        if not math.isfinite(vox_offset) or vox_offset < HEADER + 4 or vox_offset != int(vox_offset):
            raise ValueError(f"{url}: bad vox_offset {vox_offset}")
        self._data = int(vox_offset)

        nx, ny = dim[1], dim[2]         # NIfTI stores x fastest: the file is a C-order (z, y, x) block
        self._ny, self._nx = ny, nx
        self.axes = "zyx" if dim[0] >= 3 else "yx"
        self.shape: tuple[int, ...] = (dim[3], ny, nx) if dim[0] >= 3 else (ny, nx)

        # scl_slope 0 (or not finite) means "no scaling" in the standard; 1, 0 is the identity
        self._scale: tuple[float, float] | None = None
        if not stored_values and math.isfinite(slope) and slope != 0 and (slope, inter) != (1.0, 0.0):
            if not math.isfinite(inter):
                raise ValueError(f"{url}: scl_inter {inter} with scl_slope {slope}")
            self._scale = (slope, inter)
        native = self._stored.newbyteorder("=")
        small = native.itemsize <= 2 or native == np.float32
        self.dtype = native if self._scale is None else np.dtype(np.float32 if small else np.float64)

        unit = UNIT_CODES.get(h[123] & 7)
        if pixdim_unit is not None and unit not in (None, pixdim_unit):
            raise ValueError(f"{url}: params.pixdim_unit {pixdim_unit!r}, but the header's xyzt_units says {unit!r}")
        unit = unit or pixdim_unit
        zooms = dict(zip("xyz", pixdim[1:4]))
        self.voxel_size_nm: dict[str, float] | None = None
        if unit is not None and all(math.isfinite(zooms[a]) and zooms[a] > 0 for a in self.axes):
            # pixdim is float32: its shortest decimal (0.05, not 0.05000000074505806) times the unit
            self.voxel_size_nm = {a: float(str(np.float32(zooms[a]))) * UNIT_NM[unit] for a in self.axes}

        self._fh: BinaryIO | None = None
        self._plane: tuple[int, np.ndarray] | None = None     # gzip: the last plane inflated
        if self._gz:
            fh.close()
            self.access = "file"
        else:
            end = self._data + math.prod(self.shape) * self._stored.itemsize
            if fh.seek(0, 2) < end:
                raise ValueError(f"{url}: file too short: {fh.tell()} bytes, header says data ends at {end}")
            self._fh = fh
            self.access = "chunk" if ranged else "file"

    @staticmethod
    def _header(fh: BinaryIO) -> tuple[bytes, bool]:
        """The first 348 bytes of the NIfTI stream, and whether the file is gzip."""
        gz = fh.read(2) == b"\x1f\x8b"
        fh.seek(0)
        return (gzip.GzipFile(fileobj=fh).read(HEADER) if gz else fh.read(HEADER)), gz

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._yx(0, y, x)
        nz, z = self.shape[0], region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._yx(range(nz)[z], y, x)             # IndexError when out of range, like numpy
        planes = [self._yx(k, y, x) for k in range(nz)[z]]
        return np.stack(planes) if planes else np.empty((0, self._ny, self._nx), self.dtype)[:, y, x]

    def _yx(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
        ny, nx = self._ny, self._nx
        rows = range(ny)[y] if isinstance(y, slice) else range(range(ny)[y], range(ny)[y] + 1)
        if len(rows) == 0:
            return np.empty((ny, nx), self.dtype)[y, x]
        lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
        if self._gz:        # a gzip stream only runs forward: keep the plane, the next patch is often on it
            if self._plane is None or self._plane[0] != k:
                self._plane = (k, self._block(k * ny, ny))
            block = self._plane[1][lo:hi]
        else:
            block = self._block(k * ny + lo, hi - lo)       # rows lo..hi of plane k: one range
        if not isinstance(y, slice):
            picked = block[0, x]
        else:
            picked = (block if rows.step == 1 else block[np.asarray(rows) - lo])[:, x]
        if self._scale is None:
            return picked.astype(self.dtype)
        slope, inter = (self.dtype.type(v) for v in self._scale)
        return picked.astype(self.dtype) * slope + inter

    def _block(self, first_row: int, n_rows: int) -> np.ndarray:
        """`n_rows` whole rows from row `first_row` of the volume (rows counted through all planes), as stored."""
        if self._fh is None:
            self._fh = gzip.open(self._io.fetch(self._url), "rb")
        row = self._nx * self._stored.itemsize
        self._fh.seek(self._data + first_row * row)     # gzip: inflates forward, or again from the start
        buf = self._fh.read(n_rows * row)
        if len(buf) != n_rows * row:
            raise ValueError(f"{self._url}: file ends inside the voxel data")
        return np.frombuffer(buf, self._stored).reshape(n_rows, self._nx)


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"nifti-http reads one file, got {len(src.urls)} urls")
    stored = src.params.get("stored_values", False)
    unit = src.params.get("pixdim_unit")
    if not isinstance(stored, bool):
        raise ValueError(f"params.stored_values must be true or false, got {stored!r}")
    if unit is not None and unit not in UNIT_NM:
        raise ValueError(f"params.pixdim_unit must be one of {sorted(UNIT_NM)}, got {unit!r}")
    return _Nifti(io, src.urls[0], stored, unit)
