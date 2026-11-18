"""SVG label files of filled `<path>` polygons and `<circle>` discs, one file per z plane (zip members or
directory files chosen by glob, or listed one by one), rasterised as class ids on the file's own pixel grid.

The grid is the root element's `width` x `height` (a `viewBox` must be `0 0 width height`, so one user unit is
one pixel). `params.classes` names the element types to fill, in id order: `[path, circle]` paints paths 1 and
then circles 2 over them, `[path]` or `[circle]` is a {0, 1} mask of one type. `params.origin` says where
coordinate (0, 0) lies: `centre` (coordinates are pixel indices, as an image-processing contour gives them) or
`corner` (SVG user units, as a renderer draws them). `open_array` reads the first file for the grid; `read`
parses and fills the planes it needs.
"""
import re
from xml.etree import ElementTree

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

NS = "{http://www.w3.org/2000/svg}"
ATTRS = {"path": {"d", "fill"}, "circle": {"cx", "cy", "r", "fill"}}    # the shapes read and all they may carry
ROOT_ATTRS = {"width", "height", "viewBox", "version"}
LIMIT = 1e9                                     # coordinates beyond it are refused
UNDRAWN = ("defs", "title", "desc", "metadata")                         # children of <svg> that paint nothing
ORIGINS = {"centre": 0.0, "corner": 0.5}        # the user-space offset of a pixel's sample point from its index
NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)")
SIZE = re.compile(r"\s*([0-9]+)(?:\.0*)?(?:px)?\s*")

Spans = tuple[np.ndarray, np.ndarray, np.ndarray]   # rows, and the closed x interval [left, right] on each row


def _number(text: str | None, where: str) -> float:
    if text is None or not NUMBER.fullmatch(text.strip()):
        raise ValueError(f"{where}: {text!r} is not a plain number")
    return float(text)


def _root(data: bytes, url: str) -> ElementTree.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"{url}: XML with a DOCTYPE or entity declaration is refused")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ValueError(f"{url}: not well-formed XML ({e})") from None
    if root.tag != NS + "svg":
        raise ValueError(f"{url}: the root element is {root.tag!r}, not an <svg> in the SVG namespace")
    return root


def _grid(root: ElementTree.Element, url: str) -> tuple[int, int]:
    """(Y, X) from the root's height and width; a viewBox must say the same, so a user unit is a pixel."""
    size = []
    for key in ("height", "width"):
        m = SIZE.fullmatch(root.get(key, ""))
        if not m or int(m[1]) == 0:
            raise ValueError(f"{url}: <svg {key}={root.get(key)!r}> is not a whole number of pixels, so the file "
                             "does not give a pixel grid")
        size.append(int(m[1]))
    box = root.get("viewBox")
    if box is not None:
        parts = box.replace(",", " ").split()
        if not all(NUMBER.fullmatch(t) for t in parts) or [float(t) for t in parts] != [0, 0, size[1], size[0]]:
            raise ValueError(f"{url}: viewBox {box!r} is not '0 0 {size[1]} {size[0]}' (0 0 width height); a "
                             "scaled or shifted user space is not read")
    extra = set(root.attrib) - ROOT_ATTRS
    if extra:
        raise ValueError(f"{url}: <svg> attribute(s) {', '.join(sorted(extra))} not read; the root may carry only "
                         f"{sorted(ROOT_ATTRS)} (no transform, style or inherited fill)")
    return size[0], size[1]


def _subpaths(d: str, where: str) -> list[list[float]]:
    """The outlines of a path's `d` as flat [x0, y0, x1, y1, ...] lists, one per subpath. Only absolute M, L and
    Z are read: straight outlines, closed (a subpath left open is filled as if closed, as SVG fills it)."""
    if not d.strip() or d.lstrip()[0] != "M":
        raise ValueError(f"{where}: path data {d[:40]!r} does not start with M")
    subs: list[list[float]] = []
    cur: list[float] | None = None
    for cmd, args in re.findall(r"([A-Za-z])([^A-Za-z]*)", d):
        nums = [_number(t, where) for t in args.replace(",", " ").split()]
        if cmd == "Z" and not nums and cur is not None:
            subs.append(cur)
            cur = None
        elif cmd in "ML" and nums and len(nums) % 2 == 0 and (cmd == "M" or cur is not None):
            if cmd == "M":
                if cur is not None:
                    subs.append(cur)
                cur = []
            cur += nums
        elif cmd in "MLZ":
            raise ValueError(f"{where}: path command {cmd} with {len(nums)} number(s)"
                             f"{'' if cmd == 'M' or cur is not None else ' and no open subpath'} is not read")
        else:
            raise ValueError(f"{where}: path command {cmd!r} is not read; this snippet reads straight outlines "
                             "written with absolute M, L and Z only (no curves, arcs, H, V or relative commands)")
    if cur is not None:
        subs.append(cur)
    return subs


