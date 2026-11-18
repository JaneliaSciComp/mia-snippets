"""A set of 2D PNG images (grey or RGB) or of YOLO box annotations (one text file per image), chosen by glob
from zip members or an HTTP directory (or listed one by one), stacked along z, one file per plane.

Images keep their samples: an RGB file is three `c` planes. A YOLO file holds rows
`<class> <x_center> <y_center> <width> <height>`, all four numbers fractions of the image size, and no size of
its own, so it becomes a label plane on the grid of the PNG beside it (same name, extension `.png`), with every
box filled: by its 1-based row number in the file (`boxes: instance`) or by its class id + 1 (`boxes: semantic`).
The kind is taken from the first file's leading bytes, not from its name. `open_array` reads headers only (the
first file's, and for annotations the first 26 bytes of its PNG; a deflated member is staged whole to get
them, still without decoding pixels); `read` fetches the files it needs.
"""
import math
import re
from pathlib import Path
from typing import BinaryIO

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_HEAD = 26                               # signature, IHDR length and tag, width, height, bit depth, colour type
PNG_SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}      # colour type -> samples per pixel (3, palette, is refused)
CLASS_ID = re.compile(r"[0-9]+")
NUMBER = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?")

Row = tuple[int, float, float, float, float]    # class id, x centre, y centre, width, height (fractions)


def _source(io: IO, url: str) -> BinaryIO | Path:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url)


def _bytes(io: IO, url: str, n: int | None = None) -> bytes:
    """The file's first `n` bytes, or all of it."""
    src = _source(io, url)
    if isinstance(src, Path):
        with src.open("rb") as f:
            return f.read() if n is None else f.read(n)
    with src:
        return src.read() if n is None else src.read(n)


