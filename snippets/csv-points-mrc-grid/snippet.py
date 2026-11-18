"""A CSV table of particle centres (one row per particle, `X-Coordinate` and `Y-Coordinate` columns in micrograph
pixels) as a label on the grid of the MRC micrograph it was picked on: zyx (1, ny, nx) uint16, 0 for background
and the particle's 1-based row number at its centre pixel (or, with `radius`, in a disc around it).

Two urls: the table, then the micrograph. Only the micrograph's 1024-byte header is read, for nx, ny and nz. The
table does not say whether its Y counts rows down from the first stored row or up from the last one, so the
binding has to: `y_axis`. `open_array` reads the header and the table (a few kB of text, no pixels) and checks
every row; the plane is painted at the first `read` and kept.
"""
import csv
import math
import struct

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

X, Y = "X-Coordinate", "Y-Coordinate"       # the columns read; every other column is ignored
Y_AXES = ("up", "down")
MRC_MODES = (0, 1, 2, 6, 12)                # the modes mrc-http-3 reads: this label pairs with what it opens
TABLE_MAX = 16 << 20                        # a coordinate table is kB; more than this is not one
RADIUS_MAX = 2048


def _head(io: IO, url: str, n: int) -> bytes:
    """The first n bytes of a file (fewer when it is shorter)."""
    try:
        with io.open(url) as f:
            return f.read(n)
    except NeedsFetch:          # a server without Range: stage the whole file
        with open(io.fetch(url), "rb") as f:
            return f.read(n)


def _grid(io: IO, url: str) -> tuple[int, int, int]:
    """(nz, ny, nx) from an MRC header, read as mrc-http-3 reads it."""
    h = _head(io, url, 1024)
    if len(h) < 1024:
        raise ValueError(f"{url}: {len(h)} bytes, too short for an MRC header")
    order = ">" if h[212] == 0x11 else "<"                      # MACHST: 0x44 little, 0x11 big
    nx, ny, nz, mode = struct.unpack_from(order + "4i", h, 0)
    maps = struct.unpack_from(order + "3i", h, 64)
    if mode not in MRC_MODES or min(nx, ny, nz) < 1:
        raise ValueError(f"{url}: not an MRC header this snippet reads (n = {nx, ny, nz}, mode {mode}); the second "
                         "url must be the micrograph the table's coordinates are on")
    if maps != (1, 2, 3):
        raise ValueError(f"{url}: unsupported axis order mapc/mapr/maps = {maps}, only 1 2 3")
    return nz, ny, nx


def _centres(data: bytes, url: str) -> list[tuple[float, float]]:
    """(x, y) of every data row, in file order; [] for a table with only its header line."""
    if len(data) > TABLE_MAX:
        raise ValueError(f"{url}: more than {TABLE_MAX} bytes, not a coordinate table")
    try:
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise UnicodeError("NUL bytes: a binary file")
    except UnicodeError:
        raise ValueError(f"{url}: not a text table: header={data[:8]!r}; the first url must be the CSV") from None
    rows = [r for r in csv.reader(text.splitlines()) if any(cell.strip() for cell in r)]
    header = [cell.strip() for cell in rows[0]] if rows else []
    if header.count(X) != 1 or header.count(Y) != 1:
        raise ValueError(f"{url}: the header line needs one {X!r} and one {Y!r} column, got {[c[:40] for c in header[:20]]}")
    ix, iy = header.index(X), header.index(Y)
    out = []
    for n, row in enumerate(rows[1:], 1):
        try:
            x, y = float(row[ix]), float(row[iy])
        except (IndexError, ValueError):
            raise ValueError(f"{url}: data row {n} has no number in {X!r} / {Y!r}: {row[:20]}") from None
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"{url}: data row {n} has a non-finite coordinate: {row[:20]}")
        out.append((x, y))
    return out


