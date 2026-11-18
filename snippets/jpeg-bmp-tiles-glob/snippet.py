"""One image per file, JPEG or uncompressed 24-bit BMP, told apart by the file's first bytes and not by its
name, from zip members or directory files chosen by glob (or listed one by one), stacked along z. A colour
file's samples are the `c` axis in R, G, B order (`zcyx`), whichever of the two formats holds it.

It is for sets that mix the two formats under one naming scheme (e.g. every file called `.png`), where no glob
can bind the JPEGs alone. Values are the stored ones: a BMP's bytes reordered (BGR to RGB, bottom row last), a
JPEG's as libjpeg decodes them. Files are opened lazily: `open_array` reads the first file only; `read` fetches
the files it needs, one file per z plane.
"""
import struct

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

JPEG = b"\xff\xd8\xff"
BMP = b"BM"


def _bmp(data: bytes, url: str) -> np.ndarray:
    """An uncompressed 24-bit BMP as (Y, X, 3): samples R, G, B, row 0 the top of the picture.

    The file stores B, G, R per pixel in rows padded to a multiple of 4 bytes, the bottom row first when the
    header's height is positive. Bytes after the pixel data are ignored."""
    if len(data) < 54:
        raise ValueError(f"{url}: a BMP of {len(data)} bytes has no complete header")
    offset = struct.unpack_from("<I", data, 10)[0]
    header, width, height, planes, bits, compression = struct.unpack_from("<IiiHHI", data, 14)
    if header < 40 or planes != 1 or bits != 24 or compression != 0 or width <= 0 or height == 0:
        raise ValueError(f"{url}: BMP with header size {header}, {width}x{height}, {bits} bits per pixel, "
                         f"compression {compression}; this snippet reads uncompressed 24-bit BMP "
                         "(BITMAPINFOHEADER or later, BI_RGB) only")
    rows, stride = abs(height), (width * 3 + 3) // 4 * 4
    if offset < 14 + header or offset + rows * stride > len(data):
        raise ValueError(f"{url}: BMP pixel data at byte {offset} ({rows} rows of {stride} bytes) does not fit "
                         f"the file's {len(data)} bytes after its {14 + header}-byte header")
    a = np.frombuffer(data, np.uint8, rows * stride, offset).reshape(rows, stride)[:, :width * 3]
    a = a.reshape(rows, width, 3)[:, :, ::-1]           # B, G, R as stored -> R, G, B
    return a[::-1] if height > 0 else a                 # positive height: the file's first row is the bottom one


def _decode(data: bytes, url: str) -> np.ndarray:
    """The file's one image as (Y, X) or (Y, X, 3), by its magic bytes (a file's name is not trusted)."""
    if data[:3] == JPEG:
        a = np.asarray(imagecodecs.jpeg_decode(data))
    elif data[:2] == BMP:
        a = _bmp(data, url)
    else:
        raise ValueError(f"{url}: not a JPEG or BMP file: header={data[:8]!r}")
    if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[2] != 3):
        raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}; this snippet reads grey (Y, X) and "
                         "three-sample (Y, X, 3) images only")
    return a


class _Tiles:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._last: tuple[int, np.ndarray] | None = None    # the last file decoded: c planes are read one by one
        first = self._load(0)                               # (Y, X) or (C, Y, X); every file must match
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self.axes = ("z" if len(urls) > 1 else "") + ("cyx" if first.ndim == 3 else "yx")
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + first.shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file, read by
        self.voxel_size_nm = None       # range (stored member, plain url) or staged (deflated member)
        self._last = (0, first)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes if k != "z")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._urls))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._urls))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (C, Y, X), checked against file 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._urls[k]}: {a.dtype} {a.shape} differs from the first file's "
                             f"{self.dtype} {self._plane_shape} (c, y, x); all files of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        url = self._urls[k]
        try:
            with self._io.open(url) as fh:
                data = fh.read()
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            data = self._io.fetch(url).read_bytes()
        a = _decode(data, url)
        return a if a.ndim == 2 else np.ascontiguousarray(np.moveaxis(a, 2, 0))


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    return _Tiles(io, _expand(src.urls, io))
