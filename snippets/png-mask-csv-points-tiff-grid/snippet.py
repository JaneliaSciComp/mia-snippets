"""A label file placed on the pixel grid of the single-page TIFF it annotates, as `yx` with the TIFF's shape.

`urls` are two files, in either order, told apart by their first bytes: the TIFF whose grid it is (only its
header is used, never its pixels) and one label file, which is either

- a **CSV points table** with the header `x,y` and one object per row, in pixels of the TIFF: the pixel that
  holds a point takes the point's 1-based row number, every other pixel is 0 (`uint32`); or
- a **greyscale PNG mask** of bit depth 1 or 8 on a `label_factor` times coarser grid: TIFF pixel (y, x) takes
  PNG pixel (y // k, x // k), the stored value, so a 1-bit PNG reads as {0, 1} (`uint8`).

Nothing is interpolated in either form. Plain URLs or zip members; a member that cannot be read by range (a
deflated one) is staged whole.
"""
import math
import struct
from typing import BinaryIO

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_HEADER = 33             # signature (8) + IHDR chunk: length (4), type (4), 13 bytes of data, CRC (4)
PNG_GREY = 0                # IHDR colour type: one sample per pixel, no palette, no alpha
PNG_DEPTHS = (1, 8)
Index = int | slice


def _selected(n: int, sel: Index) -> np.ndarray:
    """The indices along an axis of length `n` that `sel` picks, as numpy would (IndexError out of range)."""
    return np.atleast_1d(np.arange(n)[sel])


def _squeeze(out: np.ndarray, region: dict[str, Index]) -> np.ndarray:
    """Drop the axes the region indexes with an int, as numpy indexing does."""
    drop = tuple(i for i, ax in enumerate("yx") if not isinstance(region.get(ax, slice(None)), slice))
    return out.reshape([n for i, n in enumerate(out.shape) if i not in drop])


class _Points:
    """A table of x,y points: the pixel holding point number i (1-based data row) is i, all others are 0."""

    def __init__(self, fh: BinaryIO, url: str, grid: tuple[int, int]):
        self._fh, self._url = fh, url
        self.axes, self.shape, self.dtype = "yx", grid, np.dtype("uint32")
        self.voxel_size_nm = None       # neither the table nor the TIFF header used for the grid is consulted
        self.access = "file"            # the table is read whole on the first read and kept
        self.n_points: int | None = None        # set by the first read
        self.n_overwritten: int | None = None   # points whose pixel a later row also falls in
        self._rows = self._cols = self._ids = np.empty(0, np.int64)
        _columns(fh.read(256).split(b"\n")[0], url)    # the header line only: a wrong table fails at open

    def _load(self) -> None:
        self._fh.seek(0)
        lines = self._fh.read().decode("utf-8-sig").splitlines()
        _columns(lines[0].encode(), self._url)
        xy = []
        for number, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue                # a blank line (the file's last) is not a row
            fields = line.split(",")
            try:
                x, y = (float(f) for f in fields)
            except ValueError:
                raise ValueError(f"{self._url} line {number}: {line[:80]!r} is not two numbers x,y") from None
            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError(f"{self._url} line {number}: {line[:80]!r} is not a finite point")
            xy.append((x, y))
        pts = np.array(xy, np.float64).reshape(-1, 2)
        cols, rows = np.floor(pts[:, 0]).astype(np.int64), np.floor(pts[:, 1]).astype(np.int64)
        ny, nx = self.shape
        outside = np.flatnonzero((rows < 0) | (rows >= ny) | (cols < 0) | (cols >= nx))
        if len(outside):
            i = int(outside[0])
            raise ValueError(f"{self._url}: {len(outside)} of {len(pts)} points lie outside the TIFF's "
                             f"{nx} x {ny} pixels, the first is row {i + 1}: x={pts[i, 0]}, y={pts[i, 1]}; "
                             "the table is not in pixels of this TIFF")
        self._rows, self._cols, self._ids = rows, cols, np.arange(1, len(pts) + 1, dtype=np.int64)
        self.n_points = len(pts)
        self.n_overwritten = len(pts) - len(np.unique(rows * nx + cols))

    def read(self, region: dict[str, Index]) -> np.ndarray:
        if self.n_points is None:
            self._load()
        ys, xs = (_selected(n, region.get(ax, slice(None))) for n, ax in zip(self.shape, "yx"))
        out = np.zeros((len(ys), len(xs)), self.dtype)
        where_y, where_x = np.full(self.shape[0], -1), np.full(self.shape[1], -1)
        where_y[ys], where_x[xs] = np.arange(len(ys)), np.arange(len(xs))    # grid index -> index in `out`
        oy, ox = where_y[self._rows], where_x[self._cols]
        inside = (oy >= 0) & (ox >= 0)
        out[oy[inside], ox[inside]] = self._ids[inside]      # in row order: of two points in a pixel the later
        return _squeeze(out, region)


