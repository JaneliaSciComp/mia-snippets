"""8-bit JPEG images, or the YOLO box tables (`.txt`) that annotate them, chosen by glob (zip members, an HTTP
directory) or listed one by one, stacked along z, one file per plane.

An image keeps its samples: an RGB JPEG is three `c` planes. A YOLO file holds one row per box, `class x_center
y_center width height`, the four numbers as fractions of the image's width and height. It has no size of its own,
so it becomes a label plane on the grid of the JPEG it annotates (`labels/<stem>.txt` -> `images/<stem>.jpg`, or the
JPEG beside it), read from that JPEG's header, with every box filled: by its 1-based row number (`boxes:
instance`) or by its class id + 1 (`boxes: semantic`). A file without rows is an all-background plane.
`params.boxes` says the files are box tables; without it they are images. `open_array` reads the first file (for
box tables: the first table and its JPEG's header) without decoding pixels; `read` gets the rest. An array has
one shape: `skip` leaves files of another shape out of a glob, to be bound as another array.
"""
import math
import re

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
HEAD = 1 << 16      # bytes of a JPEG read for its header before the whole file is asked for
SNAP = 1e-6         # pixels: a box edge this close to a whole pixel is that pixel edge (float noise of the fractions)

Row = tuple[int, float, float, float, float]    # class id, x_center, y_center, width, height (fractions)


def _bytes(io: IO, url: str, limit: int = -1) -> bytes:
    """The file's first `limit` bytes, or all of it."""
    try:
        with io.open(url) as f:
            return f.read(limit)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        with io.fetch(url).open("rb") as f:
            return f.read(limit)


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
    """((Y, X) or (Y, X, 3), Exif orientation or None) from the segments before the scan; nothing is decoded.
    None when `data` ends before the start-of-frame segment (a longer piece of the file is needed)."""
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
            if bits != 8 or ns not in (1, 3) or not ny or not nx:
                raise ValueError(f"{url}: a {bits}-bit {nx}x{ny} JPEG with {ns} components; this snippet reads "
                                 "8-bit grey or 3-component JPEGs only")
            return ((ny, nx) if ns == 1 else (ny, nx, ns)), orientation
        if marker == 0xDA:                              # start of scan: no frame header came before it
            break
        pos += 2 + length
    if pos + 4 > len(data):
        return None
    raise ValueError(f"{url}: no JPEG start-of-frame segment found")


def _image_header(io: IO, url: str, data: bytes | None = None) -> tuple[tuple[int, ...], int | None]:
    """A JPEG's shape and Exif orientation: from `data`, the whole file, when the caller has it; otherwise from
    the file's first HEAD bytes, and from all of it only when they do not reach the start of frame."""
    for limit in (HEAD, -1) if data is None else (None,):
        found = _jpeg_header(_bytes(io, url, limit) if data is None else data, url)
        if found is not None:
            return found
    raise ValueError(f"{url}: the file ends before a JPEG start-of-frame segment")


