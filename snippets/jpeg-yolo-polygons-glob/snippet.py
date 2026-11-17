"""A set of JPEG images, or the YOLO segmentation label files written for them, chosen by glob from the members
of a zip (or listed one by one), stacked along z, one file per plane.

An image keeps its samples: an RGB JPEG is three `c` planes. A label file is YOLO segmentation text: one row per
object, `class x1 y1 x2 y2 ...`, the vertices of one polygon as fractions of the image's width and height. It
becomes a label plane on the grid of the JPEG with the same stem, read from that JPEG's header, with every polygon
filled: by its 1-based row number in the file (`polygons: instance`) or by its class id + 1 (`polygons:
semantic`). An empty file is an all-background plane. The label form is the one with `params.polygons`; its
planes follow the images `params.images` names, in name order, so they pair with the image form over that glob.
`open_array` reads the first file's header (labels: the first label file and its JPEG's header), decoding
nothing; `read` gets the rest.
"""
import math
from typing import BinaryIO

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("instance", "semantic")
HEAD = 1 << 16      # bytes of a JPEG read for its header; the whole file only when the frame header lies further in
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Polygon = tuple[int, np.ndarray]        # class id, (n, 2) vertices as (x, y) fractions of width and height


def _file(io: IO, url: str) -> BinaryIO:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).open("rb")


def _bytes(io: IO, url: str, n: int = -1) -> bytes:
    with _file(io, url) as f:
        return f.read(n)


def _orientation(exif: bytes) -> int | None:
    """The Orientation tag (0x0112) of a TIFF-structured Exif block's first IFD, or None."""
    order = {b"II": "little", b"MM": "big"}.get(exif[:2])
    if order is None or len(exif) < 8:
        return None
    ifd = int.from_bytes(exif[4:8], order)
    for k in range(int.from_bytes(exif[ifd:ifd + 2], order)):
        entry = exif[ifd + 2 + 12 * k:ifd + 14 + 12 * k]
        if len(entry) == 12 and int.from_bytes(entry[:2], order) == 0x0112:
            return int.from_bytes(entry[8:10], order)
    return None


def _jpeg_header(data: bytes, url: str) -> tuple[tuple[int, ...], int | None] | None:
    """((Y, X) or (Y, X, 3), Exif orientation or None) from the segments before the scan, nothing decoded; None
    when `data` ends before the frame header."""
    if data[:3] != b"\xff\xd8\xff":
        raise ValueError(f"{url}: not a JPEG file: header={data[:4]!r}")
    pos, orientation = 2, None
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:    # markers without a length
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if pos + 2 + length > len(data):
            return None
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00" and orientation is None:
            orientation = _orientation(data[pos + 10:pos + 2 + length])
        if marker in SOF and length >= 8:
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8 or ns not in (1, 3):
                raise ValueError(f"{url}: a {bits}-bit JPEG with {ns} components; this snippet reads 8-bit grey "
                                 "or 3-component JPEGs only")
            return ((ny, nx) if ns == 1 else (ny, nx, ns)), orientation
        if marker == 0xDA:                              # start of scan: no frame header came before it
            raise ValueError(f"{url}: no JPEG start-of-frame segment before the scan")
        pos += 2 + length
    return None


def _header(io: IO, url: str, data: bytes | None = None) -> tuple[tuple[int, ...], int | None]:
    """A JPEG's header from its first HEAD bytes, or from the whole file (`data` when the caller has it)."""
    found = _jpeg_header(_bytes(io, url, HEAD) if data is None else data, url)
    if found is None and data is None:
        found = _jpeg_header(_bytes(io, url), url)
    if found is None:
        raise ValueError(f"{url}: no JPEG start-of-frame segment found")
    return found


