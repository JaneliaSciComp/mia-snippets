"""One 2D image per array, from one file: a PNG (grey or colour, 8 or 16 bit), a single-page TIFF (YX or YXS) or
a NumPy `.npy` array (2-D, a numeric dtype, never unpickled), told apart by the file's first bytes and not by
its name. The file is a zip member or a plain URL.

Written for sets whose images differ in size from file to file, so that no stack can hold them: each file is
bound as its own array and a label pairs with its image through the binding's `pairs_with`. A colour image's
samples are the `c` axis (`cyx`); `samples: grey` instead returns one plane from an image whose samples are
identical copies (a mask saved as RGB) and raises when they differ. Values are returned as stored.

`open_array` reads the file's header only (PNG `IHDR`, TIFF first IFD, `.npy` header); a deflated zip member
has to be staged whole for that. Pixels are decoded at `read`.
"""
import struct
from pathlib import Path
from typing import BinaryIO

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("grey",)
PNG = b"\x89PNG\r\n\x1a\n"
NPY = b"\x93NUMPY"
TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
PNG_SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}      # IHDR colour type -> samples per pixel (3, palette, is refused)


class _File:
    """The one file of an array: read by range where `io.open` can (a stored member, a plain URL), otherwise
    staged whole with `io.fetch` (a deflated member, a server without Range)."""

    def __init__(self, io: IO, url: str):
        self._io, self.url = io, url
        self._path: Path | None = None
        try:
            with self.open() as fh:
                self.head = fh.read(64)
        except NeedsFetch:
            self._path = io.fetch(url)
            with self.open() as fh:
                self.head = fh.read(64)

    @property
    def staged(self) -> bool:
        return self._path is not None

    def open(self) -> BinaryIO:
        return self._path.open("rb") if self._path is not None else self._io.open(self.url)


def _png_header(f: _File) -> tuple[tuple[int, ...], np.dtype]:
    """(stored shape (Y, X) or (Y, X, S), dtype) from the IHDR chunk."""
    head = f.head
    if len(head) < 33 or head[12:16] != b"IHDR":
        raise ValueError(f"{f.url}: a PNG without an IHDR chunk after its signature")
    width, height, depth, colour = struct.unpack(">IIBB", head[16:26])
    if colour not in PNG_SAMPLES or depth not in (8, 16):
        raise ValueError(f"{f.url}: PNG colour type {colour}, bit depth {depth}; a palette or 1/2/4-bit PNG "
                         "is not read, since the decoder would replace the stored indices by palette "
                         "colours or rescale them to 0..255")
    samples = PNG_SAMPLES[colour]
    return (height, width) + ((samples,) if samples > 1 else ()), np.dtype(np.uint8 if depth == 8 else np.uint16)


def _tiff_header(f: _File) -> tuple[tuple[int, ...], np.dtype]:
    """(stored shape (Y, X) or (Y, X, S), dtype) of a single-page TIFF, from its first IFD."""
    with f.open() as fh, tifffile.TiffFile(fh) as tif:
        s = tif.series[0]
        if len(tif.series) != 1 or len(tif.pages) != 1 or s.axes not in ("YX", "YXS"):
            raise ValueError(f"{f.url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.pages)} page(s); "
                             "this snippet reads a single-page YX or YXS (RGB) TIFF only")
        if tif.pages[0].photometric == tifffile.PHOTOMETRIC.PALETTE:
            raise ValueError(f"{f.url}: a palette TIFF; its stored values are colour indices, not read here")
        return tuple(int(n) for n in s.shape), np.dtype(s.dtype).newbyteorder("=")


class _Image:
    """A PNG or single-page TIFF as (Y, X) or (C, Y, X). Any region costs the whole file (a PNG is one deflate
    stream; the TIFFs met are a few LZW strips), so it is decoded once, at the first read, and kept."""

    access = "file"
    voxel_size_nm = None    # a PNG pHYs chunk and TIFF resolution tags are print resolutions, not pixel sizes

    def __init__(self, f: _File, samples: str | None):
        self._f, self._grey, self._image = f, samples == "grey", None
        self._tiff = f.head[:4] in TIFF
        self._stored, self.dtype = _tiff_header(f) if self._tiff else _png_header(f)
        if len(self._stored) == 2 or self._grey:
            self.axes, self.shape = "yx", self._stored[:2]
        else:
            self.axes, self.shape = "cyx", (self._stored[2],) + self._stored[:2]

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._image is None:
            self._image = self._decode()
        return self._image[tuple(region.get(k, slice(None)) for k in self.axes)]

    def _decode(self) -> np.ndarray:
        with self._f.open() as fh:
            if self._tiff:
                with tifffile.TiffFile(fh) as tif:
                    a = np.asarray(tif.series[0].asarray())
            else:
                a = np.asarray(imagecodecs.png_decode(fh.read()))
        if a.shape != self._stored or a.dtype != self.dtype:    # e.g. a PNG tRNS chunk, decoded as added alpha
            raise ValueError(f"{self._f.url}: decoded to {a.dtype} {a.shape}, but its header says {self.dtype} "
                             f"{self._stored} (y, x, samples)")
        if a.ndim == 2:
            return a
        if self._grey:
            if not np.array_equal(a, np.broadcast_to(a[:, :, :1], a.shape)):
                raise ValueError(f"{self._f.url}: samples differ, so this is not a grey image or mask stored as "
                                 "RGB; drop params.samples to read the samples as a c axis")
            return np.ascontiguousarray(a[:, :, 0])
        return np.ascontiguousarray(np.moveaxis(a, 2, 0))


