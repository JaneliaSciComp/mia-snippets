"""The outlines of one ROI JSON file, a bare list of `{"type": ..., "points": [[x, y], ...]}` entries (ImageJ
ROIs exported as JSON), filled as instance ids on the pixel grid of the TIFF they were drawn on.

The urls are the JSON and its TIFF. The JSON holds coordinates only, so the grid is the ImageLength x ImageWidth
of the TIFF's first page; none of the TIFF's pixels are decoded. An entry is filled with its 1-based position in
the list, over the pixels whose centres lie inside its outline (even-odd rule), larger outlines first so that the
smaller of two that overlap stays on top. The closed types (`polygon`, `freehand`, `traced`) are always filled;
an open type (`polyline`, `freeline`) is an error unless `params.open_outlines` says to close and fill it or to
leave it out. `open_array` reads the TIFF header and parses the JSON; `read` fills only the region asked for.
See README.md."""
import json
from pathlib import Path
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

CLOSED = ("polygon", "freehand", "traced")      # ImageJ's point-list ROI types that enclose an area
OPEN = ("polyline", "freeline")                 # its point-list types that are a line: no area until closed
OPEN_MODES = ("close", "skip")
OUTSIDE = 16.0                  # pixels an outline may reach beyond the grid before the pairing is refused
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form

Outline = tuple[int, np.ndarray]    # id (1-based position in the file), points as (N, 2) float64 of x, y


def _name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _all_bytes(io: IO, url: str) -> bytes:
    try:
        src: BinaryIO | Path = io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        src = io.fetch(url)
    if isinstance(src, Path):
        return src.read_bytes()
    with src:
        src.seek(0)
        return src.read()


