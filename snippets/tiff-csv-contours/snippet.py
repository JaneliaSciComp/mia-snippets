"""One single-page grayscale TIFF at a plain URL as yx; or, with a contour CSV beside it, the CSV's closed
polygons (one per contour_id, vertices in pixels) filled onto that TIFF's grid as yx uint16 instance ids."""
import csv
import math
import operator

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

HEADER = ["contour_id", "x [pix]", "y [pix]"]       # the only table read: one row per polygon vertex
ORIGINS = ("top-left", "bottom-left")


class _Tiff:
    """The one page of a 2D single-sample TIFF. Nothing but the header and the IFD is read until `read`."""

    def __init__(self, tif: tifffile.TiffFile, access: str, url: str):
        self.tif, self.access = tif, access
        s = tif.series[0]
        kept = [(a, n) for a, n in zip(s.axes, s.shape) if n > 1 or a in "YX"]
        if len(tif.series) != 1 or len(s.pages) != 1 or "".join(a for a, _ in kept) != "YX":
            raise ValueError(f"{url}: {len(tif.series)} series, the first with {len(s.pages)} pages, axes "
                             f"{s.axes!r} and shape {tuple(s.shape)}; only one single-page, one-sample-per-pixel "
                             "(yx) image is read")
        self.axes, self.shape, self.dtype = "yx", tuple(int(n) for _, n in kept), np.dtype(s.dtype)
        self.voxel_size_nm = None       # a plain TIFF's resolution tags are not a pixel size anyone measured
        self._plane: np.ndarray | None = None

    def __del__(self):          # the harness never closes arrays; a staged file's handle goes with the array
        self.tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:         # the page is decoded once: a read of any region needs all of it
            self._plane = np.asarray(self.tif.asarray(key=0, series=0)).reshape(self.shape)
        return np.array(self._plane[region.get("y", slice(None))][..., region.get("x", slice(None))])


def _number(text: str, what: str, line: int) -> float:
    try:
        v = float(text)
    except ValueError:
        v = math.nan
    if not math.isfinite(v):
        raise ValueError(f"contour CSV line {line}: {what} {text!r} is not a finite number")
    return v


def _polygons(text: str, name: str) -> list[tuple[int, np.ndarray, np.ndarray]]:
    """The table as (contour_id, xs, ys) per polygon, in file order."""
    rows = [(n, r) for n, r in enumerate(csv.reader(text.splitlines()), 1) if r]
    if not rows or [c.strip().lower() for c in rows[0][1]] != HEADER:
        raise ValueError(f"{name}: the first line is {rows[0][1] if rows else 'missing'}, not the columns {HEADER}")
    out: dict[int, tuple[list[float], list[float]]] = {}
    last = None
    for line, r in rows[1:]:
        if len(r) != 3:
            raise ValueError(f"{name} line {line}: {len(r)} fields, not 3")
        v = _number(r[0], "contour_id", line)
        if v != int(v) or not 1 <= v <= 65535:
            raise ValueError(f"{name} line {line}: contour_id {r[0]!r} is not an integer in 1..65535 (uint16 ids, "
                             "0 is 'no contour')")
        cid = int(v)
        if cid != last and cid in out:      # two runs of one id: two polygons or one? the table does not say
            raise ValueError(f"{name} line {line}: the rows of contour_id {cid} are not consecutive")
        last = cid
        xs, ys = out.setdefault(cid, ([], []))
        xs.append(_number(r[1], "x", line))
        ys.append(_number(r[2], "y", line))
    for cid, (xs, _) in out.items():
        if len(xs) < 3:
            raise ValueError(f"{name}: contour_id {cid} has {len(xs)} vertices, which enclose nothing")
    return [(cid, np.array(xs), np.array(ys)) for cid, (xs, ys) in out.items()]


def _fill(cols: np.ndarray, rows: np.ndarray, shape: tuple[int, int]) -> tuple[int, int, np.ndarray] | None:
    """A closed polygon, its vertices in pixel-index units (a pixel's centre is at its integer row and column),
    as (row, column, mask): the pixels of the grid whose centres it encloses by the even-odd rule, with the grid
    position of mask[0, 0]. None when it encloses no pixel centre of the grid."""
    r0, r1 = max(math.ceil(rows.min()), 0), min(math.floor(rows.max()) + 1, shape[0])
    c0, c1 = max(math.ceil(cols.min()), 0), min(math.floor(cols.max()) + 1, shape[1])
    if r0 >= r1 or c0 >= c1:
        return None
    py, px = np.arange(r0, r1, dtype="f8")[:, None], np.arange(c0, c1, dtype="f8")[None, :]
    mask = np.zeros((r1 - r0, c1 - c0), bool)
    for xa, ya, xb, yb in zip(cols, rows, np.roll(cols, -1), np.roll(rows, -1)):    # the last vertex joins the first
        if ya != yb:        # the edge crosses the rows between its ends; pixels left of the crossing toggle
            mask ^= ((ya > py) != (yb > py)) & (px < xa + (xb - xa) * (py - ya) / (yb - ya))
    return (r0, c0, mask) if mask.any() else None


