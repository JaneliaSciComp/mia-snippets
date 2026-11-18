"""8-bit JPEG images, or the YOLO box tables that belong to them, from zip members or an HTTP directory chosen by
glob (or plain URLs), stacked along z, one image per plane.

An image keeps its samples: an RGB JPEG is three `c` planes. A YOLO box table is a text file with one row per
box, `class x_center y_center width height`, the four numbers as fractions of the image width and height. It
belongs to the image with the same file name stem, wherever the two files are (often different zips of a
dataset published in parts), and becomes a label plane on that image's grid, read from the JPEG's header, with
every box filled: by its 1-based row number (`boxes: instance`) or by its class id + 1 (`boxes: semantic`).
The images are returned as stored. When a JPEG has an Exif orientation, `drawn_on` says whether its boxes are
fractions of the picture as displayed (they are turned back onto the stored pixels) or of the stored pixels.

The urls of a label array are the image urls of its raw, in the raw's order, plus the urls of the box tables
(file names ending `.txt`): z follows the images, so plane k of the labels is the table of image k of the raw.
`open_array` reads the listings and the first image's header (and the first table); `read` gets the rest.
"""
import math
import re

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
FRAMES = ("displayed", "stored")
# Exif orientation -> (swap, flip x, flip y): how a point (u, v) of the displayed picture, as fractions of its
# width and height, lies in the stored image: x = v if swap else u, y = u if swap else v, then 1 - x / 1 - y
TURNS = {1: (False, False, False), 2: (False, True, False), 3: (False, True, True), 4: (False, False, True),
         5: (True, False, False), 6: (True, False, True), 7: (True, True, True), 8: (True, True, False)}
TABLE_EXT = ".txt"      # a url whose file name ends so is a box table; every other url is an image
EDGE = 1e-6             # pixels: a box edge this close to a pixel boundary is on it (the fractions are rounded)
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Row = tuple[int, float, float, float, float]        # class id, x_center, y_center, width, height (fractions)


def _bytes(io: IO, url: str) -> bytes:
    try:
        with io.open(url) as f:
            return f.read()
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


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


def _jpeg_header(data: bytes, url: str) -> tuple[tuple[int, ...], int | None]:
    """((Y, X) or (Y, X, 3), Exif orientation or None) from the segments before the scan; nothing is decoded."""
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
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00" and orientation is None:
            orientation = _orientation(data[pos + 10:pos + 2 + length])
        if marker in SOF and pos + 10 <= len(data):
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8 or ns not in (1, 3):
                raise ValueError(f"{url}: a {bits}-bit JPEG with {ns} components; this snippet reads 8-bit grey "
                                 "or 3-component JPEGs only")
            return ((ny, nx) if ns == 1 else (ny, nx, ns)), orientation
        if marker == 0xDA:                              # start of scan: no frame header came before it
            break
        pos += 2 + length
    raise ValueError(f"{url}: no JPEG start-of-frame segment found")


def _rows(data: bytes, url: str) -> list[Row]:
    """The rows of a YOLO box table, in file order; [] for an empty file (an image without boxes)."""
    try:
        text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not a text file, so not a YOLO box table") from None
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 5 or not re.fullmatch(r"\d+", fields[0]):
            raise ValueError(f"{url} line {n}: {line[:80]!r} is not a YOLO box row 'class x_center y_center width "
                             "height' (a row with more numbers is a polygon, which this snippet does not read)")
        try:
            xc, yc, w, h = (float(f) for f in fields[1:])
        except ValueError:
            raise ValueError(f"{url} line {n}: {line[:80]!r} has a field that is not a number") from None
        if not all(math.isfinite(v) for v in (xc, yc, w, h)):
            raise ValueError(f"{url} line {n}: {line[:80]!r} has a field that is not a finite number")
        rows.append((int(fields[0]), xc, yc, w, h))
    return rows