def _rows(data: bytes, url: str) -> list[Row]:
    """The rows of a YOLO box table, in file order; [] for a file without rows."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not a YOLO text file: header={data[:4]!r}") from None
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        try:
            if len(fields) != 5 or not re.fullmatch(r"\d+", fields[0]):
                raise ValueError
            row = (int(fields[0]), *(float(f) for f in fields[1:]))
            if not all(math.isfinite(v) for v in row[1:]):
                raise ValueError
        except ValueError:
            raise ValueError(f"{url} line {number}: not a YOLO box row 'class x_center y_center width height' "
                             f"(a whole class id and four numbers): {line[:80]!r}") from None
        rows.append(row)
    return rows


def _span(centre: float, extent: float, n: int) -> tuple[int, int]:
    """The whole pixels [lo, hi) of an axis of n pixels that a box edge to edge covers: an edge within SNAP of a
    pixel edge is that edge, any other is moved outwards."""
    lo, hi = (centre - extent / 2) * n, (centre + extent / 2) * n
    return (round(lo) if abs(lo - round(lo)) <= SNAP else math.floor(lo),
            round(hi) if abs(hi - round(hi)) <= SNAP else math.ceil(hi))


def _paint(shape: tuple[int, int], rows: list[Row], mode: str, classes: list[str], url: str) -> np.ndarray:
    """Boxes filled over pixels [y0, y1) x [x0, x1), largest first, so the smaller of two overlapping boxes is on
    top (file order breaks ties). instance: 1-based row number in the file; semantic: class id + 1. 0 is
    background."""
    if len(rows) > 65535:
        raise ValueError(f"{url}: {len(rows)} boxes exceed uint16 ids")
    ny, nx = shape
    boxes = []
    for i, (cls, xc, yc, w, h) in enumerate(rows):
        if cls >= len(classes):
            raise ValueError(f"{url} box {i + 1}: class id {cls} is not in params.classes {classes} (ids 0 to "
                             f"{len(classes) - 1})")
        (x0, x1), (y0, y1) = _span(xc, w, nx), _span(yc, h, ny)
        if not (w > 0 and h > 0 and 0 <= x0 < x1 <= nx and 0 <= y0 < y1 <= ny):
            raise ValueError(f"{url} box {i + 1}: centre ({xc}, {yc}) size ({w}, {h}) is x {x0}..{x1} y {y0}..{y1} "
                             f"px, empty or outside the {nx}x{ny} image")
        boxes.append((-(x1 - x0) * (y1 - y0), i, cls, x0, y0, x1, y1))
    out = np.zeros(shape, np.uint16)
    for _, i, cls, x0, y0, x1, y1 in sorted(boxes):
        out[y0:y1, x0:x1] = i + 1 if mode == "instance" else cls + 1
    return out


def _image_url(url: str, ext: str) -> str:
    """The JPEG a box table annotates: `<stem>.<ext>` in the sibling folder `images` when the table is in a folder
    named `labels` (the YOLO dataset layout), else beside the table."""
    head, slash, name = url.rpartition("/")
    stem, dot, _ = name.rpartition(".")
    if not slash or not dot or not stem:
        raise ValueError(f"{url}: a box table needs a folder and an extension to find its image by")
    if head.endswith(("/labels", "::labels")):
        head = head[:-len("labels")] + "images"
    return f"{head}/{stem}.{ext}"


class _Planes:
    """One file per z plane. Images: (Y, X) or (3, Y, X) per file, axes z[c]yx. Box tables: (Y, X), axes zyx."""

    def __init__(self, io: IO, urls: list[str], boxes: str | None, classes: list[str] | None, image_ext: str):
        self._io, self._urls, self._boxes, self._classes, self._ext = io, urls, boxes, classes, image_ext
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded file: its c planes are read in turn
        if boxes is not None:
            _rows(_bytes(io, urls[0]), urls[0])             # a file that is not a box table fails at open
            image = _image_url(urls[0], image_ext)
            self._file_shape = _image_header(io, image)[0][:2]
        else:
            self._file_shape = _image_header(io, urls[0])[0]
        c = self._file_shape[2:]                # () or (3,)
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape = (len(urls),) + c + self._file_shape[:2]
        self.dtype = np.dtype("uint8" if boxes is None else "uint16")
        self.access = "plane"           # a y/x region costs its file: one member, staged whole when deflated
        self.voxel_size_nm = None       # JPEG densities describe print size, and a YOLO table has none

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._file(k)[inner] for k in range(len(self._urls))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self.shape[1:], bool)[inner].shape,
                                                            self.dtype)
        return self._file(range(len(self._urls))[z])[inner]     # IndexError when out of range, like numpy

    def _file(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (3, Y, X), checked against the array's shape."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        data = _bytes(self._io, url)
        if self._boxes is not None:
            rows = _rows(data, url)
            image = _image_url(url, self._ext)
            shape, orientation = _image_header(self._io, image)
            shape = shape[:2]
            if rows and orientation not in (None, 1):
                raise ValueError(f"{url}: {image} has Exif orientation {orientation}, so its stored pixels are "
                                 "turned against the picture the boxes were drawn on; this snippet cannot tell "
                                 "which of the two the box coordinates mean")
            a = _paint(shape, rows, self._boxes, self._classes, url) if shape == self._file_shape else None
        else:
            shape, _ = _image_header(self._io, url, data)   # refused before decoding when it is another shape
            a = np.asarray(imagecodecs.jpeg_decode(data)) if shape == self._file_shape else None
            if a is not None and (a.shape != shape or a.dtype != self.dtype):
                raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, its header says {shape}")
        if a is None:
            raise ValueError(f"{url}: {'the image of this file is ' if self._boxes is not None else ''}{shape}, "
                             f"not the first file's {self._file_shape}; all files of one array must agree, bind "
                             "the files of another shape as another array")
        a = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _stem_key(url: str) -> tuple[str, str]:
    """A file's url without its extension, then the extension: `a.rf.1.jpg`, `a.jpg` and `a.rf.1.txt`, `a.txt`
    sort alike, so an image glob and a box table glob over the same stems pair plane for plane."""
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


def _select(urls: list[str], skip: object) -> list[str]:
    """The urls without the files `skip` names by stem (file name without its extension). Every stem listed must be
    exactly one file's, so a file that is missing or renamed is not passed over."""
    if skip is None:
        return urls
    if not (isinstance(skip, list) and skip and len(set(skip)) == len(skip)
            and all(isinstance(s, str) and s for s in skip)):
        raise ValueError(f"params.skip must be a list of distinct file stems, got {skip!r}")
    stems = [u.rpartition("::")[2].rpartition("/")[2].rpartition(".")[0] for u in urls]
    if wrong := [s for s in skip if stems.count(s) != 1]:
        raise ValueError(f"params.skip: {wrong[:5]}{' ...' if len(wrong) > 5 else ''} each name no file or more "
                         "than one among the urls (a stem is the file name without its extension)")
    urls = [u for u, s in zip(urls, stems) if s not in skip]
    if not urls:
        raise ValueError("params.skip leaves no file")
    return urls


def open_array(src: Source, io: IO) -> Array:
    boxes, classes, ext = src.params.get("boxes"), src.params.get("classes"), src.params.get("image_ext")
    if boxes is None and (classes is not None or ext is not None):
        raise ValueError("params.classes and params.image_ext apply to box tables only: set params.boxes "
                         f"({' or '.join(BOX_MODES)}) when the urls are YOLO .txt files")
    if boxes is not None and boxes not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {boxes!r}")
    if boxes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                  and all(isinstance(c, str) and c for c in classes)):
        raise ValueError("params.boxes needs params.classes, the list of distinct class names in YOLO class id "
                         f"order (data.yaml `names`), got {classes!r}")
    ext = "jpg" if ext is None else ext
    if not (isinstance(ext, str) and re.fullmatch(r"\w+", ext)):
        raise ValueError(f"params.image_ext must be a file extension without the dot, got {ext!r}")
    return _Planes(io, _select(_expand(src.urls, io), src.params.get("skip")), boxes, classes, ext)