def _polygons(data: bytes, classes: int, url: str) -> list[Polygon]:
    """The rows of a YOLO segmentation label file, in file order; [] for an empty file."""
    out: list[Polygon] = []
    for n, line in enumerate(data.removeprefix(b"\xef\xbb\xbf").decode("utf-8").splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        try:
            cls, values = float(fields[0]), [float(f) for f in fields[1:]]
        except ValueError:
            raise ValueError(f"{url} line {n}: not a row of numbers: {line[:60]!r}") from None
        if not (cls.is_integer() and 0 <= cls < classes):
            raise ValueError(f"{url} line {n}: class {fields[0]!r} is not a class id 0..{classes - 1} "
                             "(params.classes)")
        if len(values) == 4:
            raise ValueError(f"{url} line {n}: a row of class and 4 numbers is a YOLO box (cx cy w h), not a "
                             "polygon; this snippet fills segmentation polygons only")
        if len(values) < 6 or len(values) % 2:
            raise ValueError(f"{url} line {n}: {len(values)} numbers after the class are not the x y pairs of a "
                             "polygon (3 vertices or more)")
        if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in values):
            raise ValueError(f"{url} line {n}: a coordinate is outside 0..1; YOLO vertices are fractions of the "
                             "image's width and height")
        out.append((int(cls), np.array(values, np.float64).reshape(-1, 2)))
    if len(out) > 65535:
        raise ValueError(f"{url}: {len(out)} rows exceed uint16 ids")
    return out


def _inside(xy: np.ndarray, ny: int, nx: int) -> tuple[int, int, np.ndarray]:
    """(y0, x0, mask): the pixels whose centres lie inside the closed polygon `xy` (in pixels; pixel (i, j) covers
    x in [j, j + 1), y in [i, i + 1)) by the even-odd rule, as a mask over the polygon's bounding rows and
    columns, whose first are y0 and x0."""
    y0, y1 = max(math.floor(xy[:, 1].min()), 0), min(math.ceil(xy[:, 1].max()), ny)
    x0, x1 = max(math.floor(xy[:, 0].min()), 0), min(math.ceil(xy[:, 0].max()), nx)
    if y1 <= y0 or x1 <= x0:
        return y0, x0, np.zeros((0, 0), bool)
    a, b = xy, np.roll(xy, -1, axis=0)
    yc = np.arange(y0, y1)[:, None] + 0.5                               # pixel-centre rows
    ri, ei = np.nonzero((a[:, 1] <= yc) != (b[:, 1] <= yc))             # the edges crossing each row
    xc = a[ei, 0] + (yc[ri, 0] - a[ei, 1]) * (b[ei, 0] - a[ei, 0]) / (b[ei, 1] - a[ei, 1])
    first = np.clip(np.ceil(xc - 0.5) - x0, 0, x1 - x0).astype(np.int64)    # first pixel centre right of the edge
    count = np.zeros((y1 - y0, x1 - x0 + 1), np.int32)
    np.add.at(count, (ri, first), 1)
    return y0, x0, (np.cumsum(count, axis=1)[:, :-1] & 1).astype(bool)


def _paint(shape: tuple[int, int], polygons: list[Polygon], mode: str) -> np.ndarray:
    """Polygons filled largest first, so the smaller of two overlapping ones is on top (file order breaks ties).
    instance: 1-based row number in the file; semantic: class id + 1. 0 is background."""
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    masks = [_inside(xy * (nx, ny), ny, nx) for _, xy in polygons]
    for i in sorted(range(len(polygons)), key=lambda i: -int(masks[i][2].sum())):
        y0, x0, mask = masks[i]
        out[y0:y0 + mask.shape[0], x0:x0 + mask.shape[1]][mask] = i + 1 if mode == "instance" else polygons[i][0] + 1
    return out


