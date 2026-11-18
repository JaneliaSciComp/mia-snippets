"""The first image of each TIFF file, from directory files or zip members chosen by glob (or listed one by
one), stacked along z: one file per z plane.

It is for files that carry a small preview after their image, as a microscope's export does (a Keyence BZ-X
`.tif` is a grey 720x960 page followed by a 120x160 RGB thumbnail page). tifffile reports such a file as two
series, and the other glob snippets refuse a file with more than one. Here series 0 is the image and every
later series is left unread, after a check that it is smaller than the image in both y and x: a second image
of full size is data, and a file that holds one is refused instead of losing it.

The image is one grey page (zyx) or one page with interleaved samples (zcyx, c in the file's sample order).
Values are returned as stored. Files are opened lazily: `open_array` reads the first file's tags only; `read`
fetches the files it needs.

A request the host refuses with 403 is asked again (`_again`): ftp.ebi.ac.uk at times answers the index of a
directory it serves with 403 for a few seconds, and a binding of many folders needs every index to open.
"""
import time
from io import BytesIO
from typing import Callable, TypeVar

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

WAITS = (5.0, 20.0, 60.0)       # seconds before each further attempt of a request the host refused
T = TypeVar("T")


def _refused(e: Exception) -> bool:
    """A 403 answer. `io` raises its HTTP client's status error, told here by class name since a snippet may
    not import the client. Nothing else is asked again: `io` itself retries dropped connections, 5xx and 429,
    a 404 is a file that is not there, and BudgetExceeded, HostNotAllowed and NeedsFetch are final."""
    names = {c.__name__ for c in type(e).__mro__}
    return "HTTPStatusError" in names and getattr(getattr(e, "response", None), "status_code", None) == 403


def _again(step: Callable[[], T]) -> T:
    """`step()`, repeated after each of WAITS while the host answers 403; the last 403 and any other error are
    raised as they are."""
    for wait in WAITS:
        try:
            return step()
        except Exception as e:
            if not _refused(e):
                raise
        time.sleep(wait)
    return step()


def _image(tif: tifffile.TiffFile, url: str) -> tifffile.TiffPageSeries:
    """Series 0 of the file, after checking that it is one YX or YXS page and that nothing else in the file is
    as large as it."""
    if not tif.series:
        raise ValueError(f"{url}: a TIFF file without an image")
    image = tif.series[0]
    if image.axes not in ("YX", "YXS") or len(image.pages) != 1:
        raise ValueError(f"{url}: the first image has tifffile axes {image.axes!r} {tuple(image.shape)} on "
                         f"{len(image.pages)} page(s); this snippet reads a single-page YX or YXS image per "
                         "file, see tiff-zip-member-2 for stacks")
    ny, nx = image.shape[:2]
    for i, other in enumerate(tif.series[1:], 1):
        size = dict(zip(other.axes, other.shape))
        if not (size.get("Y", ny) < ny and size.get("X", nx) < nx):
            raise ValueError(f"{url}: series {i} of the file has tifffile axes {other.axes!r} "
                             f"{tuple(other.shape)}, not smaller in y and x than the first image "
                             f"{tuple(image.shape)}; only a preview after the image is left unread, so a file "
                             "with a second image is refused")
    return image


def _plane_shape(image: tifffile.TiffPageSeries) -> tuple[int, ...]:
    """(Y, X), or (C, Y, X) for an image with samples."""
    shape = tuple(int(n) for n in image.shape)
    return shape if len(shape) == 2 else (shape[2], shape[0], shape[1])


class _Images:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._last: tuple[int, np.ndarray] | None = None    # the last file decoded: c planes are read one by one
        with tifffile.TiffFile(BytesIO(self._bytes(0))) as tif:     # tags only; every file must match this one
            image = _image(tif, urls[0])
            self._plane_shape, self.dtype = _plane_shape(image), np.dtype(image.dtype)
        self.axes = ("z" if len(urls) > 1 else "") + ("cyx" if len(self._plane_shape) == 3 else "yx")
        self.shape: tuple[int, ...] = ((len(urls),) if len(urls) > 1 else ()) + self._plane_shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file
        self.voxel_size_nm = None       # plain TIFF resolution tags describe print size, not pixels

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
        """The image of file k as (Y, X) or (C, Y, X), checked against file 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        with tifffile.TiffFile(BytesIO(self._bytes(k))) as tif:
            image = _image(tif, url)
            shape, dtype = _plane_shape(image), np.dtype(image.dtype)
            if shape != self._plane_shape or dtype != self.dtype:
                raise ValueError(f"{url} (file {k} of {len(self._urls)}): {dtype} {shape} differs from the "
                                 f"first file's {self.dtype} {self._plane_shape}; all files of one array must "
                                 "agree, so bind the files of each shape as their own array")
            a = np.asarray(image.asarray())
        if a.ndim == 3:
            a = np.ascontiguousarray(np.moveaxis(a, 2, 0))
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{url}: decoded to {a.dtype} {a.shape}, not {self.dtype} {self._plane_shape} as "
                             "its tags say")
        self._last = (k, a)
        return a

    def _bytes(self, k: int) -> bytes:
        """File k whole: one request for a file read by range, instead of one per block tifffile seeks to."""
        return _again(lambda: self._whole(self._urls[k]))

    def _whole(self, url: str) -> bytes:
        try:
            with self._io.open(url) as fh:
                return fh.read()
        except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
            return self._io.fetch(url).read_bytes()


def _files(src: Source, io: IO) -> list[str]:
    """Each glob url (`<dir>/::<glob>`, `<zip>::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls, and params.count is checked against the total."""
    count = src.params.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise ValueError(f"params.count must be a positive integer, got {count!r}")
    files = [u for url in src.urls for u in _again(lambda: io.expand(url))]
    if not files:
        raise ValueError("the urls name no file")
    if len(set(files)) != len(files):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    if count is not None and len(files) != count:
        raise ValueError(f"the {len(src.urls)} urls stand for {len(files)} files, params.count says {count}: a "
                         "directory listing is short or has changed")
    return files


def open_array(src: Source, io: IO) -> Array:
    return _Images(io, _files(src, io))
