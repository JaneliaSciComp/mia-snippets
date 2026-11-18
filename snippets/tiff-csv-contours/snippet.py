"""One single-plane, single-sample TIFF at a plain URL as yx; or, given that TIFF and a CSV of contour vertices
(columns: contour id, x, y in pixels of that image), each contour's closed polygon filled onto the TIFF's pixel
grid as instance ids (yx uint16, id = the CSV's contour id). `y_up` is for a CSV whose y runs upward from the
image's bottom row.

The CSV states no image size, so the label form reads the TIFF's header for the grid (never its pixels). An
uncompressed TIFF is read by byte range, only the rows a read asks for.

Every step that makes requests (reading a TIFF's header, its rows, the CSV) is tried again, after waits of
WAITS seconds, when the harness's client gives up on it in transit (a timeout, a dropped connection, a 5xx or
429 answer): a host can be out for longer than the client's own tries last, and a crop job opens every array of
a binding before it makes its first crop, so one such request would otherwise end the job with nothing. What
was fetched before the failure is in the job's block cache and is not asked for again. See README.md."""
import csv
import math
import re
import time
from pathlib import Path
from typing import BinaryIO, Callable, TypeVar

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form
PIXEL_UNITS = ("pix", "px", "pixel", "pixels")
COLUMN = re.compile(r"\s*([xy])\s*(?:[\[(]\s*([^\])]*?)\s*[\])])?\s*", re.I)       # "x", "x [pix]", "Y (px)"
WAITS = (10.0, 30.0, 90.0)      # seconds before each further try of a step whose requests failed in transit
T = TypeVar("T")


def _transient(e: Exception) -> bool:
    """A failure worth asking again for: the HTTP client's transport errors (timeouts, a connection dropped or
    cut inside a body) and 5xx/429 answers. Told by class name, since a snippet may not import the client."""
    names = {c.__name__ for c in type(e).__mro__}
    status = getattr(getattr(e, "response", None), "status_code", None)
    return "TransportError" in names or ("HTTPStatusError" in names and isinstance(status, int)
                                         and (status >= 500 or status == 429))


def _retry(step: Callable[[], T]) -> T:
    """`step()`, and again after each of WAITS while it fails in transit; any other error is raised at once."""
    for wait in (*WAITS, None):
        try:
            return step()
        except Exception as e:      # BudgetExceeded, HostNotAllowed, NeedsFetch, ValueError: not transient
            if wait is None or not _transient(e):
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


def _span(key: int | slice, n: int) -> tuple[int, int, int | np.ndarray]:
    """What `key` asks of an axis of length n: (lo, hi, index into [lo, hi)); an int index drops the axis."""
    if isinstance(key, slice):
        idx = np.arange(n)[key]
        if idx.size == 0:
            return 0, 0, idx
        lo = int(idx.min())
        return lo, int(idx.max()) + 1, idx - lo
    k = int(key) + (n if key < 0 else 0)
    if not 0 <= k < n:
        raise IndexError(f"index {key} is outside an axis of length {n}")
    return k, k + 1, 0


class _Tiff:
    """The one yx plane of a TIFF. Uncompressed pixel data stored in row order is read by byte range (access
    chunk); anything else is decoded whole by tifffile (access plane, or file when it had to be staged)."""

    def __init__(self, source: BinaryIO | Path, ranged: bool, url: str):
        self._source, self.tif = source, None       # set first: __del__ runs also when the file is refused below
        magic = _magic(source)
        if magic[:2] not in (b"II", b"MM"):
            raise ValueError(f"{url} starts with {magic!r}: not a TIFF")
        self.tif = tif = tifffile.TiffFile(source)
        s = tif.series[0]
        kept = [(a, n) for a, n in zip(s.axes, s.shape) if n > 1 or a in "YX"]
        if [a for a, _ in kept] != ["Y", "X"] or len(tif.series) != 1:
            raise ValueError(f"{url}: {len(tif.series)} series, the first with axes {s.axes!r} {tuple(s.shape)}; "
                             "this snippet reads a TIFF that is one yx plane of one sample per pixel")
        self.axes, self.shape, self.dtype = "yx", (kept[0][1], kept[1][1]), np.dtype(s.dtype)
        self.voxel_size_nm = _voxel_size(tif)
        self._start = _row_data(tif.pages[0], self.shape, self.dtype)
        self.access = ("chunk" if self._start is not None else "plane") if ranged else "file"

    def __del__(self):          # the harness never closes arrays; the handles go with the array
        if self.tif is not None:
            self.tif.close()
        if not isinstance(self._source, Path):      # tifffile leaves a handle it was given open
            self._source.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        if self._start is None:
            rows = _retry(lambda: self.tif.asarray(series=0)).reshape(self.shape)[y0:y1]
        else:
            stored = self.dtype.newbyteorder(self.tif.byteorder)
            fh, n = self.tif.filehandle, (y1 - y0) * nx * stored.itemsize
            at = self._start + y0 * nx * stored.itemsize
            data = _retry(lambda: (fh.seek(at), fh.read(n))[1])
            if len(data) != n:
                raise ValueError(f"TIFF rows {y0}..{y1} read back short ({len(data)} of {n} bytes)")
            rows = np.frombuffer(data, stored).reshape(y1 - y0, nx).astype(self.dtype, copy=False)
        return np.array(rows[:, x0:x1][yi][..., xi])


