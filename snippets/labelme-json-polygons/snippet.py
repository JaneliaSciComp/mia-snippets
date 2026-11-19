"""LabelMe JSON annotations (one `<stem>.json` per image, as the labelme tool saves them), their polygons filled
on the image grid the file declares; chosen by glob from zip members or an HTTP directory (or listed one by one),
one file per z plane.

A file's grid is its `imageHeight` x `imageWidth`; each `shapes[]` polygon is filled over the pixels whose
centres lie inside it, by its 1-based number in the file (`ids: instance`, the default) or by the 1-based index
of its label in `classes` (`ids: semantic`). Polygons are painted largest first, so a polygon drawn inside
another (an annual ring outline inside the next year's) stays visible. `open_array` reads and parses the first
file (the JSON has no header: the image size follows the embedded image) and paints nothing; `read` paints only
the region it is asked for.
"""
import json
from pathlib import Path
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

ID_MODES = ("instance", "semantic")

Shape = tuple[str, np.ndarray]              # label, points as (N, 2) float64 of x, y in image pixel coordinates


def _all_bytes(io: IO, url: str) -> bytes:
    try:
        src: BinaryIO | Path = io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        src = io.fetch(url)
    if isinstance(src, Path):
        return src.read_bytes()
    src.seek(0)
    return src.read()


def _size(doc: dict, key: str, url: str) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{url}: {key} is {value!r}, not an image size in pixels")
    return value