def _shapes(root: ElementTree.Element, url: str, want: tuple[str, ...]) -> dict[str, list]:
    """The geometry of the wanted element types: for `path` (xa, ya, xb, yb, path number) edge rows, for `circle`
    (cx, cy, r) rows. Every child of <svg> is checked, wanted or not, so nothing drawn goes unseen."""
    out: dict[str, list] = {name: [] for name in want}
    for number, el in enumerate(root, 1):
        tag = el.tag[len(NS):] if el.tag.startswith(NS) else None      # None: an element of another namespace
        if tag in UNDRAWN:
            continue
        where = f"{url}: element {number} <{tag or el.tag}>"
        if tag not in ATTRS:
            raise ValueError(f"{where} is not read; this snippet reads <path> and <circle> children of <svg> only "
                             "(no groups, other shapes, text, images or styles)")
        extra = set(el.attrib) - ATTRS[tag]
        if extra:
            raise ValueError(f"{where}: attribute(s) {', '.join(sorted(extra))} not read; a <{tag}> may carry only "
                             f"{sorted(ATTRS[tag])} (no stroke, transform, style or fill-rule)")
        if len(el) or el.get("fill", "").strip().lower() == "none":
            raise ValueError(f"{where}: fill none or child elements; a shape is read as filled whole")
        if tag not in out:
            continue
        if tag == "circle":
            cx, cy, r = (_number(el.get(k), f"{where} {k}") for k in ("cx", "cy", "r"))
            if r <= 0:
                raise ValueError(f"{where}: radius {r} is not positive")
            out[tag].append((cx, cy, r))
            continue
        for sub in _subpaths(el.get("d", ""), where):
            xs, ys = sub[0::2], sub[1::2]
            out[tag] += zip(xs, ys, xs[1:] + xs[:1], ys[1:] + ys[:1], [number] * len(xs))
    return out


def _rows(lo: np.ndarray, hi: np.ndarray, ny: int) -> tuple[np.ndarray, np.ndarray]:
    """For row ranges [lo[i], hi[i]] cut to the grid: (i, row) of every row of every range, flat."""
    lo, hi = np.clip(lo, 0, ny).astype(np.int64), np.clip(hi, -1, ny - 1).astype(np.int64)
    count = np.maximum(hi - lo + 1, 0)
    i = np.repeat(np.arange(len(lo)), count)
    return i, lo[i] + np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count)


def _path_spans(edges: np.ndarray, ny: int) -> Spans:
    """The rows and closed x intervals that the paths' outlines enclose or touch (nonzero winding within a path,
    plus the outline itself), in coordinates whose integers are the pixels' sample points."""
    xa, ya, xb, yb, path = edges.T
    # each vertex, and each horizontal edge, where it lies on a sample row: the outline belongs to the shape
    flat = (ya == np.floor(ya)) & (ya >= 0) & (ya < ny)
    on = flat & (ya == yb)
    rows = [ya[flat].astype(np.int64)]
    left = [np.where(on, np.minimum(xa, xb), xa)[flat]]
    right = [np.where(on, np.maximum(xa, xb), xa)[flat]]
    # every other edge crosses the sample rows lo <= row < hi once each
    lo, hi = np.minimum(ya, yb), np.maximum(ya, yb)
    e, r = _rows(np.ceil(lo), np.ceil(hi) - 1, ny)
    x = xa[e] + (xb[e] - xa[e]) * (r - ya[e]) / (yb[e] - ya[e])     # product first: exact for whole numbers
    turn = np.sign(yb[e] - ya[e])
    order = np.lexsort((x, r, path[e]))
    x, r, turn = x[order], r[order], turn[order]
    inside = np.cumsum(turn)[:-1] != 0      # the winding is 0 again after the last crossing of a path's row
    rows.append(r[:-1][inside])
    left.append(x[:-1][inside])
    right.append(x[1:][inside])
    return np.concatenate(rows), np.concatenate(left), np.concatenate(right)