def _open_tiff(io: IO, url: str) -> _Tiff:
    """The TIFF at url, its header read: by byte range, or staged whole from a server without Range."""
    try:
        return _retry(lambda: _Tiff(io.open(url), True, url))
    except NeedsFetch:
        return _retry(lambda: _Tiff(io.fetch(url), False, url))


def _magic(source: BinaryIO | Path) -> bytes:
    """The first 4 bytes of an open file (left at its start) or of a staged one."""
    if isinstance(source, Path):
        with open(source, "rb") as fh:
            return fh.read(4)
    magic = source.read(4)
    source.seek(0)
    return magic


def _row_data(page: tifffile.TiffPage, shape: tuple[int, int], dtype: np.dtype) -> int | None:
    """The file offset of pixel (0, 0) when the page's pixels lie in the file uncompressed, row after row with
    nothing between its strips; None when they have to be decoded."""
    if (page.compression != 1 or page.predictor != 1 or page.fillorder != 1 or page.is_tiled
            or page.samplesperpixel != 1 or page.bitspersample != dtype.itemsize * 8):
        return None
    offsets, counts = list(page.dataoffsets), list(page.databytecounts)
    if not offsets or len(offsets) != len(counts) or sum(counts) != shape[0] * shape[1] * dtype.itemsize:
        return None
    if any(o + c != nxt for o, c, nxt in zip(offsets, counts, offsets[1:])):
        return None
    return int(offsets[0])


def _voxel_size(tif: tifffile.TiffFile) -> dict[str, float] | None:
    """From OME PhysicalSize* or ImageJ unit + X/YResolution; None when the file doesn't say."""
    out = {}
    if tif.is_ome:
        img = tifffile.xml2dict(tif.ome_metadata)["OME"]["Image"]
        px = (img[0] if isinstance(img, list) else img)["Pixels"]
        for a in "yx":
            v, unit = px.get(f"PhysicalSize{a.upper()}"), px.get(f"PhysicalSize{a.upper()}Unit", "µm")
            if v and unit in NM:
                out[a] = float(v) * NM[unit]
    elif tif.is_imagej and tif.imagej_metadata.get("unit") in NM:
        scale, tags = NM[tif.imagej_metadata["unit"]], tif.pages[0].tags
        for a, tag in (("y", "YResolution"), ("x", "XResolution")):
            num, den = tags[tag].value if tag in tags else (0, 0)
            if num:
                out[a] = den / num * scale
    return out or None


def _contours(text: str, name: str) -> list[tuple[int, np.ndarray, np.ndarray]]:
    """The CSV's contours as (id, xs, ys), in file order. A header names the id, x and y columns."""
    rows = [r for r in csv.reader(text.splitlines()) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"{name}: empty file, not even a header row")
    found = {}
    for i, cell in enumerate(rows[0]):
        m = COLUMN.fullmatch(cell)
        if m:
            axis, unit = m.group(1).lower(), (m.group(2) or "").lower()
            if axis in found or (unit and unit not in PIXEL_UNITS):
                raise ValueError(f"{name}: header {rows[0]!r} has two {axis} columns or states a unit other than "
                                 "pixels; this snippet reads one x and one y column in pixels")
            found[axis] = i
    rest = [i for i in range(len(rows[0])) if i not in found.values()]
    if len(rows[0]) != 3 or set(found) != {"x", "y"} or len(rest) != 1:
        raise ValueError(f"{name}: header {rows[0]!r} is not three columns: a contour id, x and y")
    out: list[tuple[int, list[float], list[float]]] = []
    seen = set()
    for n, r in enumerate(rows[1:], 2):
        try:
            if len(r) != 3:
                raise ValueError
            cid, x, y = float(r[rest[0]]), float(r[found["x"]]), float(r[found["y"]])
            if not (cid == int(cid) and 1 <= cid <= 65535 and math.isfinite(x) and math.isfinite(y)):
                raise ValueError
        except (ValueError, OverflowError):
            raise ValueError(f"{name}: row {n} {r!r} is not an integer contour id in 1..65535 with a finite x "
                             "and y") from None
        if not out or out[-1][0] != int(cid):
            if int(cid) in seen:
                raise ValueError(f"{name}: row {n} returns to contour id {int(cid)} after other contours; the "
                                 "rows of a contour must be together, in order along it")
            seen.add(int(cid))
            out.append((int(cid), [], []))
        out[-1][1].append(x)
        out[-1][2].append(y)
    return [(cid, np.array(xs), np.array(ys)) for cid, xs, ys in out]


