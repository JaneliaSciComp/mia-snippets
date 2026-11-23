"""One Aperio SVS whole-slide image, its full-resolution level read tile by tile as yxc; or, for a url that holds
an MS COCO JSON, the `bbox` entries of one of its images (`file_name`) as filled boxes on that image's
width x height grid, yx.

The annotations need no `segmentation`: only `bbox`, `image_id` and, for `boxes: semantic`, `category_id` are
read. `bbox` says how the four numbers are meant: `xyxy` for corners [x1, y1, x2, y2] (what the SlideRunner
export MITOS_WSI_CCMCT_*.json writes), `xywh` for COCO's own [x, y, width, height].

A slide is 50k-120k pixels on a side, so neither form ever builds the whole plane: the raw decodes only the
tiles a region touches, the label paints only the boxes a region touches."""
import json
import math
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
BBOX_FORMS = ("xyxy", "xywh")
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
            return mpp if math.isfinite(mpp) and mpp > 0 else None
    return None


class _Boxes:
    """Filled boxes on a (ny, nx) grid, yx. A pixel holds the value of the smallest box that covers it (boxes are
    painted largest first, annotations[] order breaking ties: the later one is on top); 0 where none does."""

    def __init__(self, shape: tuple[int, int], boxes: np.ndarray, values: np.ndarray, dtype: np.dtype):
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])        # before clipping: the box's own
        order = np.argsort(-area, kind="stable")
        clipped = np.clip(boxes[order], 0, [shape[0], shape[1], shape[0], shape[1]])
        self._boxes, self._values = clipped, values[order]      # rows of y0, x0, y1, x1: [y0, y1) x [x0, x1)
        self.axes, self.shape, self.dtype = "yx", shape, dtype
        self.voxel_size_nm = None       # COCO states no pixel size
        self.access = "chunk"           # a read costs what its region costs; the JSON was read whole at open

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        b = self._boxes
        for k in np.flatnonzero((b[:, 0] < y1) & (b[:, 2] > y0) & (b[:, 1] < x1) & (b[:, 3] > x0)):
            by0, bx0, by1, bx1 = (int(v) for v in b[k])
            out[max(by0, y0) - y0:min(by1, y1) - y0, max(bx0, x0) - x0:min(bx1, x1) - x0] = self._values[k]
        return np.array(out[yi][..., xi])


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _box(bbox: object, form: str, where: str) -> tuple[int, int, int, int]:
    """(y0, x0, y1, x1) of one `bbox`: the pixels [y0, y1) x [x0, x1) it touches, a fractional edge moved
    outwards. Not clipped to the grid."""
    if not isinstance(bbox, list) or len(bbox) != 4 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in bbox):
        raise ValueError(f"{where}: bbox is not a list of four numbers: {str(bbox)[:80]}")
    xa, ya, xb, yb = bbox if form == "xyxy" else (bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3])
    if not (xa < xb and ya < yb):
        raise ValueError(f"{where}: bbox {bbox} read as {form} is empty or reversed (x {xa}..{xb}, y {ya}..{yb}); "
                         "is params.bbox right?")
    return math.floor(ya), math.floor(xa), math.ceil(yb), math.ceil(xb)


def _coco(doc: object, url: str, p: dict) -> _Boxes:
    """The boxes of the image `file_name` of a COCO document."""
    file_name, mode, form, count = p.get("file_name"), p.get("boxes"), p.get("bbox"), p.get("count")
    if not isinstance(file_name, str) or not file_name:
        raise ValueError("params.file_name is required for a COCO JSON: the images[].file_name of the slide this "
                         f"label is for, got {file_name!r}")
    if mode not in BOX_MODES:
        raise ValueError(f"params.boxes is required for a COCO JSON: one of {BOX_MODES}, got {mode!r}")
    if form not in BBOX_FORMS:
        raise ValueError(f"params.bbox is required for a COCO JSON: one of {BBOX_FORMS} (xyxy = corners "
                         f"[x1, y1, x2, y2], xywh = COCO's [x, y, width, height]), got {form!r}")
    if count is not None and (not _whole(count) or count < 0):
        raise ValueError(f"params.count must be the number of annotations the image has, got {count!r}")
    if not isinstance(doc, dict) or not all(isinstance(doc.get(k), list) for k in ("images", "annotations",
                                                                                   "categories")):
        raise ValueError(f"{url}: not a COCO JSON (no images, annotations and categories lists)")
    images = [i for i in doc["images"] if isinstance(i, dict) and i.get("file_name") == file_name]
    if len(images) != 1:
        raise ValueError(f"{url}: {len(images)} images[] entries have file_name {file_name!r} (it lists "
                         f"{len(doc['images'])} images), need exactly one")
    image = images[0]
    if not all(_whole(image.get(k)) and image[k] > 0 for k in ("height", "width")) or not _whole(image.get("id")):
        raise ValueError(f"{url}: the images[] entry of {file_name!r} has no whole-number id, height and width: "
                         f"{str(image)[:200]}")
    shape = (image["height"], image["width"])
    known = {c.get("id") for c in doc["categories"] if isinstance(c, dict) and _whole(c.get("id"))}
    boxes, categories = [], []
    for a in doc["annotations"]:
        # an annotation of another image, or of none (image_id null), is not this image's, whatever it holds
        if not isinstance(a, dict) or not _whole(a.get("image_id")) or a["image_id"] != image["id"]:
            continue
        where = f"{url}: annotation {a.get('id')} of {file_name!r}"
        boxes.append(_box(a.get("bbox"), form, where))
        category = a.get("category_id")
        if mode == "semantic" and (not _whole(category) or category < 1 or category not in known):
            raise ValueError(f"{where} has category_id {category!r}, which is not a positive id of categories[]; "
                             "a box without a class has no semantic value")
        categories.append(category)
    if count is not None and count != len(boxes):
        raise ValueError(f"params.count is {count}, but {url} has {len(boxes)} annotations for {file_name!r}")
    at = np.array(boxes, np.int64).reshape(-1, 4)
    if mode == "instance":      # numbered in annotations[] order, from 1
        return _Boxes(shape, at, np.arange(1, len(boxes) + 1, dtype=np.uint32), np.dtype("uint32"))
    top = max(categories, default=0)
    if top > 65535:
        raise ValueError(f"{url}: category id {top} does not fit uint16")
    dtype = np.dtype("uint8" if top <= 255 else "uint16")
    return _Boxes(shape, at, np.array(categories, dtype), dtype)


def _open(io: IO, url: str) -> tuple[BinaryIO, bool]:
    """(file, read by range?)"""
    try:
        return io.open(url), True
    except NeedsFetch:          # a deflated zip member, a server that states no size or ignores Range: staged whole
        return open(io.fetch(url), "rb"), False


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"svs-coco-boxes-i308 reads one file, got {len(src.urls)} urls")
    url = src.urls[0]
    fh, ranged = _open(io, url)
    head = fh.read(4)
    fh.seek(0)
    if head[:2] in (b"II", b"MM"):
        if src.params:
            raise ValueError(f"params {sorted(src.params)} are for the label form (a COCO JSON url); this url is "
                             "a TIFF")
        return _Svs(tifffile.TiffFile(fh), ranged, url)
    if head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] != b"{":
        raise ValueError(f"{url}: neither a TIFF nor a JSON object: header={head!r}")
    with fh:
        try:
            doc = json.loads(fh.read().decode("utf-8-sig"))
        except ValueError as e:     # UnicodeDecodeError and JSONDecodeError are ValueErrors
            raise ValueError(f"{url}: not a UTF-8 JSON document: {e}") from None
    return _coco(doc, url, src.params)
