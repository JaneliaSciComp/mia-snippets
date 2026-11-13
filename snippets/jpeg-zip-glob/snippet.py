"""JPEG images chosen by glob from the members of a zip, one image per z plane. The zip is whatever `io` can
open or fetch: a plain URL, or a member of another zip (a zip nested in the dataset's zip).

Each url names one zip (`<zip url>` or `<outer zip url>::<inner zip member>`); `params.glob` picks the images
inside it. A colour JPEG's samples become the c axis, all of them or the ones `params.samples` names.
`open_array` reads each zip's directory and decodes the first image; `read` decodes the planes it needs.
"""
from fnmatch import fnmatchcase
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SAMPLES = "rgb"     # the letters of params.samples, in the decoded image's sample order


class _Images:
    def __init__(self, files: list[tuple[ZipFile, str, str]], samples: str | None):
        self._files = files                             # (open zip, its url, member name), one per z plane
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded image: c planes of one file share it
        first = self._image(0)
        self._page_shape, self._file_dtype = first.shape, first.dtype   # (Y, X) or (Y, X, 3); every file must match
        if first.ndim == 2:
            if samples is not None:
                raise ValueError(f"{self._name(0)} is a grey JPEG with no sample axis; drop params.samples")
            self._sel: list[int] | None = None
        else:
            self._sel = [SAMPLES.index(ch) for ch in samples or SAMPLES]
        n, nc = len(files), len(self._sel or [0])
        self.axes = ("z" if n > 1 else "") + ("c" if nc > 1 else "") + "yx"
        self.shape = ((n,) if n > 1 else ()) + ((nc,) if nc > 1 else ()) + first.shape[:2]
        self.dtype = np.dtype(first.dtype)
        self.voxel_size_nm = None       # a JPEG's JFIF density is a print size, not a pixel size
        self.access = "plane"           # one file per plane: any y/x region costs decoding that whole image

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if "z" not in self.axes:
            return self._cut(self._image(0), region)
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._cut(self._image(range(len(self._files))[z]), region)   # IndexError like numpy
        planes = [self._cut(self._image(k), region) for k in range(len(self._files))[z]]
        if planes:
            return np.stack(planes)
        blank = self._cut(np.empty(self._page_shape, self._file_dtype), region)   # an empty z range, as numpy
        return np.empty((0, *blank.shape), blank.dtype)

    def _cut(self, img: np.ndarray, region: dict[str, int | slice]) -> np.ndarray:
        """The c, y, x part of `region` out of one decoded image (Y, X) or (Y, X, 3), as ([c,] y, x)."""
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if self._sel is None:
            return img[y, x]
        if "c" not in self.axes:
            return img[:, :, self._sel[0]][y, x]
        c = region.get("c", slice(None))
        if isinstance(c, slice):
            return np.moveaxis(img[:, :, self._sel[c]], 2, 0)[:, y, x]
        return img[:, :, self._sel[c]][y, x]

    def _name(self, k: int) -> str:
        return f"{self._files[k][1]}::{self._files[k][2]}"

    def _image(self, k: int) -> np.ndarray:
        """The decoded image of file k, (Y, X) or (Y, X, 3), checked against file 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        data = self._files[k][0].read(self._files[k][2])    # zipfile inflates the member and checks its CRC
        if data[:2] != b"\xff\xd8":
            raise ValueError(f"{self._name(k)} is not a JPEG (starts {data[:4]!r})")
        a = np.asarray(imagecodecs.jpeg_decode(data))
        if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[2] != 3):
            raise ValueError(f"{self._name(k)}: decoded as {a.dtype} {a.shape}; this snippet reads grey (Y, X) "
                             "and three-sample (Y, X, 3) JPEGs only")
        if k > 0 and (a.shape != self._page_shape or a.dtype != self._file_dtype):
            raise ValueError(f"{self._name(k)}: {a.dtype} {a.shape} differs from the first file's "
                             f"{self._file_dtype} {self._page_shape}; all files of one array must agree")
        self._last = (k, a)
        return a


def _open_zip(url: str, io: IO) -> ZipFile:
    """The zip at `url`, read by range when it can be (a plain URL, a stored member of an outer zip), else staged
    whole (a deflated member of an outer zip, a server without Range)."""
    try:
        return ZipFile(io.open(url))
    except NeedsFetch:
        return ZipFile(io.fetch(url))


def open_array(src: Source, io: IO) -> Array:
    glob, samples = src.params.get("glob"), src.params.get("samples")
    if not isinstance(glob, str) or not glob:
        raise ValueError(f"params.glob must be a member name pattern such as 'val/*.jpg', got {glob!r}")
    if samples is not None and (not isinstance(samples, str) or not samples or len(set(samples)) != len(samples)
                                or not set(samples) <= set(SAMPLES)):
        raise ValueError(f"params.samples must be distinct letters of {SAMPLES!r} such as 'rg', got {samples!r}")
    for url in src.urls:
        if url.count("::", max(url.find("/", url.find("://") + 3), 0)) > 1:
            raise ValueError(f"{url}: a url names the zip that holds the images (at most one '::'); the pattern "
                             "for the images inside it goes in params.glob")
    urls = [u for url in src.urls for u in io.expand(url)]      # an outer `<zip>::<glob>` over several inner zips
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same zip twice")
    files: list[tuple[ZipFile, str, str]] = []
    for url in urls:
        z = _open_zip(url, io)
        names = sorted(n for n in z.namelist() if not n.endswith("/") and fnmatchcase(n, glob))
        if not names:
            raise ValueError(f"no member of {url} matches {glob!r}")
        files += [(z, url, n) for n in names]
    return _Images(files, samples)