class _Planes:
    """One file per z plane. Images: (Y, X) or (3, Y, X) per file, axes z[c]yx. Labels: (Y, X), axes zyx.
    `files` is one (image url, label url or None) per plane; a label url makes this the label form."""

    def __init__(self, io: IO, files: list[tuple[str, str | None]], mode: str | None, classes: int | None):
        self._io, self._files, self._mode, self._classes = io, files, mode, classes
        self._last: tuple[int, np.ndarray] | None = None    # the last plane made: its c planes are read in turn
        image, label = files[0]
        self._file_shape = _header(io, image)[0]
        if mode is not None:
            _polygons(_bytes(io, label), classes, label)        # a file that is not YOLO polygon rows fails at open
            self._file_shape = self._file_shape[:2]
        c = self._file_shape[2:]                # () or (3,)
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape = (len(files),) + c + self._file_shape[:2]
        self.dtype = np.dtype("uint8" if mode is None else "uint16")
        self.access = "plane"           # a y/x region costs its file: one member, staged whole when deflated
        self.voxel_size_nm = None       # JPEG densities describe print size, and the label text has none

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._plane(k)[inner] for k in range(len(self._files))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self.shape[1:], bool)[inner].shape,
                                                            self.dtype)
        return self._plane(range(len(self._files))[z])[inner]   # IndexError when out of range, like numpy

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (3, Y, X), checked against the array's shape."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        image, label = self._files[k]
        if label is not None:
            polygons = _polygons(_bytes(self._io, label), self._classes, label)
            shape, orientation = _header(self._io, image)
            shape = shape[:2]
            if polygons and orientation not in (None, 1):
                raise ValueError(f"{label}: {image} has Exif orientation {orientation}, so its stored pixels are "
                                 "turned against the picture the polygons were drawn on; this snippet cannot "
                                 "tell which of the two the coordinates mean")
            a = _paint(shape, polygons, self._mode) if shape == self._file_shape else None
        else:
            data = _bytes(self._io, image)
            shape, _ = _header(self._io, image, data)           # refused before decoding when it is another shape
            a = np.asarray(imagecodecs.jpeg_decode(data)) if shape == self._file_shape else None
            if a is not None and (a.shape != shape or a.dtype != self.dtype):
                raise ValueError(f"{image}: decoded as {a.dtype} {a.shape}, its header says {shape}")
        if a is None:
            raise ValueError(f"{image}: {shape}, not the first file's {self._file_shape}; all images of one array "
                             "must have one shape")
        a = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _container(url: str) -> str:
    """The zip (or directory) part of `<container>::<member or glob>`."""
    i = url.find("::", max(url.find("/", url.find("://") + 3), 0))
    if i < 0:
        raise ValueError(f"{url}: a url is `<zip url>::<member or glob>`")
    return url[:i]


def _expand(urls: list[str], io: IO) -> list[str]:
    """Each glob url as its matching files, sorted by name as `io.expand` sorts them; urls in the given order."""
    out = [u for url in urls for u in io.expand(url)]
    if not out:
        raise ValueError("the urls match no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def _by_stem(urls: list[str], what: str) -> dict[tuple[str, str], str]:
    """Files by (container, name without folder and extension); two files of one stem raise."""
    out: dict[tuple[str, str], str] = {}
    for url in urls:
        key = (_container(url), url.rpartition("/")[2].rpartition("::")[2].rpartition(".")[0])
        if not key[1]:
            raise ValueError(f"{url}: {what} needs a name and an extension to be paired by")
        if out.setdefault(key, url) != url:
            raise ValueError(f"{url} and {out[key]} have the same stem; {what}s are paired by stem")
    return out


def _pairs(urls: tuple[str, ...], images: str, io: IO) -> list[tuple[str, str | None]]:
    """(image, label file) per plane: the images `images` matches in each container the urls name, in name
    order, each with the label file of its stem. An image without a label file, or the reverse, raises."""
    labels = _by_stem(_expand(list(urls), io), "a label file")
    containers = list(dict.fromkeys(c for c, _ in labels))
    jpegs = _by_stem(_expand([f"{c}::{images}" for c in containers], io), "an image")
    if labels.keys() != jpegs.keys():
        lone = sorted(labels[k] for k in labels.keys() - jpegs.keys())
        bare = sorted(jpegs[k] for k in jpegs.keys() - labels.keys())
        raise ValueError(f"params.images {images!r} and the urls do not pair one to one by stem: {len(lone)} "
                         f"label file(s) without an image {lone[:3]}, {len(bare)} image(s) without a label file "
                         f"{bare[:3]}")
    return [(jpeg, labels[key]) for key, jpeg in jpegs.items()]


def open_array(src: Source, io: IO) -> Array:
    mode, classes, images = src.params.get("polygons"), src.params.get("classes"), src.params.get("images")
    if mode is None:
        if classes is not None or images is not None:
            raise ValueError("params.classes and params.images belong to the label form; set params.polygons "
                             f"({' or '.join(MODES)}) with them, or drop them for images")
        return _Planes(io, [(u, None) for u in _expand(list(src.urls), io)], None, None)
    if mode not in MODES:
        raise ValueError(f"params.polygons must be one of {MODES}, got {mode!r}")
    if isinstance(classes, bool) or not isinstance(classes, int) or not 1 <= classes <= 65535:
        raise ValueError(f"params.classes must be the number of classes (class ids 0..classes-1), got {classes!r}")
    if not isinstance(images, str) or not images or "::" in images:
        raise ValueError("params.images must be the member pattern of the JPEGs the label files were written "
                         f"for, such as 'images/*.jpg', got {images!r}")
    return _Planes(io, _pairs(src.urls, images, io), mode, classes)