def _labelme(data: bytes, mode: str, url: str) -> tuple[tuple[int, int], list[Shape]]:
    """(Y, X) from imageHeight/imageWidth and the polygons of a LabelMe file, in file order."""
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise ValueError(f"{url}: not a JSON file ({e}): header={data[:4]!r}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("shapes"), list):
        raise ValueError(f"{url}: a JSON file without a shapes list, not a LabelMe annotation")
    ny, nx = _size(doc, "imageHeight", url), _size(doc, "imageWidth", url)
    shapes = []
    for n, s in enumerate(doc["shapes"], 1):
        if not isinstance(s, dict) or not isinstance(s.get("label"), str):
            raise ValueError(f"{url}: shape {n} is not an object with a label")
        label, kind = s["label"], s.get("shape_type", "polygon")    # files older than shape_type hold polygons
        if kind != "polygon":
            raise ValueError(f"{url}: shape {n} ({label!r}) is a {kind!r}; this snippet fills polygons only")
        if mode == "instance" and s.get("group_id") is not None:
            raise ValueError(f"{url}: shape {n} ({label!r}) has group_id {s['group_id']!r}: shapes grouped into "
                             "one instance are not read with ids: instance")
        try:
            pts = np.asarray(s.get("points"), dtype=np.float64)
        except (TypeError, ValueError):
            pts = np.zeros(0)
        if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 3 or not np.isfinite(pts).all():
            raise ValueError(f"{url}: shape {n} ({label!r}) is not a polygon of 3 or more finite x, y points")
        shapes.append((label, pts))
    if len(shapes) > 65535:
        raise ValueError(f"{url}: {len(shapes)} shapes exceed uint16 ids")
    return (ny, nx), shapes


def _area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2


def _inside(pts: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """bool (y1 - y0, x1 - x0): the pixels of rows y0..y1 and columns x0..x1 whose centres (x + 0.5, y + 0.5) lie
    inside the closed polygon, by the even-odd rule. A centre exactly on an edge is inside when the polygon is
    on its right (or below it)."""
    xa, ya = pts[:, 0], pts[:, 1]
    xb, yb = np.roll(xa, -1), np.roll(ya, -1)
    # an edge crosses the centre line of row r when min(ya, yb) <= r + 0.5 < max(ya, yb); a horizontal one never
    lo = np.clip(np.ceil(np.minimum(ya, yb) - 0.5), y0, y1).astype(np.int64)
    hi = np.clip(np.ceil(np.maximum(ya, yb) - 0.5), y0, y1).astype(np.int64)
    count = hi - lo
    e = np.repeat(np.arange(len(pts)), count)                           # the edge of each crossing
    r = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count) + lo[e]     # and its row
    xc = xa[e] + (r + 0.5 - ya[e]) * (xb[e] - xa[e]) / (yb[e] - ya[e])
    first = np.clip(np.ceil(xc - 0.5), x0, x1).astype(np.int64) - x0    # first pixel centre at or right of it
    toggles = np.zeros((y1 - y0, x1 - x0 + 1), np.uint8)                # parity survives uint8 wrap-around
    np.add.at(toggles, (r - y0, first), 1)
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


class _Planes:
    """One LabelMe file per plane: (Y, X) uint16 per file, axes [z]yx."""

    def __init__(self, io: IO, urls: list[str], mode: str, classes: list[str] | None):
        self._io, self._urls, self._mode, self._classes = io, urls, mode, classes
        self._last: tuple[int, list[Shape]] | None = None       # the last parsed file
        self._grid: tuple[int, int] | None = None
        self._shapes(0)
        many = len(urls) > 1
        self.axes = ("z" if many else "") + "yx"
        self.shape = ((len(urls),) if many else ()) + self._grid
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # a y/x region costs its file: the JSON is parsed whole
        self.voxel_size_nm = None       # a LabelMe file has no pixel size

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._paint(0, y, x)
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._paint(k, y, x) for k in range(len(self._urls))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + self._paint(0, y, x).shape, self.dtype)
        return self._paint(range(len(self._urls))[z], y, x)     # IndexError when out of range, like numpy

    def _shapes(self, k: int) -> list[Shape]:
        """The polygons of file k, checked against the first file's grid and against `classes`."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        grid, shapes = _labelme(_all_bytes(self._io, url), self._mode, url)
        if self._grid is None:
            self._grid = grid
        elif grid != self._grid:
            raise ValueError(f"{url}: imageHeight x imageWidth {grid} differs from the first file's "
                             f"{self._grid}; all files of one array must agree, bind the others as another array")
        if self._classes is not None:
            for label, _ in shapes:
                if label not in self._classes:
                    raise ValueError(f"{url}: label {label!r} is not in params.classes {self._classes}")
        self._last = (k, shapes)
        return shapes

    def _paint(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
        """The asked part of plane k: polygons filled largest first, so the smaller of two that overlap is on top
        (file order breaks ties). instance: 1-based shape number in the file; semantic: 1-based index of the
        shape's label in `classes`. 0 is background."""
        (ny, nx), shapes = self._grid, self._shapes(k)
        y0, y1, yk = _window(y, ny)
        x0, x1, xk = _window(x, nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for i in sorted(range(len(shapes)), key=lambda i: -_area(shapes[i][1])):
            label, pts = shapes[i]
            (px0, py0), (px1, py1) = pts.min(axis=0), pts.max(axis=0)
            by0, by1 = int(np.clip(np.floor(py0), y0, y1)), int(np.clip(np.ceil(py1), y0, y1))
            bx0, bx1 = int(np.clip(np.floor(px0), x0, x1)), int(np.clip(np.ceil(px1), x0, x1))
            if by0 >= by1 or bx0 >= bx1:
                continue                # the polygon does not reach the asked region
            value = i + 1 if self._mode == "instance" else self._classes.index(label) + 1
            out[by0 - y0:by1 - y0, bx0 - x0:bx1 - x0][_inside(pts, by0, by1, bx0, bx1)] = value
        return out[yk, xk]


def _stem_key(url: str) -> tuple[str, str]:
    """A file's url without its extension, then the extension: `a.json`, `a_1.json` sort like `a.jpg`,
    `a_1.jpg`, so an annotation glob and an image glob over the same stems pair plane for plane."""
    head, sep, name = url.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    return (head + sep + stem, ext) if dot else (url, "")


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by stem; other urls
    pass through, in binding order."""
    out = [u for url in urls for u in sorted(io.expand(url), key=_stem_key)]
    if not out:
        raise ValueError("the urls match no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    mode, classes = src.params.get("ids", "instance"), src.params.get("classes")
    if mode not in ID_MODES:
        raise ValueError(f"params.ids must be one of {ID_MODES}, got {mode!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct labels, got {classes!r}")
    if mode == "semantic" and classes is None:
        raise ValueError("params.ids semantic needs params.classes (the shape labels, in class id order)")
    return _Planes(io, _expand(src.urls, io), mode, classes)