def _outlines(data: bytes, url: str, open_mode: str | None) -> list[Outline]:
    """The entries of the file that are filled, in file order, each with its 1-based position among all entries."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except ValueError as e:
        raise ValueError(f"{url}: not a JSON file ({e}): header={data[:4]!r}") from None
    if not isinstance(doc, list):
        raise ValueError(f"{url}: a JSON {type(doc).__name__}, not the bare list of "
                         '{"type": ..., "points": [[x, y], ...]} entries this snippet reads')
    if len(doc) > 65535:
        raise ValueError(f"{url}: {len(doc)} entries exceed uint16 ids")
    out, opened = [], []
    for n, entry in enumerate(doc, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
            raise ValueError(f"{url}: entry {n} is not an object with a type")
        kind = entry["type"]
        if kind not in CLOSED + OPEN:
            raise ValueError(f"{url}: entry {n} is a {kind!r}; only the point-list types {CLOSED + OPEN} are read")
        try:
            pts = np.asarray(entry.get("points"), dtype=np.float64)
        except (TypeError, ValueError):
            pts = np.zeros(0)
        if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 3 or not np.isfinite(pts).all():
            raise ValueError(f"{url}: entry {n} ({kind!r}) is not a list of 3 or more finite [x, y] points")
        if kind in OPEN:
            opened.append(n)
            if open_mode == "skip":
                continue
        out.append((n, pts))
    if opened and open_mode is None:
        raise ValueError(f"{url}: {len(opened)} of its {len(doc)} entries are open outlines {OPEN} (the first is "
                         f"entry {opened[0]}), which enclose nothing as they stand. Set params.open_outlines to "
                         "'close' (join each one's last point to its first and fill it) or 'skip' (leave them "
                         "out), with the reason in the binding")
    return out


def _area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2


def _inside(pts: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """bool (y1 - y0, x1 - x0): the pixels of rows [y0, y1) and columns [x0, x1) whose centres (x + 0.5, y + 0.5)
    lie inside the outline, closed from its last point to its first, by the even-odd rule."""
    xa, ya = pts[:, 0], pts[:, 1]
    xb, yb = np.roll(xa, -1), np.roll(ya, -1)
    # an edge crosses the centre line of row r when min(ya, yb) <= r + 0.5 < max(ya, yb); a horizontal one never
    lo = np.clip(np.ceil(np.minimum(ya, yb) - 0.5), y0, y1).astype(np.int64)
    hi = np.clip(np.ceil(np.maximum(ya, yb) - 0.5), y0, y1).astype(np.int64)
    count = hi - lo
    edge = np.repeat(np.arange(len(pts)), count)                        # the edge of each crossing
    row = np.arange(int(count.sum())) - np.repeat(np.cumsum(count) - count, count) + lo[edge]      # and its row
    cross = xa[edge] + (row + 0.5 - ya[edge]) * (xb[edge] - xa[edge]) / (yb[edge] - ya[edge])
    first = np.clip(np.ceil(cross - 0.5), x0, x1).astype(np.int64) - x0  # first pixel centre at or right of it
    toggles = np.zeros((y1 - y0, x1 - x0 + 1), np.uint8)                # parity survives uint8 wrap-around
    np.add.at(toggles, (row - y0, first), 1)
    return (np.cumsum(toggles[:, :-1], axis=1, dtype=np.uint8) & 1).astype(bool)


def _window(key: int | slice, n: int) -> tuple[int, int, int | slice]:
    """An index or slice along an axis of length n as the pixels [lo, hi) it spans and the key within them."""
    if not isinstance(key, slice):
        i = range(n)[key]                   # IndexError when out of range, like numpy
        return i, i + 1, 0
    r = range(n)[key]
    if len(r) == 0:
        return 0, 0, slice(0, 0)
    return min(r[0], r[-1]), max(r[0], r[-1]) + 1, slice(None, None, r.step)


class _Outlines:
    """yx uint16: pixel = the 1-based position in the file of the outline that encloses the pixel's centre (the
    smaller one where two overlap), 0 where none does."""

    def __init__(self, outlines: list[Outline], shape: tuple[int, int], voxel_size_nm: dict[str, float] | None):
        # largest first, so the smaller of two that overlap is painted last; file order breaks ties
        self._outlines = sorted(outlines, key=lambda o: -_area(o[1]))
        self.axes, self.shape = "yx", shape
        self.dtype = np.dtype("uint16")
        self.voxel_size_nm = voxel_size_nm
        self.access = "plane"           # any region costs the whole JSON, which open_array has already parsed

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y0, y1, yk = _window(region.get("y", slice(None)), self.shape[0])
        x0, x1, xk = _window(region.get("x", slice(None)), self.shape[1])
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for number, pts in self._outlines:
            (px0, py0), (px1, py1) = pts.min(axis=0), pts.max(axis=0)
            by0, by1 = int(np.clip(np.floor(py0), y0, y1)), int(np.clip(np.ceil(py1), y0, y1))
            bx0, bx1 = int(np.clip(np.floor(px0), x0, x1)), int(np.clip(np.ceil(px1), x0, x1))
            if by0 >= by1 or bx0 >= bx1:
                continue                # the outline does not reach the asked region
            out[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0][_inside(pts, by0, by1, bx0, bx1)] = number
        return np.array(out[yk, xk])


def _grid(io: IO, url: str) -> tuple[tuple[int, int], dict[str, float] | None]:
    """(Y, X) of the TIFF's first page and its pixel size: OME PhysicalSize*, else ImageJ unit + resolution."""
    try:
        src: BinaryIO | Path = io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        src = io.fetch(url)
    try:
        with tifffile.TiffFile(src) as tif:
            page, out = tif.pages[0], {}
            if tif.is_ome:
                image = tifffile.xml2dict(tif.ome_metadata)["OME"]["Image"]
                px = (image[0] if isinstance(image, list) else image)["Pixels"]
                for a in "yx":
                    v, unit = px.get(f"PhysicalSize{a.upper()}"), px.get(f"PhysicalSize{a.upper()}Unit", "µm")
                    if v and unit in NM:
                        out[a] = float(v) * NM[unit]
            elif tif.is_imagej and tif.imagej_metadata.get("unit") in NM:
                for a, tag in (("y", "YResolution"), ("x", "XResolution")):
                    num, den = page.tags[tag].value if tag in page.tags else (0, 0)
                    if num:
                        out[a] = den / num * NM[tif.imagej_metadata["unit"]]
            return (int(page.imagelength), int(page.imagewidth)), out or None
    except tifffile.TiffFileError as e:
        raise ValueError(f"{url}: not a TIFF ({e}); urls are one ROI .json and the TIFF it was drawn on") from None
    finally:
        if not isinstance(src, Path):
            src.close()


def open_array(src: Source, io: IO) -> Array:
    open_mode = src.params.get("open_outlines")
    if open_mode is not None and open_mode not in OPEN_MODES:
        raise ValueError(f"params.open_outlines must be one of {OPEN_MODES}, got {open_mode!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    jsons = [u for u in urls if u.split("?", 1)[0].split("#", 1)[0].lower().endswith(".json")]
    rest = [u for u in urls if u not in jsons]
    if len(jsons) != 1 or len(rest) != 1:
        raise ValueError("urls must be one ROI .json and the one TIFF it was drawn on, got "
                         f"{len(jsons)} .json and {len(rest)} other")
    (ny, nx), voxel_size_nm = _grid(io, rest[0])
    outlines = _outlines(_all_bytes(io, jsons[0]), jsons[0], open_mode)
    if outlines:
        lo = np.min([pts.min(axis=0) for _, pts in outlines], axis=0)
        hi = np.max([pts.max(axis=0) for _, pts in outlines], axis=0)
        if lo[0] < -OUTSIDE or lo[1] < -OUTSIDE or hi[0] > nx + OUTSIDE or hi[1] > ny + OUTSIDE:
            raise ValueError(f"{_name(jsons[0])}: the outlines span x {lo[0]:g}..{hi[0]:g}, y {lo[1]:g}..{hi[1]:g}, "
                             f"beyond the {nx} x {ny} pixels (x by y) of {_name(rest[0])}: not its pixel frame")
    return _Outlines(outlines, (ny, nx), voxel_size_nm)