def _png_header(head: bytes, url: str) -> tuple[tuple[int, ...], np.dtype]:
    """(Y, X) or (Y, X, S) and the dtype, from the IHDR chunk; nothing is decoded."""
    if head[:8] != PNG_MAGIC or len(head) < PNG_HEAD or head[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={head[:8]!r}")
    nx, ny = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    bits, colour = head[24], head[25]
    if colour not in PNG_SAMPLES or bits not in (8, 16):
        raise ValueError(f"{url}: a palette or 1/2/4-bit PNG (bit depth {bits}, colour type {colour}); the decoder "
                         "would replace the stored indices by palette colours or rescale them, so this snippet "
                         "does not read it")
    if ny <= 0 or nx <= 0:
        raise ValueError(f"{url}: IHDR size {nx}x{ny} is not an image size")
    s = PNG_SAMPLES[colour]
    return ((ny, nx) if s == 1 else (ny, nx, s)), np.dtype("uint8" if bits == 8 else "uint16")


def _image_url(url: str) -> str:
    """The PNG beside an annotation file: the same url with its last extension replaced by `.png`."""
    head, sep, name = url.rpartition("/")
    stem, dot, _ = name.rpartition(".")
    if not dot:
        raise ValueError(f"{url}: an annotation file without an extension; its image is <stem>.png beside it")
    return f"{head}{sep}{stem}.png"


def _grid(io: IO, url: str) -> tuple[int, int]:
    """(Y, X) of the PNG beside annotation file `url`, from its IHDR."""
    image = _image_url(url)
    try:
        head = _bytes(io, image, PNG_HEAD)
    except (KeyError, FileNotFoundError) as e:
        raise ValueError(f"{url}: its boxes are fractions of the image size, and the image {image} beside it "
                         f"cannot be opened: {e}") from None
    return _png_header(head, image)[0][:2]


def _rows(data: bytes, url: str, n_classes: int) -> list[Row]:
    """The rows of a YOLO detection file, in file order. Fields are separated by tabs or spaces; a CR before
    the newline, blank lines and a UTF-8 BOM are ignored. An empty file has no rows."""
    try:
        text = data.removeprefix(b"\xef\xbb\xbf").decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: neither a PNG nor a YOLO text file: header={data[:8]!r}") from None
    rows = []
    for n, line in enumerate(text.split("\n"), 1):
        f = line.split()
        if not f:
            continue
        if len(f) != 5:
            raise ValueError(f"{url} line {n}: {len(f)} fields, not the five of a YOLO box row '<class> <x_center> "
                             f"<y_center> <width> <height>' (polygon and oriented-box rows are not read): "
                             f"{line[:80]!r}")
        if not CLASS_ID.fullmatch(f[0]) or not all(NUMBER.fullmatch(v) for v in f[1:]):
            raise ValueError(f"{url} line {n}: not a class id and four numbers: {line[:80]!r}")
        cls, (xc, yc, w, h) = int(f[0]), (float(v) for v in f[1:])
        if cls >= n_classes:
            raise ValueError(f"{url} line {n}: class id {cls} has no name in params.classes ({n_classes} names, "
                             f"for ids 0..{n_classes - 1})")
        if not (0 <= xc <= 1 and 0 <= yc <= 1 and 0 <= w <= 1 and 0 <= h <= 1):
            raise ValueError(f"{url} line {n}: centre ({xc}, {yc}) or size ({w}, {h}) is not a fraction of the "
                             "image; YOLO rows are normalised to 0..1")
        rows.append((cls, xc, yc, w, h))
    return rows


def _edge(v: float, n: int) -> int:
    """A box edge as a pixel boundary: the nearest one (halves round up), kept inside the image."""
    return min(max(math.floor(v + 0.5), 0), n)


def _paint(shape: tuple[int, int], rows: list[Row], mode: str, url: str) -> np.ndarray:
    """Boxes filled over pixels [y0, y1) x [x0, x1), largest first, so the smaller of two overlapping boxes is
    on top (file order breaks ties). instance: 1-based row number in the file; semantic: class id + 1. 0 is
    background. A row of width or height 0 is a point or a line, not a box: it paints nothing and keeps its
    row number."""
    ny, nx = shape
    if len(rows) > 65535:
        raise ValueError(f"{url}: {len(rows)} boxes exceed uint16 ids")
    boxes = []
    for i, (cls, xc, yc, w, h) in enumerate(rows):
        if w == 0 or h == 0:
            continue
        x0, x1 = _edge((xc - w / 2) * nx, nx), _edge((xc + w / 2) * nx, nx)
        y0, y1 = _edge((yc - h / 2) * ny, ny), _edge((yc + h / 2) * ny, ny)
        if x0 >= x1 or y0 >= y1:
            raise ValueError(f"{url}: box {i + 1} (class {cls}, centre {xc}, {yc}, size {w}, {h}) covers no "
                             f"pixel of the {nx}x{ny} image")
        boxes.append((i + 1 if mode == "instance" else cls + 1, x0, y0, x1, y1))
    out = np.zeros(shape, np.uint16)
    for value, x0, y0, x1, y1 in sorted(boxes, key=lambda b: -(b[3] - b[1]) * (b[4] - b[2])):
        out[y0:y1, x0:x1] = value
    return out


class _Planes:
    """One file per plane. PNG: (Y, X) or (S, Y, X) per file, axes [z][c]yx. YOLO: (Y, X) per file, axes [z]yx."""

    def __init__(self, io: IO, urls: list[str], boxes: str | None, classes: list[str] | None):
        self._io, self._urls, self._boxes, self._classes = io, urls, boxes, classes
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded file: its c planes are read in turn
        self._yolo = _bytes(io, urls[0], len(PNG_MAGIC)) != PNG_MAGIC
        if self._yolo:
            if boxes not in BOX_MODES or not classes:
                raise ValueError(f"{urls[0]} is not a PNG, so it is read as a YOLO annotation: set params.boxes "
                                 f"({' or '.join(BOX_MODES)}) and params.classes (the class names, in id order)")
            _rows(_bytes(io, urls[0]), urls[0], len(classes))   # refuses a first file that is not YOLO text
            self._file_shape: tuple[int, ...] = _grid(io, urls[0])
            self._file_dtype = np.dtype("uint16")
        else:
            if boxes is not None or classes is not None:
                raise ValueError(f"{urls[0]} is a PNG image: params.boxes and params.classes apply to YOLO "
                                 "annotation files only")
            self._file_shape, self._file_dtype = _png_header(_bytes(io, urls[0], PNG_HEAD), urls[0])
        ny, nx = self._file_shape[:2]
        c = self._file_shape[2:]                # () or (S,)
        many = len(urls) > 1
        self.axes = ("z" if many else "") + ("c" if c else "") + "yx"
        self.shape = ((len(urls),) if many else ()) + c + (ny, nx)
        self.dtype = self._file_dtype
        self.access = "plane"           # a y/x region costs its file, read by range or staged whole
        self.voxel_size_nm = None       # YOLO text has none; a PNG's pHYs chunk describes print size, not pixels

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        if "z" not in self.axes:
            return self._file(0)[inner]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._file(range(len(self._urls))[z])[inner]     # IndexError when out of range, like numpy
        planes = [self._file(k)[inner] for k in range(len(self._urls))[z]]
        if not planes:
            return np.empty((0,) + np.empty(self.shape[1:], self.dtype)[inner].shape, self.dtype)
        return np.stack(planes)

    def _file(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (S, Y, X), checked against the first file."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        data = _bytes(self._io, url)
        if (data[:len(PNG_MAGIC)] != PNG_MAGIC) != self._yolo:
            raise ValueError(f"{url} is {'a PNG' if self._yolo else 'not a PNG'} among "
                             f"{'YOLO annotation' if self._yolo else 'PNG'} files; one array holds images or "
                             "annotations, not both")
        if self._yolo:
            rows = _rows(data, url, len(self._classes))
            shape: tuple[int, ...] = _grid(self._io, url)
            a = _paint(shape, rows, self._boxes, url) if shape == self._file_shape else None
        else:
            shape, dtype = _png_header(data[:PNG_HEAD], url)
            a = np.asarray(imagecodecs.png_decode(data)) if (shape, dtype) == (self._file_shape, self.dtype) else None
            if a is not None and (a.shape != shape or a.dtype != dtype):
                raise ValueError(f"{url}: decoded to {a.dtype} {a.shape}, not the {dtype} {shape} its IHDR "
                                 "declares (a tRNS transparency chunk adds a sample); this snippet does not read it")
        if a is None:
            raise ValueError(f"{url}: {shape} differs from the first file's {self._file_dtype} "
                             f"{self._file_shape}; all files of one array must agree, bind the others as "
                             "another array")
        a = np.ascontiguousarray(np.moveaxis(a, 2, 0)) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _stem_key(url: str) -> tuple[str, str]:
    """A file's url without its extension, then the extension: `a.png`, `a.b.png` and `a.txt`, `a.b.txt` sort
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