def _paint(shape: tuple[int, int], rows: list[Row], mode: str, classes: list[str], turn: tuple[bool, bool, bool],
           url: str) -> np.ndarray:
    """Boxes filled, largest first, so the smaller of two overlapping boxes is on top (file order breaks ties).
    A box covers every pixel it touches: its edges, in pixels, are moved outwards to pixel boundaries. instance:
    1-based row number in the file; semantic: class id + 1. 0 is background. `turn` (TURNS) takes the boxes from
    the displayed picture to the stored image, whose shape `shape` is."""
    swap, flip_x, flip_y = turn
    if len(rows) > 65535:
        raise ValueError(f"{url}: {len(rows)} boxes exceed uint16 ids")
    ny, nx = shape
    boxes = []
    for i, (cls, xc, yc, w, h) in enumerate(rows):
        if cls >= len(classes):
            raise ValueError(f"{url}: box {i + 1} has class id {cls}, but params.classes names ids "
                             f"0..{len(classes) - 1} only")
        if swap:
            xc, yc, w, h = yc, xc, h, w
        xc, yc = 1 - xc if flip_x else xc, 1 - yc if flip_y else yc
        x0, x1 = math.floor((xc - w / 2) * nx + EDGE), math.ceil((xc + w / 2) * nx - EDGE)
        y0, y1 = math.floor((yc - h / 2) * ny + EDGE), math.ceil((yc + h / 2) * ny - EDGE)
        if not (w > 0 and h > 0 and 0 <= x0 < x1 <= nx and 0 <= y0 < y1 <= ny):
            raise ValueError(f"{url}: box {i + 1} (class {cls}) is empty or reaches outside the {nx}x{ny} image: "
                             f"stored pixels x {x0}..{x1}, y {y0}..{y1}")
        boxes.append((-(x1 - x0) * (y1 - y0), i, cls, x0, y0, x1, y1))
    out = np.zeros(shape, np.uint16)
    for _, i, cls, x0, y0, x1, y1 in sorted(boxes):
        out[y0:y1, x0:x1] = i + 1 if mode == "instance" else cls + 1
    return out


class _Planes:
    """One image per z plane. Images: (Y, X) or (3, Y, X) per file, axes z[c]yx. Box tables: (Y, X), axes zyx."""

    def __init__(self, io: IO, images: list[str], tables: list[str] | None, boxes: str | None,
                 classes: list[str] | None, drawn_on: str | None):
        self._io, self._images, self._tables, self._boxes, self._classes = io, images, tables, boxes, classes
        self._drawn_on = drawn_on
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded file: its c planes are read in turn
        self._file_shape = _jpeg_header(_bytes(io, images[0]), images[0])[0]    # every image must have this shape
        if tables is not None:
            _rows(_bytes(io, tables[0]), tables[0])         # a first file that is not a box table fails at open
        c = () if tables is not None else self._file_shape[2:]     # () or (3,)
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape = (len(images),) + c + self._file_shape[:2]
        self.dtype = np.dtype("uint8" if tables is None else "uint16")
        self.access = "plane"           # a y/x region costs its file: one member, staged whole when deflated
        self.voxel_size_nm = None       # JPEG densities describe print size, and a box table has none

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._file(k)[inner] for k in range(len(self._images))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self.shape[1:], bool)[inner].shape,
                                                            self.dtype)
        return self._file(range(len(self._images))[z])[inner]   # IndexError when out of range, like numpy

    def _file(self, k: int) -> np.ndarray:
        """Plane k as (Y, X) or (3, Y, X), its image checked against the array's shape."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        image = self._images[k]
        data = _bytes(self._io, image)
        shape, orientation = _jpeg_header(data, image)      # refused before decoding when it is another shape
        if shape != self._file_shape:
            raise ValueError(f"{image}: {shape}, not the first image's {self._file_shape}; all images of one "
                             "array must agree")
        if self._tables is None:
            a = np.asarray(imagecodecs.jpeg_decode(data))
            if a.shape != shape or a.dtype != self.dtype:
                raise ValueError(f"{image}: decoded as {a.dtype} {a.shape}, its header says {shape}")
            a = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        else:
            table = self._tables[k]
            rows = _rows(_bytes(self._io, table), table)
            turned = orientation not in (None, 1)
            if rows and turned and self._drawn_on is None:
                raise ValueError(f"{table}: {image} has Exif orientation {orientation}, so its stored pixels are "
                                 "turned against the picture a viewer shows; set params.drawn_on to say which of "
                                 f"the two the box fractions are of ({' or '.join(FRAMES)})")
            if rows and turned and self._drawn_on == "displayed" and orientation not in TURNS:
                raise ValueError(f"{image}: Exif orientation {orientation} is not one of 1..8")
            turn = TURNS[orientation if turned and self._drawn_on == "displayed" else 1]
            a = _paint(shape[:2], rows, self._boxes, self._classes, turn, table)
        self._last = (k, a)
        return a


def _stem(url: str) -> str:
    """A file's name without its folders and extension: what pairs a box table with its image."""
    name = url.rpartition("::")[2].rpartition("/")[2]
    return name.rpartition(".")[0] or name


