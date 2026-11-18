"""A one-section MRC image without the replicated-edge padding around it, or a headerless text table of X Y
pixel coordinates (one point per line) as an instance label on that same image.

One url, the MRC: the raw image. Two urls, the points table then the MRC the points were picked on: the label,
for which only the MRC's 1024-byte header is read. `rows` and `columns` name the image inside a padded file; the
raw and its label take the same two, so they share one grid. The padding is never returned, and a read that
finds anything but copies of the image's edge next to it fails, so the params cannot cut pixels away.
"""
import struct
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

MODES = {1: "i2", 2: "f4", 6: "u2", 12: "f2"}   # MRC2014 mode -> dtype; mode 0 needs mrc-http-3's IMOD handling
MAX_TABLE = 16 << 20                             # bytes; a larger first url is not a points table (urls swapped)


class _Grid:
    """What the MRC header and the params say: the file's grid and the image inside it."""

    def __init__(self, fh: BinaryIO, params: dict) -> None:
        h = fh.read(1024)
        if len(h) < 1024:
            raise ValueError(f"the MRC url is {len(h)} bytes, too short for an MRC header")
        order = ">" if h[212] == 0x11 else "<"                     # MACHST: 0x44 little, 0x11 big
        nx, ny, nz, mode = struct.unpack_from(order + "4i", h, 0)
        mx, my, mz, *cell = struct.unpack_from(order + "3i3f", h, 28)
        maps = struct.unpack_from(order + "3i", h, 64)
        (nsymbt,) = struct.unpack_from(order + "i", h, 92)
        if min(nx, ny, nz) < 1 or nsymbt < 0 or maps != (1, 2, 3):
            raise ValueError(f"not an MRC image this snippet reads: n = {nx, ny, nz}, nsymbt = {nsymbt}, "
                             f"mapc/mapr/maps = {maps} (only 1 2 3)")
        if nz != 1:
            raise ValueError(f"the MRC has {nz} sections; this snippet reads a one-section image (mrc-http-3 "
                             f"reads volumes)")
        if mode not in MODES:
            raise ValueError(f"unsupported MRC mode {mode} (supported: {sorted(MODES)}; mrc-http-3 reads mode 0)")
        self.file_dtype = np.dtype(order + MODES[mode])
        self.ny, self.nx, self.data = ny, nx, 1024 + nsymbt
        self.y0, self.y1 = self._span(params, "rows", ny)          # the image: file rows y0..y1 - 1
        self.x0, self.x1 = self._span(params, "columns", nx)       # and file columns x0..x1 - 1
        self.shape: tuple[int, ...] = (1, self.y1 - self.y0, self.x1 - self.x0)
        # cell is in Å over m samples (MRC2014); fall back to n when m is unset
        n = [m if m > 0 else k for m, k in zip((mx, my, mz), (nx, ny, nz))]
        self.voxel_size_nm: dict[str, float] | None = (
            {a: c / k / 10 for a, c, k in zip("xyz", cell, n)} if all(c > 0 for c in cell) else None)

    @staticmethod
    def _span(params: dict, key: str, n: int) -> tuple[int, int]:
        v = params.get(key)
        if v is None:
            return 0, n
        if (not isinstance(v, list) or len(v) != 2 or any(isinstance(i, bool) or not isinstance(i, int) for i in v)
                or not 0 <= v[0] <= v[1] < n):
            raise ValueError(f"param {key} must be [first, last], 0-based and inclusive, inside the file's "
                             f"0..{n - 1}, got {v!r}")
        return v[0], v[1] + 1


