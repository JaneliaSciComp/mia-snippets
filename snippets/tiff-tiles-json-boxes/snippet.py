"""One single-page 2D TIFF (grey or RGB, tiled or stripped) read segment by segment, or the boxes of one
COCO-like JSON annotation file filled onto that TIFF's pixel grid.

The image form takes one url, the TIFF: `yx`, or `cyx` with the RGB samples kept as c. A region is read by
decoding only the tiles (or strips) it touches, so a crop of a whole-slide section costs a few tiles, not the
section. The label form takes two urls, the JSON and the TIFF it annotates: the JSON says where the boxes are
(`annotations[].bbox` = [x, y, width, height] in pixels, `annotations[].category` = an id of `categories`) but
not how large the image is, so the grid's shape is taken from the TIFF's header; no pixel of the TIFF is read.
Each box is filled with its 1-based number in the file (`boxes: instance`) or with the 1-based index of its
category's name in `classes` (`boxes: semantic`). Which url is which is decided by the first bytes, not by names.
"""
import json
from pathlib import Path
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
CACHE_BYTES = 64 << 20      # decoded segments kept: the harness reads the c planes of one region in turn

Box = tuple[int, int, int, int, int]        # semantic id, x0, y0, x1, y1 (far corner exclusive)


def _source(io: IO, url: str) -> tuple[BinaryIO | Path, bool]:
    """The file, and whether it is read by range (False: staged whole)."""
    try:
        return io.open(url), True
    except NeedsFetch:          # a deflated member, or a server without Range
        return io.fetch(url), False


def _head(src: BinaryIO | Path) -> bytes:
    if isinstance(src, Path):
        with src.open("rb") as f:
            return f.read(64)
    src.seek(0)
    head = src.read(64)
    src.seek(0)
    return head


def _kind(src: BinaryIO | Path, url: str) -> str:
    """'tiff' or 'json', from the file's first bytes."""
    head = _head(src)
    if head[:4] in TIFF_MAGIC:
        return "tiff"
    if head.removeprefix(b"\xef\xbb\xbf").lstrip()[:1] == b"{":
        return "json"
    raise ValueError(f"{url}: neither a TIFF nor a JSON object: header={head[:4]!r}")


def _page(tif: tifffile.TiffFile, url: str) -> tifffile.TiffPage:
    """The one page of a single-series YX or YXS TIFF."""
    s = tif.series[0]
    if len(tif.series) != 1 or len(s.pages) != 1 or s.axes not in ("YX", "YXS"):
        raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.series)} series; this "
                         "snippet reads one single-page YX or YXS (RGB) image, see tiff-zip-member-2 for stacks")
    page = tif.pages[0]
    if page.photometric == tifffile.PHOTOMETRIC.PALETTE:
        raise ValueError(f"{url}: a palette (colour-mapped) TIFF; its values are colour indices, not intensities")
    if tuple(page.shape) != tuple(s.shape):
        raise ValueError(f"{url}: page shape {tuple(page.shape)} is not the series shape {tuple(s.shape)}")
    return page


def _select(sel: int | slice, n: int, axis: str) -> tuple[range, bool]:
    """A selection on an axis of n as the indices it names, and whether it drops the axis (an int)."""
    if isinstance(sel, slice):
        return range(n)[sel], False
    k = int(sel) + n if int(sel) < 0 else int(sel)
    if not 0 <= k < n:
        raise IndexError(f"{axis} {sel} is out of range for {n}")
    return range(k, k + 1), True


def _window(region: dict[str, int | slice], ny: int, nx: int) -> tuple[tuple[int, int, int, int], tuple]:
    """The y/x bounding window (y0, y1, x0, x1) a region needs, and the index that cuts the region out of it."""
    bounds, index = [], []
    for axis, n in (("y", ny), ("x", nx)):
        r, drop = _select(region.get(axis, slice(None)), n, axis)
        lo, hi = (min(r[0], r[-1]), max(r[0], r[-1]) + 1) if len(r) else (0, 0)
        bounds += [lo, hi]
        if drop:
            index.append(0)
        elif len(r) == 0:
            index.append(slice(0, 0))
        else:
            stop = r[0] - lo + len(r) * r.step
            index.append(slice(r[0] - lo, stop if stop >= 0 else None, r.step))
    return tuple(bounds), tuple(index)


