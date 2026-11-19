"""YOLO detection label files (`class x_center y_center width height` per row, normalised to the image), each
rasterised on the pixel grid of an image that the binding names by URL, one file per plane.

YOLO coordinates are fractions of the image's width and height, and the txt does not hold either, so a label
plane needs its image. Where the two are files of one name in one directory a snippet can find the image itself;
here `params.images` lists it, one URL per label file, which is what a repository that serves every file under
an opaque id needs. Only the JPEG's header is read. Every box is filled: by its 1-based row number in its file
(`boxes: instance`) or by the 1-based position of its class id in `classes` (`boxes: semantic`). A file without
rows is an all-background plane. `open_array` reads the first label file and the head of its image; `read` gets
the rest.
"""
import math
import re

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
HEAD = 1 << 16      # bytes of an image read first for its header; the whole file only when that is not enough
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Row = tuple[int, float, float, float, float]        # class id, x_center, y_center, width, height (normalised)


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


def _jpeg_header(data: bytes, url: str) -> tuple[tuple[int, int], int | None] | None:
    """((Y, X), Exif orientation or None) from the segments before the scan; nothing is decoded. None when
    `data` ends before the start-of-frame segment (the caller reads more of the file)."""
    if data[:3] != b"\xff\xd8\xff":
        raise ValueError(f"{url}: not a JPEG file (header {data[:4]!r}); params.images must name JPEG images")
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
        if pos + 2 + length > len(data) and (marker in SOF or marker == 0xE1):
            return None                                 # the segment is cut off
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00" and orientation is None:
            orientation = _orientation(data[pos + 10:pos + 2 + length])
        if marker in SOF:
            ny, nx = int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 7:pos + 9], "big")
            if ny < 1 or nx < 1:
                raise ValueError(f"{url}: JPEG frame of {nx}x{ny} pixels")
            return (ny, nx), orientation
        if marker == 0xDA:                              # start of scan: no frame header came before it
            raise ValueError(f"{url}: no JPEG start-of-frame segment before the scan")
        pos += 2 + length
    return None


def _grid(io: IO, url: str) -> tuple[tuple[int, int], int | None]:
    """An image's (Y, X) and Exif orientation from its header: its first HEAD bytes, else the whole file."""
    try:
        with io.open(url) as f:
            data = f.read(HEAD)
            found = _jpeg_header(data, url)
            if found is None and len(data) == HEAD:
                found = _jpeg_header(data + f.read(), url)
    except NeedsFetch:
        found = _jpeg_header(io.fetch(url).read_bytes(), url)
    if found is None:
        raise ValueError(f"{url}: no JPEG start-of-frame segment found")
    return found


def _rows(data: bytes, url: str) -> list[Row]:
    """The rows of a YOLO detection label file, in file order; [] for a file that is empty or blank."""
    try:
        text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not a text file (first bytes {data[:8]!r}); urls must name YOLO txt "
                         "label files") from None
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 5:
            raise ValueError(f"{url}: line {n} has {len(parts)} fields, not the 5 of a YOLO detection row "
                             f"'class x_center y_center width height': {line[:80]!r}")
        if not re.fullmatch(r"\d+", parts[0]):
            raise ValueError(f"{url}: line {n}: class {parts[0][:20]!r} is not a whole number")
        try:
            xc, yc, w, h = (float(p) for p in parts[1:])
        except ValueError:
            raise ValueError(f"{url}: line {n}: a coordinate is not a number: {line[:80]!r}") from None
        if not (0 <= xc <= 1 and 0 <= yc <= 1 and 0 < w <= 1 and 0 < h <= 1):   # also refuses nan and inf
            raise ValueError(f"{url}: line {n}: x_center, y_center, width, height must be fractions of the image "
                             f"(0..1, width and height above 0): {line[:80]!r}")
        rows.append((int(parts[0]), xc, yc, w, h))
    return rows