class _Image:
    axes: str = "zyx"
    access: str = "chunk"         # a read is one range: the full-width file rows it spans

    def __init__(self, fh: BinaryIO, params: dict) -> None:
        self._fh, self._g = fh, _Grid(fh, params)
        g = self._g
        self.shape: tuple[int, ...] = g.shape
        self.dtype: np.dtype = g.file_dtype.newbyteorder("=")
        self.voxel_size_nm: dict[str, float] | None = g.voxel_size_nm
        end = g.data + g.nx * g.ny * self.dtype.itemsize
        if fh.seek(0, 2) < end:
            raise ValueError(f"file too short: {fh.tell()} bytes, header says data ends at {end}")

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        g = self._g
        nz, ny, nx = self.shape
        z, y, x = (region.get(a, slice(None)) for a in "zyx")
        if not isinstance(z, int):
            planes = [self.read({"z": k, "y": y, "x": x}) for k in range(nz)[z]]
            return np.stack(planes) if planes else np.empty((0, ny, nx), self.dtype)[:, y, x]
        _, rows = range(nz)[z], range(ny)[y]
        if isinstance(rows, int):
            return self.read({"z": z, "y": slice(rows, rows + 1), "x": x})[0]
        if not rows:
            return np.empty((0, nx), self.dtype)[:, x]
        lo, hi = g.y0 + min(rows[0], rows[-1]), g.y0 + max(rows[0], rows[-1]) + 1      # file rows lo..hi - 1
        # one padding row more where the read touches the image's first or last row, to check it is padding
        a, b = lo - (lo == g.y0 > 0), hi + (hi == g.y1 < g.ny)
        row = g.nx * self.dtype.itemsize
        self._fh.seek(g.data + a * row)
        buf = self._fh.read((b - a) * row)
        self._check_padding(np.frombuffer(buf, f"u{self.dtype.itemsize}").reshape(b - a, g.nx), a, b)
        block = np.frombuffer(buf, g.file_dtype).reshape(b - a, g.nx)[lo - a:hi - a, g.x0:g.x1]
        return block[np.asarray(rows) + g.y0 - lo][:, x].astype(self.dtype)

    def _check_padding(self, bits: np.ndarray, a: int, b: int) -> None:
        """`bits` is file rows a..b - 1 as stored. What `rows` and `columns` leave out of them must be copies of
        the image's edge, bit for bit; otherwise the params would be cutting image away."""
        g = self._g
        bad = []
        if not (bits[:, :g.x0] == bits[:, g.x0:g.x0 + 1]).all():
            bad.append(f"columns 0..{g.x0 - 1} are not copies of column {g.x0}")
        if not (bits[:, g.x1:] == bits[:, g.x1 - 1:g.x1]).all():
            bad.append(f"columns {g.x1}..{g.nx - 1} are not copies of column {g.x1 - 1}")
        if a < g.y0 and not np.array_equal(bits[0], bits[1]):
            bad.append(f"row {a} is not a copy of row {g.y0}")
        if b > g.y1 and not np.array_equal(bits[-1], bits[-2]):
            bad.append(f"row {g.y1} is not a copy of row {g.y1 - 1}")
        if bad:
            raise ValueError(f"rows/columns leave out pixels that are not replicated-edge padding (file rows "
                             f"{a}..{b - 1}): {'; '.join(bad)}. This snippet only leaves out padding")


