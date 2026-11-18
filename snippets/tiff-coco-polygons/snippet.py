"""One plain 2D TIFF (grey or RGB, pyramidal or not: its full-resolution image) at a plain URL or in a zip; or,
with `params.image`, the polygon annotations of that image in an MS COCO JSON file, filled onto the image's
pixel grid as semantic class ids (yx; a pixel holds the `category_id` of the polygon that encloses its centre,
0 where none does).

A COCO file holds the annotations of many images; `params.image` names the one (`images[].file_name`) whose
annotations form the array, and the grid is that entry's `height` x `width`. Where polygons overlap, the one
with the smaller area is on top. The JSON is staged whole at open (it holds the shape); polygons are filled
when a region is read, and only the ones that reach into it."""
import json
from typing import Any

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXIS = {"S": "c", "Y": "y", "X": "x"}           # tifffile letter -> czyx
COCO_PARAMS = ("image", "clip")
Ring = tuple[np.ndarray, np.ndarray]          # x and y of one closed outline, in image pixels


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rings(seg: Any, what: str) -> list[Ring]:
    """A COCO `segmentation` as outlines: a list of [x0, y0, x1, y1, ...] lists (the COCO form), or one such
    flat list (written by some exporters). RLE masks (a dict) are not polygons and are refused."""
    if not isinstance(seg, list) or not seg:
        raise ValueError(f"{what}: segmentation is {type(seg).__name__}, not a list of polygon coordinates "
                         "(RLE masks are not read)")
    parts = [seg] if _number(seg[0]) else seg
    out = []
    for p in parts:
        if not isinstance(p, list) or len(p) < 6 or len(p) % 2 or not all(_number(v) for v in p):
            raise ValueError(f"{what}: a polygon must be a list of at least 3 x, y number pairs")
        xy = np.asarray(p, "f8")
        if not np.isfinite(xy).all():
            raise ValueError(f"{what}: a polygon has a coordinate that is not finite")
        out.append((xy[0::2], xy[1::2]))
    return out


def _area(rings: list[Ring]) -> float:
    """Sum of the outlines' shoelace areas: what orders overlapping polygons."""
    return float(sum(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2 for x, y in rings))


def _fill(x: np.ndarray, y: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """The pixels of rows y0..y1 and columns x0..x1 whose centres (row + 0.5, column + 0.5) lie inside the closed
    outline, by the even-odd rule. One crossing per edge and pixel row it passes; a pixel is inside when an odd
    number of crossings lie at or left of its centre."""
    xa, ya, xb, yb = x, y, np.roll(x, -1), np.roll(y, -1)
    lo = np.clip(np.ceil(np.minimum(ya, yb) - 0.5), y0, y1).astype(np.int64)    # rows r with min <= r + 0.5 < max
    hi = np.clip(np.ceil(np.maximum(ya, yb) - 0.5), y0, y1).astype(np.int64)
    n = hi - lo
    e = np.repeat(np.arange(len(n)), n)
    rows = lo[e] + np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    xc = xa[e] + (xb[e] - xa[e]) * (rows + 0.5 - ya[e]) / (yb[e] - ya[e])       # n > 0 only where ya != yb
    cols = np.clip(np.ceil(xc - 0.5), x0, x1).astype(np.int64) - x0             # first column at or right of it
    h, w = y1 - y0, x1 - x0
    odd = np.bincount((rows - y0) * (w + 1) + cols, minlength=h * (w + 1)).reshape(h, w + 1)[:, :w] & 1
    return np.bitwise_xor.accumulate(odd.astype(np.uint8), axis=1).astype(bool)


def _span(sel: int | slice, n: int) -> tuple[int, int, int | slice]:
    """A selection on an axis of n as (first index, one past the last, the selection within that run)."""
    if not isinstance(sel, slice):
        k = int(sel) + n if int(sel) < 0 else int(sel)
        if not 0 <= k < n:
            raise IndexError(f"index {sel} is out of range for {n}")
        return k, k + 1, 0
    r = range(n)[sel]
    if len(r) == 0:
        return 0, 0, slice(0, 0)
    lo = min(r[0], r[-1])
    stop = r.stop - lo
    return lo, max(r[0], r[-1]) + 1, slice(r.start - lo, stop if stop >= 0 else None, r.step)


class _Polygons:
    def __init__(self, shape: tuple[int, int], dtype: np.dtype, polygons: list[tuple[int, list[Ring]]]):
        self.axes, self.shape, self.dtype = "yx", shape, dtype
        self.voxel_size_nm = None       # a COCO file states no pixel size
        self.access = "file"            # the JSON was staged whole at open; any region is then filled from memory
        # (category id, outlines, their bounding box as rows y0..y1 and columns x0..x1), in drawing order
        self._polygons = [(cat, rings,
                           (int(np.floor(min(y.min() for _, y in rings))), int(np.ceil(max(y.max() for _, y in rings))),
                            int(np.floor(min(x.min() for x, _ in rings))), int(np.ceil(max(x.max() for x, _ in rings)))))
                          for cat, rings in polygons]

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        unknown = set(region) - set(self.axes)
        if unknown:
            raise ValueError(f"region names {sorted(unknown)}, the array has axes {self.axes!r}")
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for cat, rings, (by0, by1, bx0, bx1) in self._polygons:     # largest first: the smallest ends on top
            a0, a1, b0, b1 = max(y0, by0), min(y1, by1), max(x0, bx0), min(x1, bx1)
            if a0 >= a1 or b0 >= b1:
                continue
            mask = np.zeros((a1 - a0, b1 - b0), bool)
            for x, y in rings:                                       # a polygon is the union of its outlines
                mask |= _fill(x, y, a0, a1, b0, b1)
            out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][mask] = cat
        return np.array(out[yi][..., xi])


