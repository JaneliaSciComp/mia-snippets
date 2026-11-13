"""A set of 2D images (single-page TIFF or JPEG, grey or RGB) or of Pascal VOC box annotations (labelImg XML),
chosen by glob from zip members or an HTTP directory (or listed one by one), stacked along z, one file per plane.

Images keep their samples: an RGB file is three `c` planes. A VOC file becomes a label plane on the image grid
its `<size>` declares, with every `<object>` box filled: by a per-file object id (`boxes: instance`) or by the
1-based index of its class name in `classes` (`boxes: semantic`). The kind is taken from the first file's
leading bytes, not from its name. `open_array` reads the first file's header only (the whole first file when it
is a JPEG, an XML or a deflated member, still without decoding pixels); `read` fetches the files it needs.
"""
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Box = tuple[str, int, int, int, int]        # class name, xmin, ymin, xmax, ymax


def _source(io: IO, url: str) -> BinaryIO | Path:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url)


def _all_bytes(src: BinaryIO | Path) -> bytes:
    if isinstance(src, Path):
        return src.read_bytes()
    src.seek(0)
    return src.read()


def _kind(src: BinaryIO | Path, url: str) -> str:
    """'tiff', 'jpeg' or 'voc', from the file's first bytes."""
    if isinstance(src, Path):
        with src.open("rb") as f:
            head = f.read(64)
    else:
        src.seek(0)
        head = src.read(64)
        src.seek(0)
    if head[:4] in TIFF_MAGIC:
        return "tiff"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head.removeprefix(b"\xef\xbb\xbf").lstrip()[:1] == b"<":
        return "voc"
    raise ValueError(f"{url}: neither a TIFF, a JPEG nor an XML file: header={head[:4]!r}")


def _jpeg_header(data: bytes, url: str) -> tuple[tuple[int, ...], np.dtype]:
    """(Y, X) or (Y, X, S) and the dtype, from the start-of-frame segment; nothing is decoded."""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:    # markers without a length
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if marker in SOF:
            if pos + 10 > len(data):
                break
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8:
                raise ValueError(f"{url}: {bits}-bit JPEG; this snippet reads 8-bit JPEGs only")
            return ((ny, nx) if ns == 1 else (ny, nx, ns)), np.dtype("uint8")
        if marker == 0xDA:                              # start of scan: no frame header came before it
            break
        pos += 2 + length
    raise ValueError(f"{url}: no JPEG start-of-frame segment found")


def _tiff_series(tif: tifffile.TiffFile, url: str) -> tifffile.TiffPageSeries:
    s = tif.series[0]
    if len(tif.series) != 1 or s.axes not in ("YX", "YXS"):
        raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.series)} series; this "
                         "snippet reads single-page YX or YXS (RGB) images only, see tiff-zip-member for stacks")
    if tif.pages[0].photometric == tifffile.PHOTOMETRIC.PALETTE:
        raise ValueError(f"{url}: a palette (colour-mapped) TIFF; its values are colour indices, not intensities")
    return s


def _image_header(src: BinaryIO | Path, kind: str, url: str) -> tuple[tuple[int, ...], np.dtype]:
    if kind == "tiff":
        with tifffile.TiffFile(src) as tif:
            s = _tiff_series(tif, url)
            return tuple(s.shape), np.dtype(s.dtype)
    return _jpeg_header(_all_bytes(src), url)


def _image(src: BinaryIO | Path, kind: str, url: str) -> np.ndarray:
    """The decoded image, (Y, X) or (Y, X, S)."""
    if kind == "tiff":
        with tifffile.TiffFile(src) as tif:
            return np.asarray(_tiff_series(tif, url).asarray())
    return np.asarray(imagecodecs.jpeg_decode(_all_bytes(src)))


def _int(node: ElementTree.Element | None, tag: str, url: str) -> int:
    text = node.findtext(tag) if node is not None else None
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{url}: <{tag}> is missing or not a number: {text!r}") from None
    if not value.is_integer():
        raise ValueError(f"{url}: <{tag}> {text!r} is not a whole number of pixels")
    return int(value)