def _is_table(url: str) -> bool:
    return url.rpartition("::")[2].rpartition("/")[2].lower().endswith(TABLE_EXT)


def _expand(urls: tuple[str, ...], io: IO) -> tuple[list[str], list[str]]:
    """(image urls, box table urls). Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching
    files, sorted by name; the urls stay in binding order."""
    out = [u for url in urls for u in io.expand(url)]
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    images, tables = [u for u in out if not _is_table(u)], [u for u in out if _is_table(u)]
    if not images:
        raise ValueError("the urls name no image: a label array's urls are the image urls of its raw, in the "
                         f"raw's order, and the urls of the {TABLE_EXT} box tables")
    return images, tables


def _pair(images: list[str], tables: list[str]) -> list[str]:
    """The box table of each image, in the images' order, found by file name stem. Every image must have exactly
    one table and every table an image, so a file that went missing is not passed over."""
    by_stem: dict[str, str] = {}
    for url in tables:
        if by_stem.setdefault(_stem(url), url) != url:
            raise ValueError(f"{url} and {by_stem[_stem(url)]} are two box tables with one stem")
    stems = [_stem(url) for url in images]
    if len(set(stems)) != len(stems):
        twice = sorted({s for s in stems if stems.count(s) > 1})[:5]
        raise ValueError(f"several images have the same file name stem, so a box table cannot be paired by it: "
                         f"{twice}")
    if missing := [u for u, s in zip(images, stems) if s not in by_stem]:
        raise ValueError(f"{len(missing)} image(s) have no {TABLE_EXT} box table of the same stem among the urls, "
                         f"e.g. {missing[:3]}")
    if extra := sorted(set(by_stem) - set(stems)):
        raise ValueError(f"{len(extra)} box table(s) have no image of the same stem among the urls, e.g. "
                         f"{[by_stem[s] for s in extra[:3]]}")
    return [by_stem[s] for s in stems]


def open_array(src: Source, io: IO) -> Array:
    boxes, classes, count = src.params.get("boxes"), src.params.get("classes"), src.params.get("count")
    drawn_on = src.params.get("drawn_on")
    if drawn_on is not None and drawn_on not in FRAMES:
        raise ValueError(f"params.drawn_on must be one of {FRAMES}, got {drawn_on!r}")
    if boxes is not None and boxes not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {boxes!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    images, tables = _expand(src.urls, io)
    if count is not None and (isinstance(count, bool) or count != len(images)):
        raise ValueError(f"params.count is {count!r}, but the urls name {len(images)} images")
    if not tables:
        if boxes is not None or classes is not None or drawn_on is not None:
            raise ValueError(f"the urls name no {TABLE_EXT} box table: params.boxes, params.classes and "
                             "params.drawn_on apply to a label array, whose urls are its raw's image urls and the "
                             "box tables' urls")
        return _Planes(io, images, None, None, None, None)
    if boxes is None or classes is None:
        raise ValueError(f"the urls name {TABLE_EXT} box tables: set params.boxes ({' or '.join(BOX_MODES)}) and "
                         "params.classes (the class names, in class id order from 0)")
    return _Planes(io, images, _pair(images, tables), boxes, classes, drawn_on)