def _paint(shape: tuple[int, int], rows: list[Row], mode: str, classes: list[int], url: str) -> np.ndarray:
    """Boxes filled, largest first, so the smaller of two overlapping boxes is on top (file order breaks ties).
    A box covers the pixels whose centres lie in it, after it is clipped to the image. instance: 1-based row
    number in the file; semantic: 1-based position of the row's class id in `classes`. 0 is background."""
    if len(rows) > 65535:
        raise ValueError(f"{url}: {len(rows)} boxes exceed uint16 ids")
    ny, nx = shape
    boxes = []
    for k, (cls, xc, yc, w, h) in enumerate(rows):
        if cls not in classes:
            raise ValueError(f"{url}: row {k + 1} has class id {cls}, which is not in params.classes {classes}")
        x0, x1 = (min(max(math.floor(v * nx + 0.5), 0), nx) for v in (xc - w / 2, xc + w / 2))
        y0, y1 = (min(max(math.floor(v * ny + 0.5), 0), ny) for v in (yc - h / 2, yc + h / 2))
        if x0 >= x1 or y0 >= y1:
            raise ValueError(f"{url}: row {k + 1} (class {cls}, centre {xc}, {yc}, size {w} x {h}) covers no "
                             f"pixel centre of the {nx}x{ny} image")
        boxes.append((-(x1 - x0) * (y1 - y0), k, x0, y0, x1, y1, cls))
    out = np.zeros(shape, np.uint16)
    for _, k, x0, y0, x1, y1, cls in sorted(boxes):
        out[y0:y1, x0:x1] = k + 1 if mode == "instance" else classes.index(cls) + 1
    return out


class _Planes:
    """One label file per plane, each (Y, X) of its image. Axes `yx` for one file, `zyx` for several."""

    def __init__(self, io: IO, urls: list[str], images: list[str], boxes: str, classes: list[int]):
        self._io, self._urls, self._images, self._boxes, self._classes = io, urls, images, boxes, classes
        self._last: tuple[int, np.ndarray] | None = None
        _rows(_bytes(io, urls[0]), urls[0])                 # a first file that is not YOLO txt fails at open
        self._file_shape = _grid(io, images[0])[0]
        self.axes = "yx" if len(urls) == 1 else "zyx"
        self.shape = self._file_shape if len(urls) == 1 else (len(urls),) + self._file_shape
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # a y/x region costs its label file and the head of its image
        self.voxel_size_nm = None       # neither the txt nor the JPEG header says (JFIF densities are print sizes)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = (region.get("y", slice(None)), region.get("x", slice(None)))
        if self.axes == "yx":
            return self._file(0)[inner]
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._file(k)[inner] for k in range(len(self._urls))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self._file_shape, bool)[inner].shape,
                                                            self.dtype)
        return self._file(range(len(self._urls))[z])[inner]     # IndexError when out of range, like numpy

    def _file(self, k: int) -> np.ndarray:
        """Label file k painted as (Y, X) on its image's grid, which must be the first image's."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url, image = self._urls[k], self._images[k]
        rows = _rows(_bytes(self._io, url), url)
        shape, orientation = _grid(self._io, image)
        if shape != self._file_shape:
            raise ValueError(f"{url}: its image {image} is {shape}, not the first image's {self._file_shape}; "
                             "all planes of one array must agree, bind each shape's files as an array of its own")
        if rows and orientation not in (None, 1):
            raise ValueError(f"{url}: {image} has Exif orientation {orientation}, so its stored pixels are turned "
                             "against the picture the boxes were drawn on; this snippet cannot tell which of the "
                             "two the box coordinates mean")
        a = _paint(shape, rows, self._boxes, self._classes, url)
        self._last = (k, a)
        return a


def _urls(value: object, name: str) -> list[str]:
    urls = list(value) if isinstance(value, (list, tuple)) else None
    if not urls or not all(isinstance(u, str) and u for u in urls):
        raise ValueError(f"{name} must be a list of urls, got {value!r}")
    if len(set(urls)) != len(urls):
        raise ValueError(f"{name} names the same file twice")
    if any(ch in u.rpartition("::")[2] for u in urls if "::" in u for ch in "*?["):
        raise ValueError(f"{name}: globs are not taken; a label file and its image pair by their position in the "
                         "two lists, so each is named one by one")
    return urls


def open_array(src: Source, io: IO) -> Array:
    boxes, classes = src.params.get("boxes"), src.params.get("classes")
    urls, images = _urls(src.urls, "urls"), _urls(src.params.get("images"), "params.images")
    if len(images) != len(urls):
        raise ValueError(f"params.images has {len(images)} urls for {len(urls)} label files; it names the image "
                         "of each label file, in the order of urls")
    if boxes not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {boxes!r}")
    ids = [int(c) if isinstance(c, str) and re.fullmatch(r"\d+", c) else c
           for c in (classes if isinstance(classes, list) else [])]
    if not ids or not all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in ids) \
            or len(set(ids)) != len(ids):
        raise ValueError(f"params.classes must be a list of distinct YOLO class ids (whole numbers from 0), "
                         f"got {classes!r}")
    return _Planes(io, urls, images, boxes, ids)