def _fill(xs: np.ndarray, ys: np.ndarray, shape: tuple[int, int]) -> tuple[int, int, np.ndarray]:
    """The pixels of a `shape` grid whose (row, column) lies inside the closed polygon (even-odd rule; an edge
    runs from each vertex to the next and from the last to the first), as (y, x, mask) with mask[0, 0] at (y, x)."""
    y0, y1 = max(0, math.ceil(ys.min())), min(shape[0], math.floor(ys.max()) + 1)
    x0, x1 = max(0, math.ceil(xs.min())), min(shape[1], math.floor(xs.max()) + 1)
    py, px = np.mgrid[y0:max(y0, y1), x0:max(x0, x1)]
    mask = np.zeros(py.shape, bool)
    for xa, ya, xb, yb in zip(xs, ys, np.roll(xs, -1), np.roll(ys, -1)):
        if ya != yb:
            mask ^= ((ya > py) != (yb > py)) & (px < xa + (xb - xa) * (py - ya) / (yb - ya))
    return y0, x0, mask


class _Contours:
    """Contour polygons on a TIFF's grid: pixel (y, x) holds the id of the contour that encloses it, 0 where
    none does. Where contours overlap, the one enclosing the smaller area is kept. With `y_up` the CSV's y runs
    upward from the image's bottom row instead of down from its top row."""

    def __init__(self, io: IO, url: str, grid: _Tiff, y_up: bool):
        self._io, self._url, self._grid, self._y_up = io, url, grid, y_up
        self._masks: list[tuple[int, int, int, np.ndarray]] | None = None
        self.axes, self.shape, self.dtype = "yx", grid.shape, np.dtype("uint16")
        self.voxel_size_nm = grid.voxel_size_nm
        self.access = "plane"           # the whole CSV is read on the first read, whatever the region

    def _load(self) -> list[tuple[int, int, int, np.ndarray]]:
        name = self._url.rsplit("/", 1)[-1]

        def ranged() -> bytes:
            with self._io.open(self._url) as fh:
                return fh.read()

        try:
            raw = _retry(ranged)
        except NeedsFetch:
            raw = _retry(lambda: self._io.fetch(self._url).read_bytes())
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError(f"{name}: not a text file (starts with {raw[:8]!r})") from None
        masks = []
        for cid, xs, ys in _contours(text, name):
            if self._y_up:              # y counted upward from the bottom row: row = height - 1 - y
                ys = self.shape[0] - 1 - ys
            if len(xs) < 3:
                raise ValueError(f"{name}: contour {cid} has {len(xs)} vertices, which enclose nothing")
            if xs.max() < 0 or ys.max() < 0 or xs.min() > self.shape[1] - 1 or ys.min() > self.shape[0] - 1:
                raise ValueError(f"{name}: contour {cid} (x {xs.min():g}..{xs.max():g}, y {ys.min():g}.."
                                 f"{ys.max():g}) lies outside the {self.shape[0]}x{self.shape[1]} (y, x) grid of "
                                 "the TIFF; is this the image the contours were drawn on?")
            y, x, mask = _fill(xs, ys, self.shape)
            masks.append((int(mask.sum()), cid, y, x, mask))
        masks.sort(key=lambda m: (-m[0], m[1]))                 # largest first: a contour inside another stays
        return [m[1:] for m in masks]

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._masks is None:
            self._masks = self._load()
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for cid, my, mx, mask in self._masks:
            a0, a1 = max(y0, my), min(y1, my + mask.shape[0])
            b0, b1 = max(x0, mx), min(x1, mx + mask.shape[1])
            if a0 < a1 and b0 < b1:
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][mask[a0 - my:a1 - my, b0 - mx:b1 - mx]] = cid
        return np.array(out[yi][..., xi])


def open_array(src: Source, io: IO) -> Array:
    y_up = src.params.get("y_up", False)
    if not isinstance(y_up, bool):
        raise ValueError(f"params.y_up must be true or false, got {y_up!r}")
    csvs = [u for u in src.urls if u.split("?", 1)[0].split("#", 1)[0].lower().endswith(".csv")]
    rest = [u for u in src.urls if u not in csvs]
    if len(rest) != 1 or len(csvs) > 1:
        raise ValueError(f"urls must name one TIFF (raw) or one TIFF and its one contour .csv (label), got "
                         f"{len(rest)} urls that are not .csv and {len(csvs)} that are")
    if not csvs and "y_up" in src.params:
        raise ValueError("params.y_up is for the label form (urls with a .csv); got one TIFF only")
    grid = _open_tiff(io, rest[0])
    if not csvs:
        return grid
    return _Contours(io, csvs[0], grid, y_up)