def _list(doc: Any, key: str, url: str) -> list[dict]:
    v = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(v, list) or not all(isinstance(i, dict) for i in v):
        raise ValueError(f"{url}: not a COCO file: no list of objects under {key!r}")
    return v


def _coco(url: str, params: dict, io: IO) -> Array:
    name, clip = params.get("image"), params.get("clip", False)
    if not isinstance(name, str) or not name:
        raise ValueError("params.image must be the images[].file_name whose annotations form the array")
    if not isinstance(clip, bool):
        raise ValueError(f"params.clip must be true or false, got {clip!r}")
    with open(io.fetch(url), "rb") as fh:
        head = fh.read(1)
        fh.seek(0)
        if head not in (b"{", b" ", b"\n", b"\r", b"\t", b"\xef"):
            raise ValueError(f"{url}: not a JSON object (starts with {head!r})")
        doc = json.load(fh)
    images, annotations, categories = (_list(doc, k, url) for k in ("images", "annotations", "categories"))
    found = [i for i in images if i.get("file_name") == name]
    if len(found) != 1:
        some = ", ".join(repr(i.get("file_name")) for i in images[:5])
        raise ValueError(f"{url}: {len(found)} of its {len(images)} images have file_name {name!r} "
                         f"(the first are {some})")
    image = found[0]
    width, height = image.get("width"), image.get("height")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (width, height)):
        raise ValueError(f"{url}: image {name!r} has width {width!r} and height {height!r}, not a pixel size")
    ids = [c.get("id") for c in categories]
    if not ids or not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i <= 65535 for i in ids):
        raise ValueError(f"{url}: category ids must be integers in 1..65535 (0 is background), got {ids[:20]}")
    polygons, outside = [], []
    for a in annotations:
        if a.get("image_id") != image.get("id"):
            continue
        what = f"{url}: annotation {a.get('id')!r} of {name!r}"
        if a.get("category_id") not in ids:
            raise ValueError(f"{what}: category_id {a.get('category_id')!r} is not among the file's categories")
        if a.get("iscrowd"):
            raise ValueError(f"{what}: iscrowd annotations (RLE masks of groups) are not read")
        rings = _rings(a.get("segmentation"), what)
        beyond = max(max(-x.min(), x.max() - width, -y.min(), y.max() - height) for x, y in rings)
        if beyond > 0:
            outside.append((beyond, a.get("id")))
        polygons.append((a["category_id"], rings))
    if outside and not clip:
        far, worst = max(outside)
        raise ValueError(f"{url}: {len(outside)} of the {len(polygons)} polygons of {name!r} leave its "
                         f"{width} x {height} grid, annotation {worst!r} by {far:g} px. Polygons drawn on another "
                         "frame are misplaced, not just cut: check them against the image, then set "
                         "params.clip: true to keep the part on the grid (with the reason in the binding)")
    polygons.sort(key=lambda p: -_area(p[1]))       # stable: of two equal areas, the later in the file is on top
    return _Polygons((height, width), np.dtype("uint8" if max(ids) <= 255 else "uint16"), polygons)


class _Tiff:
    """The full-resolution image of a plain single-image TIFF: series 0, level 0, one page."""

    def __init__(self, tif: tifffile.TiffFile, access: str, url: str):
        self._tif, self.access = tif, access
        s = tif.series[0]
        if len(tif.series) != 1 or tuple(s.shape) != tuple(s.keyframe.shape) or not set(s.axes) <= set(AXIS):
            raise ValueError(f"{url}: {len(tif.series)} series, the first {s.axes!r} {tuple(s.shape)} over pages "
                             f"of {tuple(s.keyframe.shape)}: not one 2D grey or RGB image (stacks, channels as "
                             "pages and multi-image files are for the tiff-* snippets)")
        self.axes = "".join(AXIS[a] for a in s.axes)
        self.shape, self.dtype = tuple(s.shape), np.dtype(s.dtype)
        self.voxel_size_nm = None       # plain TIFF resolution tags are not read: scanner exports leave defaults

    def __del__(self) -> None:          # the harness never closes arrays; the file handle goes with the array
        self._tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        unknown = set(region) - set(self.axes)
        if unknown:
            raise ValueError(f"region names {sorted(unknown)}, the array has axes {self.axes!r}")
        page = self._tif.asarray(series=0, level=0)     # the whole page is decoded: access is "plane"
        return np.array(page[tuple(region.get(a, slice(None)) for a in self.axes)])


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"tiff-coco-polygons reads one file (a TIFF, or with params.image a COCO JSON), "
                         f"got {len(src.urls)} urls")
    url = src.urls[0]
    if "image" in src.params:
        return _coco(url, src.params, io)
    given = [k for k in COCO_PARAMS if k in src.params]
    if given:
        raise ValueError(f"params {given} are for the COCO form, which params.image selects; without it the url "
                         "is read as a TIFF")
    try:
        fh, access = io.open(url), "plane"
    except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
        fh, access = open(io.fetch(url), "rb"), "file"
    magic = fh.read(4)
    fh.seek(0)
    if magic[:2] not in (b"II", b"MM"):
        raise ValueError(f"{url} starts with {magic!r}: not a TIFF (a COCO JSON needs params.image)")
    return _Tiff(tifffile.TiffFile(fh), access, url)