def _voc(data: bytes, url: str) -> tuple[tuple[int, int], list[Box]]:
    """(Y, X) from <size> and the <object> boxes of a Pascal VOC annotation, in file order."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"{url}: XML with a DOCTYPE or entity declaration is refused")
    root = ElementTree.fromstring(data)
    if root.tag != "annotation":
        raise ValueError(f"{url}: root element <{root.tag}>, not a Pascal VOC <annotation>")
    size = root.find("size")
    ny, nx = _int(size, "height", url), _int(size, "width", url)
    if ny <= 0 or nx <= 0:
        raise ValueError(f"{url}: <size> {nx}x{ny} is not an image size")
    boxes = []
    for obj in root.findall("object"):
        name = (obj.findtext("name") or "").strip()
        bb = obj.find("bndbox")
        x0, y0, x1, y1 = (_int(bb, t, url) for t in ("xmin", "ymin", "xmax", "ymax"))
        if not (0 <= x0 < x1 <= nx and 0 <= y0 < y1 <= ny):
            raise ValueError(f"{url}: box {name!r} x {x0}..{x1} y {y0}..{y1} is empty or outside the "
                             f"{nx}x{ny} image")
        boxes.append((name, x0, y0, x1, y1))
    return (ny, nx), boxes


def _paint(shape: tuple[int, int], boxes: list[Box], mode: str, classes: list[str], url: str) -> np.ndarray:
    """Boxes filled over pixels [ymin, ymax) x [xmin, xmax), largest first, so the smaller of two overlapping
    boxes is on top (file order breaks ties). instance: 1-based object number in the file; semantic: 1-based
    index of the object's name in `classes`. 0 is background."""
    if len(boxes) > 65535:
        raise ValueError(f"{url}: {len(boxes)} objects exceed uint16 ids")
    out = np.zeros(shape, np.uint16)
    order = sorted(range(len(boxes)), key=lambda i: -(boxes[i][3] - boxes[i][1]) * (boxes[i][4] - boxes[i][2]))
    for i in order:
        name, x0, y0, x1, y1 = boxes[i]
        if name not in classes:
            raise ValueError(f"{url}: object name {name!r} is not in params.classes {classes}")
        out[y0:y1, x0:x1] = i + 1 if mode == "instance" else classes.index(name) + 1
    return out


class _Planes:
    """One file per plane. Images: (Y, X) or (S, Y, X) per file, axes [z][c]yx. VOC: (Y, X) per file, axes [z]yx."""

    def __init__(self, io: IO, urls: list[str], boxes: str | None, classes: list[str] | None):
        self._io, self._urls, self._boxes, self._classes = io, urls, boxes, classes
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded file: its c planes are read in turn
        src = _source(io, urls[0])
        self._voc = _kind(src, urls[0]) == "voc"
        if self._voc:
            if boxes not in BOX_MODES or not classes:
                raise ValueError(f"{urls[0]} is a VOC XML annotation: set params.boxes ({' or '.join(BOX_MODES)}) "
                                 "and params.classes (the object names, in class id order)")
            self._file_shape, _ = _voc(_all_bytes(src), urls[0])
            self._file_dtype = np.dtype("uint16")
        else:
            if boxes is not None or classes is not None:
                raise ValueError(f"{urls[0]} is an image: params.boxes and params.classes apply to VOC XML only")
            self._file_shape, self._file_dtype = _image_header(src, _kind(src, urls[0]), urls[0])
        ny, nx = self._file_shape[:2]
        c = self._file_shape[2:]                # () or (S,)
        many = len(urls) > 1
        self.axes = ("z" if many else "") + ("c" if c else "") + "yx"
        self.shape = ((len(urls),) if many else ()) + c + (ny, nx)
        self.dtype = self._file_dtype
        self.access = "plane"           # a y/x region costs its file, read by range or staged whole
        self.voxel_size_nm = None       # TIFF resolution tags and JPEG densities describe print size, not pixels

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        if "z" not in self.axes:
            return self._file(0)[inner]
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            return np.stack([self._file(k)[inner] for k in range(len(self._urls))[z]])
        return self._file(range(len(self._urls))[z])[inner]     # IndexError when out of range, like numpy

    def _file(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (S, Y, X), checked against the first file."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        src = _source(self._io, url)
        kind = _kind(src, url)
        if (kind == "voc") != self._voc:
            raise ValueError(f"{url} is a {kind} file among {'VOC XML' if self._voc else 'image'} files; "
                             "one array holds images or annotations, not both")
        if self._voc:
            shape, boxes = _voc(_all_bytes(src), url)
            a = _paint(shape, boxes, self._boxes, self._classes, url) if shape == self._file_shape else None
        else:
            a = _image(src, kind, url)
            shape = a.shape
        if a is None or a.shape != self._file_shape or a.dtype != self._file_dtype:
            raise ValueError(f"{url}: {shape} differs from the first file's {self._file_dtype} "
                             f"{self._file_shape}; all files of one array must agree, bind the others as "
                             "another array")
        a = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _stem_key(url: str) -> tuple[str, str]:
    """A file's url without its extension, then the extension: `a.tif`, `a_1.jpeg` and `a.xml`, `a_1.xml` sort
    alike, so an image glob and an annotation glob over the same stems pair plane for plane."""
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
    boxes, classes = src.params.get("boxes"), src.params.get("classes")
    if boxes is not None and boxes not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {boxes!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    return _Planes(io, _expand(src.urls, io), boxes, classes)