def _span(sel: int | slice, n: int) -> tuple[int, int, int | np.ndarray]:
    """A selection on an axis of n pixels as the run [lo, hi) that holds it, and the selection within that run."""
    if isinstance(sel, slice):
        idx = np.arange(n)[sel]
        if idx.size == 0:
            return 0, 0, idx
        lo = int(idx.min())
        return lo, int(idx.max()) + 1, idx - lo
    k = operator.index(sel)
    k = k + n if k < 0 else k
    if not 0 <= k < n:
        raise IndexError(f"index {sel} is out of range for an axis of {n}")
    return k, k + 1, 0


class _Contours:
    """The polygons of a contour CSV on a TIFF's grid: pixel (y, x) holds the contour_id of the polygon that
    encloses its centre, 0 where none does."""

    def __init__(self, io: IO, url: str, grid: _Tiff, origin: str):
        self._io, self._url, self._grid, self._origin = io, url, grid, origin    # the grid keeps its file open
        self._masks: list[tuple[int, int, int, np.ndarray]] | None = None
        self.axes, self.shape, self.dtype = "yx", grid.shape, np.dtype("uint16")
        self.voxel_size_nm = grid.voxel_size_nm
        self.access = "plane"           # the whole CSV is read on the first read, whatever the region

    def _load(self) -> list[tuple[int, int, int, np.ndarray]]:
        try:
            with self._io.open(self._url) as fh:
                b = fh.read()
        except NeedsFetch:
            b = self._io.fetch(self._url).read_bytes()
        masks = []
        for cid, xs, ys in _polygons(b.decode("utf-8-sig"), self._url.rsplit("/", 1)[-1]):
            rows = ys if self._origin == "top-left" else self.shape[0] - 1 - ys
            m = _fill(xs, rows, self.shape)
            if m is not None:
                masks.append((cid, *m))
        # the largest first, so that where polygons overlap the smaller one (a vesicle inside another) is kept
        return sorted(masks, key=lambda m: -int(m[3].sum()))

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._masks is None:
            self._masks = self._load()
        y0, y1, yi = _span(region.get("y", slice(None)), self.shape[0])
        x0, x1, xi = _span(region.get("x", slice(None)), self.shape[1])
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for cid, my, mx, mask in self._masks:
            a0, a1 = max(y0, my), min(y1, my + mask.shape[0])
            b0, b1 = max(x0, mx), min(x1, mx + mask.shape[1])
            if a0 < a1 and b0 < b1:
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][mask[a0 - my:a1 - my, b0 - mx:b1 - mx]] = cid
        return np.array(out[yi][..., xi])


def _open(io: IO, url: str) -> _Tiff:
    try:
        return _Tiff(tifffile.TiffFile(io.open(url)), "plane", url)
    except NeedsFetch:          # a server without Range: stage the whole file
        return _Tiff(tifffile.TiffFile(io.fetch(url)), "file", url)


def open_array(src: Source, io: IO) -> Array:
    tables = [u for u in src.urls if u.split("?", 1)[0].split("#", 1)[0].lower().endswith(".csv")]
    images = [u for u in src.urls if u not in tables]
    if len(images) != 1 or len(tables) > 1:
        raise ValueError(f"urls must name one TIFF, and for a label one .csv beside it; got {len(images)} "
                         f"image and {len(tables)} .csv urls")
    origin = src.params.get("origin")
    if tables and origin not in ORIGINS:
        raise ValueError(f"params.origin must be one of {ORIGINS} for a contour CSV, got {origin!r}: the table "
                         "does not say from which image edge y is measured")
    if not tables and origin is not None:
        raise ValueError("params.origin is for the label form (urls with a .csv); got one image only")
    tiff = _open(io, images[0])
    return _Contours(io, tables[0], tiff, origin) if tables else tiff