class _Points:
    axes: str = "zyx"
    access: str = "file"          # the first read reads the whole table (a few KB); nothing smaller is addressable
    dtype: np.dtype = np.dtype("uint32")
    voxel_size_nm: dict[str, float] | None = None   # a points table states none

    def __init__(self, table: BinaryIO, mrc: BinaryIO, params: dict, radius: int) -> None:
        self._table, self._radius, self._g = table, radius, _Grid(mrc, params)
        self.shape: tuple[int, ...] = self._g.shape
        if table.seek(0, 2) > MAX_TABLE:
            raise ValueError(f"the first url is {table.tell()} bytes, not a points table (limit {MAX_TABLE}); "
                             f"urls are [points table, MRC image]")
        self.n_points: int | None = None                           # set by the first read
        self.n_in_padding: int | None = None                       # of those, the points outside rows/columns
        self._xy: np.ndarray | None = None                         # (n, 2) int64: column, row of the image

    def _parse(self) -> np.ndarray:
        g = self._g
        self._table.seek(0)
        try:
            text = self._table.read().decode("ascii")
        except UnicodeDecodeError as e:
            raise ValueError(f"the first url is not a text table: {e}") from None
        points: list[tuple[int, int]] = []
        for number, line in enumerate(text.splitlines(), 1):
            fields = line.split()
            if not fields:
                continue
            if len(fields) != 2 or not all(f.isdigit() for f in fields):
                raise ValueError(f"line {number} is {line[:60]!r}, not two non-negative integers 'X Y'")
            x, y = int(fields[0]), int(fields[1])
            if x >= g.nx or y >= g.ny:
                raise ValueError(f"line {number}: point x={x} y={y} is outside the {g.nx} x {g.ny} grid of the MRC")
            points.append((x - g.x0, y - g.y0))                    # file pixel -> image pixel
        if len(points) > np.iinfo(self.dtype).max:
            raise ValueError(f"{len(points)} points exceed the ids of {self.dtype}")
        xy = np.array(points, np.int64).reshape(-1, 2)
        _, ny, nx = self.shape
        self.n_points = len(xy)
        self.n_in_padding = int(((xy < 0) | (xy >= (nx, ny))).any(axis=1).sum())
        return xy

    def _paint(self, lo: int, hi: int, c0: int, c1: int) -> np.ndarray:
        """Rows lo..hi, columns c0..c1: each pixel within `radius` of a point holds the row number of the nearest
        one (the earlier row on a tie), every other pixel 0. A point in the padding marks what its disc reaches."""
        r = self._radius
        out = np.zeros((hi - lo, c1 - c0), self.dtype)
        best = np.full(out.shape, r * r + 1, np.int64)             # squared distance to the nearest point so far
        px, py = self._xy[:, 0], self._xy[:, 1]
        for i in np.nonzero((py + r >= lo) & (py - r < hi) & (px + r >= c0) & (px - r < c1))[0]:
            y0, y1 = max(py[i] - r, lo), min(py[i] + r + 1, hi)
            x0, x1 = max(px[i] - r, c0), min(px[i] + r + 1, c1)
            d2 = (np.arange(y0, y1)[:, None] - py[i]) ** 2 + (np.arange(x0, x1)[None, :] - px[i]) ** 2
            window = (slice(y0 - lo, y1 - lo), slice(x0 - c0, x1 - c0))
            near = d2 < best[window]
            best[window][near] = d2[near]
            out[window][near] = i + 1
        return out

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nz, ny, nx = self.shape
        z, y, x = (region.get(a, slice(None)) for a in "zyx")
        if not isinstance(z, int):
            planes = [self.read({"z": k, "y": y, "x": x}) for k in range(nz)[z]]
            return np.stack(planes) if planes else np.zeros((0, ny, nx), self.dtype)[:, y, x]
        _, rows, cols = range(nz)[z], range(ny)[y], range(nx)[x]
        if isinstance(rows, int):
            return self.read({"z": z, "y": slice(rows, rows + 1), "x": x})[0]
        if isinstance(cols, int):
            return self.read({"z": z, "y": y, "x": slice(cols, cols + 1)})[:, 0]
        if not rows or not cols:
            return np.zeros((len(rows), len(cols)), self.dtype)
        if self._xy is None:
            self._xy = self._parse()
        lo, c0 = min(rows[0], rows[-1]), min(cols[0], cols[-1])
        block = self._paint(lo, max(rows[0], rows[-1]) + 1, c0, max(cols[0], cols[-1]) + 1)
        return block[np.asarray(rows) - lo][:, np.asarray(cols) - c0]


def open_array(src: Source, io: IO) -> Array:
    radius = src.params.get("radius")
    if len(src.urls) == 1:
        if radius is not None:
            raise ValueError("param radius is for a points label (urls [points table, MRC image]), not for the image")
        return _Image(io.open(src.urls[0]), src.params)
    if len(src.urls) != 2:
        raise ValueError(f"mrc-unpadded-xy-points takes one url (the MRC image) or two ([points table, MRC "
                         f"image]), got {len(src.urls)}")
    radius = 0 if radius is None else radius
    if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
        raise ValueError(f"param radius must be a whole number of pixels >= 0, got {radius!r}")
    return _Points(io.open(src.urls[0]), io.open(src.urls[1]), src.params, radius)