class _Npy:
    """A 2-D `.npy` array as (Y, X). The data follow the header uncompressed, so rows are read by position:
    from the staged file, or by range requests."""

    voxel_size_nm = None
    axes = "yx"

    def __init__(self, f: _File):
        with f.open() as fh:
            version = np.lib.format.read_magic(fh)
            if version == (1, 0):
                shape, fortran, stored = np.lib.format.read_array_header_1_0(fh)
            elif version == (2, 0):
                shape, fortran, stored = np.lib.format.read_array_header_2_0(fh)
            else:
                raise ValueError(f"{f.url}: .npy format version {version}; this snippet reads 1.0 and 2.0")
            self._offset = fh.tell()
        if stored.hasobject or stored.fields is not None or stored.kind not in "biuf":
            raise ValueError(f"{f.url}: .npy dtype {stored}; only bool, integer and float arrays are read "
                             "(an object array would need pickle, which is never used)")
        if len(shape) != 2 or 0 in shape:
            raise ValueError(f"{f.url}: .npy shape {tuple(shape)}; this snippet reads 2-D (y, x) arrays only")
        self._f, self._stored, self._fortran = f, stored, bool(fortran)
        self._whole: np.ndarray | None = None       # a Fortran-ordered file, read once
        self.shape = (int(shape[0]), int(shape[1]))
        self.dtype = stored.newbyteorder("=")       # values unchanged; a big-endian file is byte-swapped
        # rows of a C-ordered file read by range are a chunk; a staged file (a deflated member) was the unit
        self.access = "file" if f.staged or self._fortran else "chunk"

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ysel, xsel = region.get("y", slice(None)), region.get("x", slice(None))
        height = self.shape[0]
        if not isinstance(ysel, slice):
            row = range(height)[ysel]               # IndexError when out of range, like numpy
            return self._rows(row, row + 1)[0][xsel]
        rows = range(height)[ysel]
        if not len(rows):
            return np.empty((0, self.shape[1]), self.dtype)[:, xsel]
        lo = min(rows[0], rows[-1])
        block = self._rows(lo, max(rows[0], rows[-1]) + 1)
        return block[rows[0] - lo::rows.step][:len(rows), xsel]

    def _rows(self, lo: int, hi: int) -> np.ndarray:
        """Rows [lo, hi) as (hi - lo, X) in the native byte order."""
        height, width = self.shape
        if self._fortran:
            if self._whole is None:
                self._whole = self._block(0, height * width).reshape((height, width), order="F")
            return self._whole[lo:hi]
        return self._block(lo * width, (hi - lo) * width).reshape(hi - lo, width)

    def _block(self, first: int, count: int) -> np.ndarray:
        size = self._stored.itemsize
        with self._f.open() as fh:
            fh.seek(self._offset + first * size)
            data = fh.read(count * size)
        if len(data) != count * size:
            raise ValueError(f"{self._f.url}: the file ends before its header's shape {self.shape} of "
                             f"{self._stored} is complete")
        return np.frombuffer(data, self._stored).astype(self.dtype, copy=False)


def open_array(src: Source, io: IO) -> Array:
    samples = src.params.get("samples")
    if samples is not None and samples not in MODES:
        raise ValueError(f"params.samples must be one of {MODES}, got {samples!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(urls) != 1:
        raise ValueError(f"an array is one file, but the urls name {len(urls)}; bind each file as its own "
                         "raw or label entry (files of a set may differ in shape, so they are not stacked)")
    f = _File(io, urls[0])
    if f.head[:8] == PNG or f.head[:4] in TIFF:
        return _Image(f, samples)
    if f.head[:6] == NPY:
        if samples is not None:
            raise ValueError(f"{f.url}: params.samples is for a PNG or TIFF; a .npy array has no samples")
        return _Npy(f)
    raise ValueError(f"{f.url}: not a PNG, TIFF or .npy file: header={f.head[:8]!r}")