def _circle_spans(circles: np.ndarray, ny: int) -> Spans:
    """The rows and closed x intervals of the discs (centre distance <= r), in sample-point coordinates."""
    cx, cy, rad = circles.T
    i, r = _rows(np.ceil(cy - rad), np.floor(cy + rad), ny)
    half = np.sqrt(np.maximum(rad[i] ** 2 - (r - cy[i]) ** 2, 0))
    return r, cx[i] - half, cx[i] + half


def _mask(spans: Spans, shape: tuple[int, int]) -> np.ndarray:
    """True at the pixels whose sample point lies in one of the closed row intervals."""
    rows, left, right = spans
    first = np.clip(np.ceil(left), 0, shape[1]).astype(np.int64)
    after = np.clip(np.floor(right) + 1, 0, shape[1]).astype(np.int64)
    keep = first < after
    cover = np.zeros((shape[0], shape[1] + 1), np.int32)
    np.add.at(cover, (rows[keep], first[keep]), 1)
    np.add.at(cover, (rows[keep], after[keep]), -1)
    return np.cumsum(cover, axis=1, out=cover)[:, :-1] > 0


class _Planes:
    def __init__(self, io: IO, urls: list[str], classes: tuple[str, ...], origin: str):
        self._io, self._urls, self._classes, self._offset = io, urls, classes, ORIGINS[origin]
        self._yx = _grid(_root(self._load(0), urls[0]), urls[0])
        self._last: tuple[int, np.ndarray] | None = None        # the last plane filled
        self.axes = ("z" if len(urls) > 1 else "") + "yx"
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + self._yx
        self.dtype = np.dtype("uint8")
        self.voxel_size_nm = None       # SVG user units are pixels here; the file gives no physical size
        self.access = "plane"           # one file per plane: any region costs parsing and filling that whole file

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0)[idx]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(len(self._urls))[z])[idx]      # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._urls))[z]]
        if not planes:
            return np.empty((0,) + np.empty(self._yx, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _load(self, k: int) -> bytes:
        try:
            with self._io.open(self._urls[k]) as fh:
                return fh.read()
        except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
            return self._io.fetch(self._urls[k]).read_bytes()

    def _plane(self, k: int) -> np.ndarray:
        """File k's class ids, uint8 (Y, X); 0 where no wanted shape is."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        root = _root(self._load(k), url)
        if _grid(root, url) != self._yx:
            raise ValueError(f"{url}: its grid {_grid(root, url)} (Y, X) differs from the first file's {self._yx}; "
                             "all files of one array must agree")
        shapes = _shapes(root, url, self._classes)
        out = np.zeros(self._yx, np.uint8)
        for value, name in enumerate(self._classes, 1):         # in id order: a later class is on top
            if not shapes[name]:
                continue
            rows = np.array(shapes[name], np.float64)
            if not (np.abs(rows) < LIMIT).all():
                raise ValueError(f"{url}: a <{name}> coordinate is not a number below {LIMIT:g} in size")
            rows[:, :4 if name == "path" else 2] -= self._offset    # now a pixel's sample point is its index
            spans = _path_spans(rows, self._yx[0]) if name == "path" else _circle_spans(rows, self._yx[0])
            out[_mask(spans, self._yx)] = value
        self._last = (k, out)
        return out


def open_array(src: Source, io: IO) -> Array:
    classes, origin = src.params.get("classes"), src.params.get("origin")
    if (not isinstance(classes, list) or not classes or not all(isinstance(c, str) and c in ATTRS for c in classes)
            or len(set(classes)) != len(classes)):
        raise ValueError(f"params.classes must list the element types to fill, each once and in id order, out of "
                         f"{sorted(ATTRS)} (e.g. [path, circle]: paths 1, circles 2 on top); got {classes!r}")
    if origin not in ORIGINS:
        raise ValueError(f"params.origin must be one of {sorted(ORIGINS)}: where coordinate (0, 0) lies in the "
                         f"top-left pixel; got {origin!r}")
    urls = [u for url in src.urls for u in io.expand(url)]      # binding order across urls, name order in a glob
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return _Planes(io, urls, tuple(classes), origin)