class _Points:
    axes: str = "zyx"
    access: str = "file"        # the table is read at open; reads cost no bytes
    dtype: np.dtype = np.dtype("uint16")
    voxel_size_nm: dict[str, float] | None = None       # the table has none

    def __init__(self, io: IO, table: str, micrograph: str, y_axis: str, radius: int) -> None:
        nz, ny, nx = _grid(io, micrograph)
        if nz != 1:
            raise ValueError(f"{micrograph}: {nz} sections; a coordinate table is one micrograph's, with no z")
        self.shape: tuple[int, ...] = (1, ny, nx)
        self._radius = radius
        self._plane: np.ndarray | None = None
        centres = _centres(_head(io, table, TABLE_MAX + 1), table)
        if len(centres) > 65535:
            raise ValueError(f"{table}: {len(centres)} particles exceed uint16 ids")
        self._pixels: list[tuple[int, int]] = []        # (row, column) of particle k + 1
        seen: dict[tuple[int, int], int] = {}
        for n, (x, y) in enumerate(centres, 1):
            if not (0 <= x < nx and 0 <= y < ny):
                raise ValueError(f"{table}: data row {n} is at x {x:g} y {y:g}, outside the {nx}x{ny} micrograph "
                                 f"{micrograph}")
            # coordinates are pixel indices: column floor(x); row floor(y) from the first stored row (down) or
            # from the last one (up)
            at = (ny - 1 - math.floor(y) if y_axis == "up" else math.floor(y)), math.floor(x)
            if at in seen:
                raise ValueError(f"{table}: data rows {seen[at]} and {n} are the same pixel (x {x:g} y {y:g}); "
                                 "one of the two particles would be lost")
            seen[at] = n
            self._pixels.append(at)

    def _paint(self) -> np.ndarray:
        """The label plane: id k at the centre pixel of particle k; with a radius, at every pixel whose distance
        to that centre pixel is <= radius, a pixel in reach of several going to the nearest (lowest id on a tie)."""
        _, ny, nx = self.shape
        out = np.zeros((ny, nx), self.dtype)
        r = self._radius
        if r == 0:
            for k, (row, col) in enumerate(self._pixels, 1):
                out[row, col] = k
            return out
        nearest = np.full((ny, nx), np.iinfo(np.int32).max, np.int32)      # squared distance to the id's centre
        for k, (row, col) in enumerate(self._pixels, 1):
            ys, xs = slice(max(row - r, 0), min(row + r + 1, ny)), slice(max(col - r, 0), min(col + r + 1, nx))
            yy, xx = np.ogrid[ys, xs]
            d = ((yy - row) ** 2 + (xx - col) ** 2).astype(np.int32)
            take = (d <= r * r) & (d < nearest[ys, xs])
            nearest[ys, xs][take] = d[take]
            out[ys, xs][take] = k
        return out

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        z, y, x = (region.get(a, slice(None)) for a in "zyx")
        if self._plane is None:
            self._plane = self._paint()
        part = np.array(self._plane[y, x])
        if isinstance(z, slice):
            return np.stack([part] * len(range(1)[z])) if range(1)[z] else np.zeros((0,) + part.shape, self.dtype)
        range(1)[z]                 # IndexError when out of range, like numpy
        return part


def open_array(src: Source, io: IO) -> Array:
    y_axis, radius = src.params.get("y_axis"), src.params.get("radius", 0)
    if y_axis not in Y_AXES:
        raise ValueError(f"params.y_axis must be one of {Y_AXES}, got {y_axis!r}")
    if isinstance(radius, bool) or not isinstance(radius, int) or not 0 <= radius <= RADIUS_MAX:
        raise ValueError(f"params.radius must be a whole number of pixels, 0..{RADIUS_MAX}, got {radius!r}")
    if len(src.urls) != 2:
        raise ValueError(f"csv-points-mrc-grid reads two urls, the coordinate table and then the micrograph it "
                         f"is on, got {len(src.urls)}")
    return _Points(io, src.urls[0], src.urls[1], y_axis, radius)
