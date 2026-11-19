"""One Aperio SVS whole-slide image, its full-resolution level read tile by tile as yxc; or, for a url that holds
an MS COCO JSON, the polygons of one of its images (`file_name`) rasterised per requested region as a yx
semantic mask of category ids.

A slide is 50k-120k pixels on a side, so neither form ever builds the whole plane: the raw decodes only the
tiles a region touches, the label fills only the pixels of the region."""
import json
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BAND = 1 << 22      # a polygon is filled over at most about this many pixels at a time
YCBCR_J2K = 33003   # Aperio JPEG 2000 with YCbCr samples: not read, its colour conversion was never tested here


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


class _Svs:
    """Page 0 of an Aperio SVS, the full-resolution level, as yxc uint8 (c = the file's samples: R, G, B)."""

    def __init__(self, tif: tifffile.TiffFile, ranged: bool, url: str):
        self.tif = tif
        if not tif.is_svs:
            raise ValueError(f"{url}: a TIFF, but not an Aperio SVS (its first page's description does not start "
                             "with 'Aperio')")
        page = tif.pages[0]
        if not page.is_tiled or page.axes != "YXS" or page.planarconfig != 1 or page.dtype != np.uint8:
            raise ValueError(f"{url}: page 0 is {page.axes} {page.shape} {page.dtype}, tiled={page.is_tiled}, "
                             f"planarconfig={int(page.planarconfig)}; only tiled, contiguous uint8 YXS is read")
        if int(page.compression) == YCBCR_J2K:
            raise ValueError(f"{url}: compression 33003 (Aperio JPEG 2000, YCbCr) is not read by this snippet")
        self._page = page
        self._across = -(-page.imagewidth // page.tilewidth)            # tiles per row of tiles
        if len(page.dataoffsets) != self._across * -(-page.imagelength // page.tilelength):
            raise ValueError(f"{url}: {len(page.dataoffsets)} tiles do not cover {page.shape} in "
                             f"{page.tilelength}x{page.tilewidth} tiles")
        self.axes, self.shape, self.dtype = "yxc", tuple(page.shape), np.dtype("uint8")
        mpp = _mpp(page.description)
        self.voxel_size_nm = {"y": mpp * 1e3, "x": mpp * 1e3} if mpp else None
        self.access = "chunk" if ranged else "file"     # file: the server gave no size or no ranges; staged whole

    def __del__(self) -> None:      # the harness never closes arrays; the file handle goes with the array
        self.tif.close()

    def _tile(self, ty: int, tx: int) -> np.ndarray | None:
        """Tile (ty, tx) decoded as (tilelength, tilewidth, samples); None for a tile the file leaves empty."""
        page, i = self._page, ty * self._across + tx
        if page.databytecounts[i] == 0:
            return None
        fh = self.tif.filehandle
        fh.seek(page.dataoffsets[i])
        data, where, _ = page.decode(fh.read(page.databytecounts[i]), i)
        if data is None:
            return None
        if tuple(where[2:4]) != (ty * page.tilelength, tx * page.tilewidth):
            raise ValueError(f"tile {i} decoded for position {tuple(where[2:4])}, not row {ty} column {tx}")
        return data[0]

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx, nc = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        c0, c1, ci = _span(region.get("c", slice(None)), nc)
        th, tw = self._page.tilelength, self._page.tilewidth
        out = np.zeros((y1 - y0, x1 - x0, nc), self.dtype)         # a tile the file leaves empty stays 0
        if out.size:
            for ty in range(y0 // th, -(-y1 // th)):
                for tx in range(x0 // tw, -(-x1 // tw)):
                    tile = self._tile(ty, tx)
                    if tile is None:
                        continue
                    a0, a1 = max(y0, ty * th), min(y1, (ty + 1) * th)
                    b0, b1 = max(x0, tx * tw), min(x1, (tx + 1) * tw)
                    out[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = tile[a0 - ty * th:a1 - ty * th, b0 - tx * tw:b1 - tx * tw]
        return np.array(out[yi][..., xi, :][..., c0:c1][..., ci])


def _mpp(description: str) -> float | None:
    """`MPP = <micrometres per pixel>` of an Aperio description ('header|key = value|...'); None when absent."""
    for item in description.split("|")[1:]:
        key, _, value = item.partition("=")
        if key.strip() == "MPP":
            try:
                mpp = float(value)
            except ValueError:
                return None
            return mpp if mpp > 0 else None
    return None


class _Polygon:
    """One annotation: its category and the edges of its closed rings, in level-0 pixel coordinates."""

    def __init__(self, category: int, rings: list[np.ndarray]):
        self.category = category
        ends = [np.roll(r, -1, axis=0) for r in rings]
        self.area = float(sum(abs(np.sum(r[:, 0] * e[:, 1] - e[:, 0] * r[:, 1])) / 2 for r, e in zip(rings, ends)))
        a, b = np.concatenate(rings), np.concatenate(ends)
        self.y0, self.x0 = (int(np.floor(v)) for v in (a[:, 1].min(), a[:, 0].min()))     # pixels it can touch:
        self.y1, self.x1 = (int(np.ceil(v)) for v in (a[:, 1].max(), a[:, 0].max()))      # [y0, y1) x [x0, x1)
        sloped = a[:, 1] != b[:, 1]                    # a horizontal edge crosses no row of pixel centres
        self._xa, self._ya, self._xb, self._yb = a[sloped, 0], a[sloped, 1], b[sloped, 0], b[sloped, 1]

    def fill(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        """Bool (y1 - y0, x1 - x0): the pixels of rows [y0, y1) and columns [x0, x1) whose centre (x + 0.5,
        y + 0.5) lies inside, by the even-odd rule: an odd number of edges crosses the row to its right."""
        xa, ya, xb, yb = self._xa, self._ya, self._xb, self._yb
        r0 = np.clip(np.ceil(np.minimum(ya, yb) - 0.5), y0, y1).astype(np.int64)    # an edge crosses the rows
        r1 = np.clip(np.ceil(np.maximum(ya, yb) - 0.5), y0, y1).astype(np.int64)    # r0 <= r < r1 of the window
        n = r1 - r0
        e = np.repeat(np.arange(n.size), n)                                         # one entry per crossing
        rows = r0[e] + np.arange(e.size) - np.repeat(np.cumsum(n) - n, n)
        at = xa[e] + (xb[e] - xa[e]) * (rows + 0.5 - ya[e]) / (yb[e] - ya[e])       # x where it crosses the row
        k = np.clip(np.ceil(at - 0.5), x0, x1).astype(np.int64) - x0               # columns [0, k) are left of it
        w = x1 - x0
        hits = np.bincount((rows - y0) * (w + 1) + k, minlength=(y1 - y0) * (w + 1)).reshape(y1 - y0, w + 1)
        right = hits[:, ::-1].cumsum(axis=1)[:, ::-1]           # right[:, j]: crossings with k >= j
        return right[:, 1:] % 2 == 1


class _Coco:
    """The polygons of one image of a COCO JSON on that image's grid: a pixel holds the category id of the
    smallest polygon whose interior holds its centre; 0 where no polygon does."""

    def __init__(self, doc: dict, file_name: str, url: str):
        if not isinstance(doc, dict) or not all(isinstance(doc.get(k), list) for k in ("images", "annotations",
                                                                                       "categories")):
            raise ValueError(f"{url}: not a COCO JSON (no images, annotations and categories lists)")
        images = [i for i in doc["images"] if i.get("file_name") == file_name]
        if len(images) != 1:
            raise ValueError(f"{url}: {len(images)} images[] entries have file_name {file_name!r}, need exactly one")
        image = images[0]
        known = {c.get("id") for c in doc["categories"]}
        polygons = []
        for a in doc["annotations"]:
            if a.get("image_id") != image["id"]:
                continue
            seg, category = a.get("segmentation"), a.get("category_id")
            if a.get("iscrowd") or not isinstance(seg, list) or not seg:
                raise ValueError(f"{url}: annotation {a.get('id')} is not a polygon (iscrowd or RLE); not read")
            if isinstance(category, bool) or not isinstance(category, int) or category < 1 or category not in known:
                raise ValueError(f"{url}: annotation {a.get('id')} has category_id {category!r}, which is not a "
                                 "positive id of categories[]")
            rings = [np.asarray(part, np.float64) for part in (seg if isinstance(seg[0], list) else [seg])]
            if any(r.ndim != 1 or r.size < 6 or r.size % 2 for r in rings):
                raise ValueError(f"{url}: annotation {a.get('id')} has a segmentation that is not x, y pairs of "
                                 "three or more vertices")
            polygons.append(_Polygon(category, [r.reshape(-1, 2) for r in rings]))
        top = max((p.category for p in polygons), default=0)
        if top > 65535:
            raise ValueError(f"{url}: category id {top} does not fit uint16")
        # painted largest first, so where polygons overlap the smallest is kept; equal areas in annotations[] order
        self._polygons = sorted(polygons, key=lambda p: -p.area)
        self.axes, self.shape = "yx", (int(image["height"]), int(image["width"]))
        self.dtype = np.dtype("uint8" if top <= 255 else "uint16")
        self.voxel_size_nm = None       # COCO states no pixel size
        self.access = "chunk"           # a read costs what its region costs; the JSON was read whole at open

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for p in self._polygons:
            a0, a1, b0, b1 = max(y0, p.y0), min(y1, p.y1), max(x0, p.x0), min(x1, p.x1)
            if a0 >= a1 or b0 >= b1:
                continue
            step = max(1, BAND // (b1 - b0))
            for r in range(a0, a1, step):
                s = min(r + step, a1)
                out[r - y0:s - y0, b0 - x0:b1 - x0][p.fill(r, s, b0, b1)] = p.category
        return np.array(out[yi][..., xi])


def _open(io: IO, url: str) -> tuple[BinaryIO, bool]:
    """(file, read by range?)"""
    try:
        return io.open(url), True
    except NeedsFetch:          # a deflated zip member, a server that states no size or ignores Range: staged whole
        return open(io.fetch(url), "rb"), False


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"svs-coco-polygons reads one file, got {len(src.urls)} urls")
    url, file_name = src.urls[0], src.params.get("file_name")
    fh, ranged = _open(io, url)
    head = fh.read(4)
    fh.seek(0)
    if head[:2] in (b"II", b"MM"):
        if file_name is not None:
            raise ValueError("params.file_name is for the label form (a COCO JSON url); this url is a TIFF")
        return _Svs(tifffile.TiffFile(fh), ranged, url)
    if head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] != b"{":
        raise ValueError(f"{url}: neither a TIFF nor a JSON object: header={head!r}")
    if not isinstance(file_name, str) or not file_name:
        raise ValueError("params.file_name is required for a COCO JSON: the images[].file_name of the slide "
                         f"this label is for, got {file_name!r}")
    with fh:
        doc = json.loads(fh.read())
    return _Coco(doc, file_name, url)