def _columns(header: bytes, url: str) -> None:
    names = [f.strip().lower() for f in header.decode("utf-8-sig", "replace").strip().split(",")]
    if names != ["x", "y"]:
        raise ValueError(f"{url}: the table's header line is {header[:80]!r}; this snippet reads a CSV whose "
                         "columns are exactly x,y")


class _Mask:
    """A grey PNG whose pixel (i, j) covers pixels (i*k .. i*k+k-1, j*k .. j*k+k-1) of the TIFF's grid."""

    def __init__(self, fh: BinaryIO, url: str, grid: tuple[int, int], factor: int):
        self._fh, self._url = fh, url
        head = fh.read(PNG_HEADER)
        if len(head) < PNG_HEADER or head[12:16] != b"IHDR":
            raise ValueError(f"{url}: not a PNG file: no IHDR chunk after the signature")
        width, height, self._depth, colour = struct.unpack(">IIBB", head[16:26])
        if colour != PNG_GREY or self._depth not in PNG_DEPTHS:
            raise ValueError(f"{url}: PNG colour type {colour}, bit depth {self._depth}; this snippet reads "
                             "greyscale (colour type 0) PNGs of 1 or 8 bits")
        self._stored = (height, width)
        want = tuple(-(-n // factor) for n in grid)
        if self._stored != want:
            raise ValueError(f"{url}: the PNG is {self._stored}; with label_factor {factor} the TIFF's {grid} "
                             f"needs {want} (each axis divided by the factor, rounded up)")
        self.axes, self.shape, self.dtype = "yx", grid, np.dtype("uint8")
        self.voxel_size_nm = None
        self.access = "file"            # one deflate stream: decoded whole on the first read and kept
        self._index = tuple(np.arange(n) // factor for n in grid)       # grid index -> PNG index
        self._page: np.ndarray | None = None

    def _decode(self) -> np.ndarray:
        self._fh.seek(0)
        page = np.asarray(imagecodecs.png_decode(self._fh.read()))
        if page.shape != self._stored or page.dtype != self.dtype:     # e.g. a tRNS chunk decoded as alpha
            raise ValueError(f"{self._url}: decoded as {page.dtype} {page.shape}, its header says "
                             f"{self._depth}-bit grey {self._stored}")
        if self._depth == 1:
            # libpng may hand a 1-bit sample back as it is stored (0, 1) or widened to 8 bits (0, 255); the
            # stored bit is which of the two a pixel is, so `> 0` gives it back in both cases
            values = np.unique(page)
            if not (set(values.tolist()) <= {0, 1} or set(values.tolist()) <= {0, 255}):
                raise ValueError(f"{self._url}: a 1-bit PNG decoded to the values {values[:8].tolist()}")
            page = (page > 0).astype(self.dtype)
        return page

    def read(self, region: dict[str, Index]) -> np.ndarray:
        if self._page is None:
            self._page = self._decode()
        iy, ix = self._index
        return np.array(self._page[iy[region.get("y", slice(None))]][..., ix[region.get("x", slice(None))]])


def _grid(fh: BinaryIO, url: str) -> tuple[int, int]:
    """(height, width) of the TIFF, from its header; its pixels are not read."""
    fh.seek(0)
    with tifffile.TiffFile(fh) as tif:
        s = tif.series[0]
        if len(tif.series) != 1 or len(s.pages) != 1 or s.axes != "YX":
            raise ValueError(f"{url}: the TIFF has {len(tif.series)} series, the first with {len(s.pages)} pages "
                             f"and axes {s.axes!r} {tuple(s.shape)}; a label is placed on one single-page YX image")
        return int(s.shape[0]), int(s.shape[1])


def _open(io: IO, url: str) -> BinaryIO:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated zip member, or a server without Range: stage the whole file
        return open(io.fetch(url), "rb")


def open_array(src: Source, io: IO) -> Array:
    factor = src.params.get("label_factor")
    if len(src.urls) != 2:
        raise ValueError(f"this snippet reads two urls, the label file (CSV points table or PNG mask) and the "
                         f"TIFF whose grid it is placed on, got {len(src.urls)}")
    tiffs: list[tuple[BinaryIO, str]] = []
    others: list[tuple[BinaryIO, str, bool]] = []
    for url in src.urls:
        fh = _open(io, url)
        magic = fh.read(8)
        fh.seek(0)
        if magic[:4] in TIFF_MAGIC:
            tiffs.append((fh, url))
        else:
            others.append((fh, url, magic == PNG_SIGNATURE))
    if len(tiffs) != 1:
        raise ValueError(f"{len(tiffs)} of the two urls are TIFFs; exactly one must be, the image whose grid "
                         "the other file is placed on")
    grid = _grid(*tiffs[0])
    fh, url, is_png = others[0]
    if not is_png:
        if factor is not None:
            raise ValueError(f"{url} is not a PNG; params.label_factor is for a PNG mask, a points table is "
                             "in the TIFF's own pixels")
        return _Points(fh, url, grid)
    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 1:
        raise ValueError(f"{url} is a PNG mask: params.label_factor must say, as a positive integer, how many "
                         f"TIFF pixels one of its pixels covers on each axis, got {factor!r}")
    return _Mask(fh, url, grid, factor)
