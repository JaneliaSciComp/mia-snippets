"""Grey PNG files at plain URLs (or zip members), one file per channel of one field of view, as `cyx`.

A PNG is one deflate stream, so a file is always read and decoded whole (`access: file`). One url is a `yx`
image; several urls are the channels of one field, in binding order, stacked as `cyx`. `open_array` reads the
first file's 33-byte header only (signature + IHDR: width, height, bit depth, colour type); `read` decodes the
channels it is asked for and checks each against that header.
"""
import struct

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SIGNATURE = b"\x89PNG\r\n\x1a\n"
HEADER = 33                 # signature (8) + IHDR chunk: length (4), type (4), 13 bytes of data, CRC (4)
GREY = 0                    # IHDR colour type: one sample per pixel, no palette, no alpha
DTYPES = {8: np.dtype("uint8"), 16: np.dtype("uint16")}     # IHDR bit depth -> dtype, stored values unchanged


class _Png:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._plane_shape, self.dtype = _header(_data(io, urls[0], HEADER), urls[0])
        self.shape: tuple[int, ...] = (len(urls), *self._plane_shape) if len(urls) > 1 else self._plane_shape
        self.axes = "cyx" if len(urls) > 1 else "yx"
        self.voxel_size_nm = None       # pHYs, when present, is a print resolution, not a pixel size
        self.access = "file"            # one deflate stream per file: no rows or tiles to read by range
        self._last: tuple[int, np.ndarray] | None = None    # the channel decoded last; patches of one file reuse it

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "c" not in self.axes:
            return self._plane(0)[y, x]
        sel = region.get("c", slice(None))
        if isinstance(sel, slice):
            picked = [self._plane(k)[y, x] for k in range(self.shape[0])[sel]]
            return np.stack(picked) if picked else np.empty((0, *self._plane_shape), self.dtype)[:, y, x]
        return self._plane(range(self.shape[0])[sel])[y, x]     # IndexError when out of range, like numpy

    def _plane(self, k: int) -> np.ndarray:
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        data = _data(self._io, url)
        shape, dtype = _header(data, url)
        if shape != self._plane_shape or dtype != self.dtype:
            raise ValueError(f"{url}: {dtype} {shape} differs from the first file's {self.dtype} "
                             f"{self._plane_shape}; all channels of one field must agree")
        plane = np.asarray(imagecodecs.png_decode(data))
        if plane.shape != shape or plane.dtype != dtype:        # e.g. a tRNS chunk decoded as an alpha sample
            raise ValueError(f"{url}: decoded as {plane.dtype} {plane.shape}, its header says {dtype} {shape}")
        self._last = (k, plane)
        return plane


def _data(io: IO, url: str, n: int = -1) -> bytes:
    """The first `n` bytes of the file, or all of it: by range when the server allows, else staged whole."""
    try:
        with io.open(url) as fh:
            return fh.read(n)
    except NeedsFetch:      # a deflated zip member, or a server without Range / Content-Length
        data = io.fetch(url).read_bytes()
        return data if n < 0 else data[:n]


def _header(data: bytes, url: str) -> tuple[tuple[int, int], np.dtype]:
    """(height, width) and dtype from the signature and IHDR chunk; ValueError for anything but 8/16-bit grey."""
    if len(data) < HEADER or data[:8] != SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    if colour != GREY or depth not in DTYPES:
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; this snippet reads greyscale "
                         "(colour type 0) PNGs of 8 or 16 bits, one channel per file")
    return (height, width), DTYPES[depth]


def open_array(src: Source, io: IO) -> Array:
    urls = [u for url in src.urls for u in io.expand(url)]
    if not urls:
        raise ValueError("png-channels needs at least one url")
    if len(set(urls)) != len(urls):
        raise ValueError("the urls name the same file twice")
    return _Png(io, urls)
