"""A headerless text table of X Y pixel coordinates (one point per line) as an instance label on an MRC's grid.

Two urls: the points table, then the MRC image the points were picked on. Only the MRC's 1024-byte header is
read, for the grid size; a points table says nothing about the image it belongs to. Each point becomes one
pixel, or a disc of `radius` pixels, whose value is the point's 1-based row number in the table.
"""
import struct
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

MAX_TABLE = 16 << 20              # bytes; a larger first url is not a points table (e.g. the urls are swapped)


def _grid(fh: BinaryIO) -> tuple[int, int, int]:
    """(nz, ny, nx) from an MRC header; the image data is never read."""
    h = fh.read(1024)
    if len(h) < 1024:
        raise ValueError(f"the second url is {len(h)} bytes, too short for an MRC header; urls are "
                         f"[points table, MRC image]")
    order = ">" if h[212] == 0x11 else "<"                         # MACHST: 0x44 little, 0x11 big
    nx, ny, nz = struct.unpack_from(order + "3i", h, 0)
    maps = struct.unpack_from(order + "3i", h, 64)
    if min(nx, ny, nz) < 1 or maps != (1, 2, 3):
        raise ValueError(f"the second url is not an MRC image this snippet can take the grid from: n = "
                         f"{nx, ny, nz}, mapc/mapr/maps = {maps} (only 1 2 3); urls are [points table, MRC image]")
    if nz != 1:
        raise ValueError(f"the MRC has {nz} sections; an X Y table has no z, so only a one-section image is a grid")
    return nz, ny, nx


class _Points:
    axes: str = "zyx"
    access: str = "file"          # the first read reads the whole table (a few KB); nothing smaller is addressable
    dtype: np.dtype = np.dtype("uint32")
    voxel_size_nm: dict[str, float] | None = None   # a points table states none

    def __init__(self, table: BinaryIO, grid: BinaryIO, radius: int) -> None:
        self._table, self._radius = table, radius
        self.shape: tuple[int, ...] = _grid(grid)
        if table.seek(0, 2) > MAX_TABLE:
            raise ValueError(f"the first url is {table.tell()} bytes, not a points table (limit {MAX_TABLE}); "
                             f"urls are [points table, MRC image]")
        self.n_points: int | None = None                           # set by the first read
        self._xy: np.ndarray | None = None                         # (n, 2) int64: column, row

    def _parse(self) -> np.ndarray:
        _, ny, nx = self.shape
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
            if x >= nx or y >= ny:
                raise ValueError(f"line {number}: point x={x} y={y} is outside the {nx} x {ny} grid of the MRC")
            points.append((x, y))
        if len(points) > np.iinfo(self.dtype).max:
            raise ValueError(f"{len(points)} points exceed the ids of {self.dtype}")
        self.n_points = len(points)
        return np.array(points, np.int64).reshape(-1, 2)

    def _paint(self, lo: int, hi: int, c0: int, c1: int) -> np.ndarray:
        """Rows lo..hi, columns c0..c1: each pixel within `radius` of a point holds the row number of the nearest
        one (the earlier row on a tie), every other pixel 0."""
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
    radius = src.params.get("radius", 0)
    if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
        raise ValueError(f"param radius must be a whole number of pixels >= 0, got {radius!r}")
    if len(src.urls) != 2:
        raise ValueError(f"xy-points-mrc-grid takes two urls, [points table, MRC image], got {len(src.urls)}")
    return _Points(io.open(src.urls[0]), io.open(src.urls[1]), radius)
