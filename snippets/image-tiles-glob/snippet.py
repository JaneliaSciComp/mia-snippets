"""One image per file (PNG, JPEG or single-page TIFF, told apart by the file's first bytes), from zip members or
directory files chosen by glob (or listed one by one), stacked along z. A colour file's samples are exposed as
a `c` axis (`zcyx`), so no colour is lost; `samples: grey` instead returns one plane from a file whose samples
are identical copies (a class map or grey image saved as RGB) and raises when they differ.

Values are returned as the decoder gives them, e.g. the class ids of a mask PNG. Files are opened lazily:
`open_array` reads the first file only; `read` fetches the files it needs, one file per z plane.
"""
from io import BytesIO

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("grey",)
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def _decode(data: bytes, url: str) -> np.ndarray:
    """The file's one image as (Y, X) or (Y, X, S), by its magic bytes (a zip member's name is not trusted)."""
    if data[:8] == PNG:
        # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type (3 = palette)
        if len(data) < 26 or data[25] == 3 or data[24] < 8:
            raise ValueError(f"{url}: a palette or 1/2/4-bit PNG; the decoder would replace the stored indices "
                             "by palette colours or rescale them to 0..255, so this snippet does not read it")
        a = imagecodecs.png_decode(data)
    elif data[:3] == JPEG:
        a = imagecodecs.jpeg_decode(data)
    elif data[:4] in TIFF:
        with tifffile.TiffFile(BytesIO(data)) as tif:
            s = tif.series[0]
            if s.axes not in ("YX", "YXS") or len(tif.series) != 1:
                raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)}; this snippet reads "
                                 "single-page YX or YXS (RGB) tiles only, see tiff-zip-member for stacks")
            a = s.asarray()
    else:
        raise ValueError(f"{url}: not a PNG, JPEG or TIFF file: header={data[:8]!r}")
    a = np.asarray(a)
    if a.ndim not in (2, 3):
        raise ValueError(f"{url}: decoded to shape {a.shape}, not (Y, X) or (Y, X, S)")
    return a


class _Tiles:
    def __init__(self, io: IO, urls: list[str], samples: str | None):
        self._io, self._urls, self._samples = io, urls, samples
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
        if a.ndim == 2:
            return a
        if self._samples == "grey":
            if not np.array_equal(a, np.broadcast_to(a[:, :, :1], a.shape)):
                raise ValueError(f"{url}: samples differ, so this is not a grey image or class map stored as "
                                 "RGB; drop params.samples to read the samples as a c axis")
            return a[:, :, 0]
        return np.ascontiguousarray(np.moveaxis(a, 2, 0))


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    samples = src.params.get("samples")
    if samples is not None and samples not in MODES:
        raise ValueError(f"params.samples must be one of {MODES}, got {samples!r}")
    return _Tiles(io, _expand(src.urls, io), samples)
