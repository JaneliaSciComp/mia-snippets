"""A set of YOLO segmentation label files (one text file per image, each line `class x1 y1 x2 y2 ...` with
coordinates normalised to that image), chosen by glob from zip members or an HTTP directory (or listed one by
one), rasterised on the grid of their images and stacked along z, one file per plane.

A YOLO text file does not say how large its image is, so `params.images` names the images, file for file; each
plane's grid is read from its own image's header (PNG, JPEG or TIFF, no pixel is decoded). Every polygon is
filled: by its 1-based line number in the file (`fill: instance`) or by its YOLO class id + 1 (`fill: class`).
An empty file is an all-background plane. `open_array` expands the globs and reads the first image's header;
`read` fetches the label files and image headers it needs.
"""
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

FILL = ("instance", "class")
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
SNAP = 1e-6         # YOLO text keeps 6 decimals of a normalised coordinate: a whole pixel reads back this close
EPS = 1e-9          # float round-off of an edge's crossing with a row, in pixels
CELLS = 1 << 22     # rows x edges worked on at once while filling one polygon

Polygon = tuple[int, np.ndarray]        # YOLO class id, (n, 2) normalised x, y


def _file(io: IO, url: str) -> BinaryIO:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).open("rb")


def _jpeg_grid(data: bytes, url: str) -> tuple[int, int]:
    """(Y, X) from the start-of-frame segment."""
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:    # markers without a length
            pos += 2
            continue
        if marker in SOF and pos + 9 <= len(data):
            return int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 7:pos + 9], "big")
        if marker == 0xDA:                              # start of scan: no frame header came before it
            break
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    raise ValueError(f"{url}: no JPEG start-of-frame segment found")


def _grid(io: IO, url: str) -> tuple[int, int]:
    """(Y, X) of an image from its header, told apart by its first bytes; no pixel is decoded."""
    with _file(io, url) as fh:
        head = fh.read(26)
        if head[:8] == PNG and head[12:16] == b"IHDR" and len(head) >= 24:
            grid = int.from_bytes(head[20:24], "big"), int.from_bytes(head[16:20], "big")
        elif head[:3] == JPEG:
            fh.seek(0)
            grid = _jpeg_grid(fh.read(), url)
        elif head[:4] in TIFF:
            fh.seek(0)
            with tifffile.TiffFile(fh) as tif:
                grid = int(tif.pages[0].imagelength), int(tif.pages[0].imagewidth)
        else:
            raise ValueError(f"{url}: not a PNG, JPEG or TIFF image: header={head[:8]!r}")
    if min(grid) < 1:
        raise ValueError(f"{url}: image size {grid[1]}x{grid[0]}")
    return grid


def _polygons(data: bytes, url: str) -> list[Polygon]:
    """The polygons of a YOLO segmentation file, one per non-blank line, in file order."""
    out = []
    for n, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        tok = line.split()
        if not tok:
            continue
        try:
            cls, v = int(tok[0]), np.array(tok[1:], dtype=np.float64)
        except ValueError:
            raise ValueError(f"{url} line {n}: not `class x1 y1 x2 y2 ...` numbers: {line[:60]!r}") from None
        if len(v) == 4:
            raise ValueError(f"{url} line {n}: 4 values after the class, a YOLO box (cx cy w h), not a polygon; "
                             "this snippet rasterises segmentation polygons only")
        if cls < 0 or len(v) < 6 or len(v) % 2:
            raise ValueError(f"{url} line {n}: class {cls} with {len(v)} coordinates is not a polygon of 3 or "
                             "more x y points")
        if not (np.isfinite(v).all() and v.min() >= 0 and v.max() <= 1):
            raise ValueError(f"{url} line {n}: coordinates outside 0..1 are not normalised to the image")
        out.append((cls, v.reshape(-1, 2)))
    return out


def _pixels(v: np.ndarray, size: int) -> np.ndarray:
    """Normalised coordinates as pixel indices; one within the text's rounding of a whole pixel is that pixel."""
    p = v * size
    r = np.rint(p)
    return np.where(np.abs(p - r) <= SNAP * size, r, p)