class _Tiles:
    """The image: (Y, X) or (Y, X, S) in the file, presented as yx or cyx, decoded one tile or strip at a time."""

    def __init__(self, tif: tifffile.TiffFile, url: str, ranged: bool):
        self._tif, self._url = tif, url
        self._page = page = _page(tif, url)
        self._ny, self._nx = int(page.imagelength), int(page.imagewidth)
        self._ns = int(page.shape[2]) if len(page.shape) == 3 else 0
        if page.is_tiled:
            self._sy, self._sx = int(page.tilelength), int(page.tilewidth)
        else:
            self._sy, self._sx = min(int(page.rowsperstrip), self._ny), self._nx
        self._across = -(-self._nx // self._sx)
        segments = self._across * -(-self._ny // self._sy)
        if len(page.dataoffsets) != segments or len(page.databytecounts) != segments:
            raise ValueError(f"{url}: {len(page.dataoffsets)} segments in the file, {segments} expected for "
                             f"{self._ny}x{self._nx} in {self._sy}x{self._sx} segments (planar samples or an "
                             "unusual layout are not read)")
        # taken here, not at the first read: tifffile builds them lazily and may move the file position
        self._decode, self._tables = page.decode, page.jpegtables
        self._cache: dict[int, np.ndarray] = {}
        self.axes = ("c" if self._ns else "") + "yx"
        self.shape = ((self._ns,) if self._ns else ()) + (self._ny, self._nx)
        self.dtype = np.dtype(page.dtype)
        self.access = "chunk" if ranged else "file"     # a region costs its tiles; a staged file is paid at open
        self.voxel_size_nm = None       # TIFF resolution tags describe print size; see the README

    def __del__(self) -> None:          # the harness never closes arrays; the file handle goes with the array
        self._tif.close()

    def _segment(self, k: int) -> np.ndarray:
        """Segment k as (rows, columns[, S]), cut to the image; an absent segment is zeros."""
        if k in self._cache:
            return self._cache[k]
        page, y0, x0 = self._page, (k // self._across) * self._sy, (k % self._across) * self._sx
        shape = (min(self._sy, self._ny - y0), min(self._sx, self._nx - x0)) + ((self._ns,) if self._ns else ())
        offset, count = int(page.dataoffsets[k]), int(page.databytecounts[k])
        seg = None
        if count:
            fh = self._tif.filehandle
            fh.seek(offset)
            seg = self._decode(fh.read(count), k, jpegtables=self._tables)[0]
        a = np.zeros(shape, self.dtype) if seg is None else np.asarray(seg).reshape(seg.shape[1:])
        if not self._ns and a.ndim == 3:
            a = a[..., 0]
        a = a[:shape[0], :shape[1]]
        if a.shape != shape or a.dtype != self.dtype:
            raise ValueError(f"{self._url}: segment {k} decoded as {a.dtype} {a.shape}, expected {self.dtype} "
                             f"{shape}")
        while self._cache and sum(v.nbytes for v in self._cache.values()) + a.nbytes > CACHE_BYTES:
            self._cache.pop(next(iter(self._cache)))        # the oldest first
        self._cache[k] = a
        return a

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        (y0, y1, x0, x1), index = _window(region, self._ny, self._nx)
        out = np.zeros((y1 - y0, x1 - x0) + ((self._ns,) if self._ns else ()), self.dtype)
        if out.size:
            for ty in range(y0 // self._sy, (y1 - 1) // self._sy + 1):
                for tx in range(x0 // self._sx, (x1 - 1) // self._sx + 1):
                    seg, sy0, sx0 = self._segment(ty * self._across + tx), ty * self._sy, tx * self._sx
                    a0, a1 = max(y0, sy0), min(y1, sy0 + seg.shape[0])
                    b0, b1 = max(x0, sx0), min(x1, sx0 + seg.shape[1])
                    out[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = seg[a0 - sy0:a1 - sy0, b0 - sx0:b1 - sx0]
        out = out[index]
        if not self._ns:
            return out
        return np.moveaxis(out, -1, 0)[region.get("c", slice(None))]


def _whole(value: object, what: str, url: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not float(value).is_integer():
        raise ValueError(f"{url}: {what} {value!r} is not a whole number of pixels")
    return int(value)


def _boxes(data: bytes, shape: tuple[int, int], classes: list[str], url: str) -> list[Box]:
    """The annotations of one file, in file order, each cut to the (Y, X) grid."""
    doc = json.loads(data)
    if not isinstance(doc, dict) or not isinstance(doc.get("annotations"), list) \
            or not isinstance(doc.get("categories"), list):
        raise ValueError(f"{url}: not an object with an 'annotations' list and a 'categories' list")
    names: dict[int, str] = {}
    for cat in doc["categories"]:
        if not isinstance(cat, dict) or isinstance(cat.get("id"), bool) or not isinstance(cat.get("id"), int) \
                or not isinstance(cat.get("name"), str):
            raise ValueError(f"{url}: category {cat!r} is not an object with an integer 'id' and a 'name'")
        if cat["id"] in names:
            raise ValueError(f"{url}: category id {cat['id']} is declared twice")
        if cat["name"] not in classes:
            raise ValueError(f"{url}: category name {cat['name']!r} is not in params.classes {classes}")
        names[cat["id"]] = cat["name"]
    if len(doc["annotations"]) > 65535:
        raise ValueError(f"{url}: {len(doc['annotations'])} annotations exceed uint16 ids")
    ny, nx = shape
    out = []
    for n, ann in enumerate(doc["annotations"], 1):
        bbox = ann.get("bbox") if isinstance(ann, dict) else None
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"{url}: annotation {n} has no 'bbox' of [x, y, width, height]: {ann!r}")
        x, y, w, h = (_whole(v, f"annotation {n} bbox value", url) for v in bbox)
        cat = ann.get("category")
        if isinstance(cat, bool) or not isinstance(cat, int) or cat not in names:
            raise ValueError(f"{url}: annotation {n} 'category' {cat!r} is not an id of the file's categories "
                             f"{sorted(names)}")
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, nx), min(y + h, ny)
        if w <= 0 or h <= 0 or x0 >= x1 or y0 >= y1:
            raise ValueError(f"{url}: annotation {n} bbox {bbox} is empty or covers no pixel of the {nx}x{ny} "
                             "image; is this the image the file annotates?")
        out.append((classes.index(names[cat]) + 1, x0, y0, x1, y1))
    return out


class _Boxes:
    """The label: every box filled over [y, y + height) x [x, x + width) on the image's grid, 0 elsewhere."""

    def __init__(self, boxes: list[Box], shape: tuple[int, int], mode: str):
        self._instance = mode == "instance"
        # largest first, so the smaller of two overlapping boxes is on top; of equal ones, the later in the file
        self._order = sorted(enumerate(boxes, 1), key=lambda b: -(b[1][3] - b[1][1]) * (b[1][4] - b[1][2]))
        self.axes = "yx"
        self.shape = shape
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # the whole label is one small file, read at open; any region is free
        self.voxel_size_nm = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        (y0, y1, x0, x1), index = _window(region, *self.shape)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for number, (class_id, bx0, by0, bx1, by1) in self._order:
            a0, a1, b0, b1 = max(by0, y0), min(by1, y1), max(bx0, x0), min(bx1, x1)
            if a0 < a1 and b0 < b1:
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = number if self._instance else class_id
        return out[index]


def _all_bytes(src: BinaryIO | Path) -> bytes:
    if isinstance(src, Path):
        return src.read_bytes()
    src.seek(0)
    return src.read()


def open_array(src: Source, io: IO) -> Array:
    mode, classes = src.params.get("boxes"), src.params.get("classes")
    if mode is not None and mode not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {mode!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    if len(src.urls) not in (1, 2):
        raise ValueError(f"tiff-tiles-json-boxes reads one TIFF, or one JSON annotation file and the TIFF it "
                         f"annotates; got {len(src.urls)} urls")
    files = [(url, *_source(io, url)) for url in src.urls]
    kinds = [_kind(f, url) for url, f, _ in files]
    if len(files) == 1:
        url, f, ranged = files[0]
        if kinds[0] != "tiff":
            raise ValueError(f"{url} is a JSON annotation file: add the url of the TIFF it annotates, whose "
                             "header gives the grid's shape")
        if mode is not None or classes is not None:
            raise ValueError(f"{url} is an image: params.boxes and params.classes apply to the label form "
                             "(a JSON url and a TIFF url) only")
        return _Tiles(tifffile.TiffFile(f), url, ranged)
    if sorted(kinds) != ["json", "tiff"]:
        raise ValueError(f"two urls must be one JSON annotation file and the one TIFF it annotates, got "
                         f"{kinds[0]} and {kinds[1]}; bind each image as its own array")
    if mode is None or not classes:
        raise ValueError(f"a JSON annotation file needs params.boxes ({' or '.join(BOX_MODES)}) and "
                         "params.classes (the category names, in class id order)")
    (json_url, json_file, _), (tiff_url, tiff_file, _) = sorted(files, key=lambda f: kinds[files.index(f)])
    with tifffile.TiffFile(tiff_file) as tif:
        page = _page(tif, tiff_url)
        shape = (int(page.imagelength), int(page.imagewidth))
    return _Boxes(_boxes(_all_bytes(json_file), shape, classes, json_url), shape, mode)