def _mask(x: np.ndarray, y: np.ndarray, grid: tuple[int, int]) -> tuple[slice, slice, np.ndarray] | None:
    """The pixels (row j, column i) whose point (x = i, y = j) lies inside the polygon (even-odd rule) or on its
    outline, as the row and column slices of its bounding box and a boolean mask of that box; None when the
    polygon holds no pixel of the grid."""
    ny, nx = grid
    j0, j1 = max(0, int(np.ceil(y.min() - EPS))), min(ny - 1, int(np.floor(y.max() + EPS)))
    i0, i1 = max(0, int(np.ceil(x.min() - EPS))), min(nx - 1, int(np.floor(x.max() + EPS)))
    if j0 > j1 or i0 > i1:
        return None
    xb, yb = np.roll(x, -1), np.roll(y, -1)                     # edge k runs from (x, y)[k] to (xb, yb)[k]
    lo, hi = np.minimum(y, yb), np.maximum(y, yb)
    flat = lo == hi
    slope = (xb - x) / np.where(flat, 1.0, yb - y)
    diff = np.zeros((j1 - j0 + 1, i1 - i0 + 2), np.int32)       # +1 where a span starts, -1 after its last pixel

    def spans(rows: np.ndarray, left: np.ndarray, right: np.ndarray) -> None:
        a = np.maximum(np.ceil(left - EPS), i0).astype(np.int64) - i0
        b = np.minimum(np.floor(right + EPS), i1).astype(np.int64) - i0
        ok = a <= b
        np.add.at(diff, (rows[ok], a[ok]), 1)
        np.add.at(diff, (rows[ok], b[ok] + 1), -1)

    step = max(1, CELLS // len(x))
    for start in range(j0, j1 + 1, step):
        rows = np.arange(start, min(start + step, j1 + 1), dtype=np.float64)[:, None]
        cross = x + (rows - y) * slope                          # where each edge's line meets each row
        # the polygon just below each row line, then just above it: together with the edges lying on the
        # row, their closed spans are the row's points inside the polygon or on its outline
        for meets in ((lo <= rows) & (rows < hi), (lo < rows) & (rows <= hi)):
            xs = np.sort(np.where(meets, cross, np.inf), axis=1)
            left, right = xs[:, 0:xs.shape[1] // 2 * 2:2], xs[:, 1::2]      # crossings pair up along a row
            r, k = np.nonzero(np.isfinite(right))
            spans(r + start - j0, left[r, k], right[r, k])
        r, k = np.nonzero(flat & (rows == y))
        spans(r + start - j0, np.minimum(x, xb)[k], np.maximum(x, xb)[k])
    return slice(j0, j1 + 1), slice(i0, i1 + 1), np.cumsum(diff, axis=1)[:, :-1] > 0


def _paint(polygons: list[Polygon], grid: tuple[int, int], fill: str, n_classes: int | None,
           url: str) -> np.ndarray:
    """The polygons filled on the grid, largest first, so the smaller of two overlapping polygons is on top
    (file order breaks ties). instance: 1-based polygon number in the file; class: YOLO class id + 1.
    0 is background."""
    out = np.zeros(grid, np.uint16)
    if len(polygons) > 65535:
        raise ValueError(f"{url}: {len(polygons)} polygons exceed uint16 ids")
    shapes = []
    for cls, v in polygons:
        if cls >= (65535 if n_classes is None else n_classes):
            raise ValueError(f"{url}: class id {cls} is outside " + (
                "uint16 ids" if n_classes is None else f"params.classes, which names ids 0..{n_classes - 1}"))
        x, y = _pixels(v[:, 0], grid[1]), _pixels(v[:, 1], grid[0])
        area = abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2
        shapes.append((area, x, y, cls))
    for i in sorted(range(len(shapes)), key=lambda i: -shapes[i][0]):
        _, x, y, cls = shapes[i]
        hit = _mask(x, y, grid)
        if hit is not None:
            rows, cols, mask = hit
            out[rows, cols][mask] = i + 1 if fill == "instance" else cls + 1
    return out


class _Planes:
    """One label file per plane, (Y, X) uint16 on the grid of its image; axes [z]yx."""

    def __init__(self, io: IO, urls: list[str], images: list[str], fill: str, n_classes: int | None):
        self._io, self._urls, self._images, self._fill, self._n_classes = io, urls, images, fill, n_classes
        self._last: tuple[int, np.ndarray] | None = None        # the last plane painted
        self._grid = _grid(io, images[0])
        many = len(urls) > 1
        self.axes = "zyx" if many else "yx"
        self.shape = ((len(urls),) if many else ()) + self._grid
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # a y/x region costs its label file and its image's header
        self.voxel_size_nm = None       # YOLO text has no pixel size

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in "yx")
        if "z" not in self.axes:
            return self._plane(0)[inner]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(len(self._urls))[z])[inner]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[inner] for k in range(len(self._urls))[z]]
        if not planes:
            return np.empty((0,) + np.empty(self._grid, self.dtype)[inner].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """File k painted on its image's grid, which must be the first image's."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        grid = _grid(self._io, self._images[k])
        if grid != self._grid:
            raise ValueError(f"{self._images[k]}: {grid} (y, x) differs from the first image's {self._grid}; "
                             "all planes of one array share a grid, bind the others as another array")
        with _file(self._io, self._urls[k]) as fh:
            data = fh.read()
        a = _paint(_polygons(data, self._urls[k]), grid, self._fill, self._n_classes, self._urls[k])
        self._last = (k, a)
        return a


def _stem(url: str) -> str:
    """A file's name without its directories and extension."""
    name = url.rpartition("::")[2].rpartition("/")[2]
    return name.rpartition(".")[0] or name


def _expand(urls: list[str] | tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if not out:
        raise ValueError("the urls match no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    fill, images, classes = src.params.get("fill"), src.params.get("images"), src.params.get("classes")
    if fill not in FILL:
        raise ValueError(f"params.fill must be one of {FILL}, got {fill!r}")
    if not (isinstance(images, list) and images and all(isinstance(u, str) and u for u in images)):
        raise ValueError(f"params.images must be a list of image urls (globs allowed), got {images!r}")
    if classes is not None and not (isinstance(classes, list) and classes
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of class names in YOLO id order, got {classes!r}")
    urls, image_urls = _expand(src.urls, io), _expand(images, io)
    if len(urls) != len(image_urls):
        raise ValueError(f"{len(urls)} label files but params.images names {len(image_urls)} images; a YOLO "
                         "label file belongs to one image")
    for u, i in zip(urls, image_urls):
        if _stem(u) != _stem(i):
            raise ValueError(f"label file {u} is paired by position with image {i}, which has another name; "
                             "a YOLO label file has its image's name with the extension .txt")
    return _Planes(io, urls, image_urls, fill, None if classes is None else len(classes))
