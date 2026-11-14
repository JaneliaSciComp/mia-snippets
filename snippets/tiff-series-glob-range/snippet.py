"""A run of a numbered series of single-page grey TIFF files over HTTP (one file per z plane), as zyx: the
files whose numbers lie between params.first and params.last.

It is for a series whose files do not all have one yx shape (a FIB-SEM run whose frame was resized on the
way). Such a folder is not one array; it is one array per stretch of files of one shape, and a binding names
each stretch by the numbers of its first and last file, with the same urls.

Each url is a plain file url or a glob over an HTTP directory index (`<dir url>/::<glob>`); `io.expand` resolves
it. The files of one glob are ordered by the numbers in their names (image2.tif before image10.tif), the urls
are concatenated in binding order, the files outside first..last are dropped, and z is the position in what is
left. The array's yx shape is the first file's; open also reads the last file's header and refuses a run whose
two ends differ, and `read` refuses any file inside it that differs, so a run across a shape change is never
read as one array.

Every step that makes requests is tried again when the transport fails (a timeout, a dropped or cut
connection, a 5xx answer): the harness's client gives up after 60 s, and one slow answer in the hundred a
crop job needs would otherwise end the job.
"""
import re
import time
from typing import BinaryIO, Callable, TypeVar

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

COUNTER = re.compile(r"\d+(?=\D*$)")     # the last digit run of the file name
DIGITS = re.compile(r"(\d+)")
ATTEMPTS = 4                             # tries per step that makes requests
T = TypeVar("T")


def _transient(e: Exception) -> bool:
    """A failure worth asking again for: the HTTP client's transport errors (timeouts, a connection dropped or
    cut inside a body) and 5xx/429 answers. Told by class name, since a snippet may not import the client."""
    names = {c.__name__ for c in type(e).__mro__}
    status = getattr(getattr(e, "response", None), "status_code", None)
    return "TransportError" in names or ("HTTPStatusError" in names and isinstance(status, int)
                                         and (status >= 500 or status == 429))


def _retry(step: Callable[[], T]) -> T:
    """`step()`, tried up to ATTEMPTS times while it fails in transit; any other error is raised at once."""
    for attempt in range(ATTEMPTS):
        try:
            return step()
        except Exception as e:      # BudgetExceeded, HostNotAllowed, NeedsFetch, ValueError: not transient
            if attempt == ATTEMPTS - 1 or not _transient(e):
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


class _Run:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._page_shape, self._file_dtype, contiguous = _retry(lambda: self._header(0))
        if len(urls) > 1:
            shape, dtype, _ = _retry(lambda: self._header(len(urls) - 1))
            if shape != self._page_shape or dtype != self._file_dtype:
                raise ValueError(
                    f"the run's last file {urls[-1]} is {dtype} {shape} and its first file {urls[0]} is "
                    f"{self._file_dtype} {self._page_shape}: the files of one array must agree. Bind each "
                    "stretch of one shape as its own array, with params.first and params.last")
        nz, (ny, nx) = len(urls), self._page_shape
        self.shape: tuple[int, ...] = (nz, ny, nx) if nz > 1 else (ny, nx)
        self.axes = "zyx" if nz > 1 else "yx"
        self.dtype = self._file_dtype
        self.voxel_size_nm = None       # plain TIFF resolution tags describe print size, not pixels
        self.access = "chunk" if contiguous else "plane"    # rows by range, or the page decoded whole

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0, y, x)
        sel = region.get("z", slice(None))
        if isinstance(sel, slice):
            planes = [self._plane(k, y, x) for k in range(self.shape[0])[sel]]
            return np.stack(planes) if planes else np.empty((0, *self._page_shape), self.dtype)[:, y, x]
        return self._plane(range(self.shape[0])[sel], y, x)     # IndexError when out of range, like numpy

    def _plane(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
        return _retry(lambda: self._file_plane(k, y, x))

    def _tiff(self, k: int) -> tifffile.TiffFile:
        url = self._urls[k]
        try:
            fh: BinaryIO = self._io.open(url)
            return tifffile.TiffFile(fh)
        except NeedsFetch:      # a server without Range: stage the whole file
            return tifffile.TiffFile(self._io.fetch(url))

    def _page(self, tif: tifffile.TiffFile, k: int) -> tifffile.TiffPage:
        s = tif.series[0]
        if len(tif.series) != 1 or len(tif.pages) != 1 or s.axes != "YX":
            raise ValueError(f"{self._urls[k]}: tifffile axes {s.axes!r} {tuple(s.shape)}, {len(tif.pages)} "
                             "page(s); each file of the series must be one single-page grey YX TIFF")
        return tif.pages[0]

    def _header(self, k: int) -> tuple[tuple[int, ...], np.dtype, bool]:
        """(page shape, dtype, rows readable by range) of file k."""
        with self._tiff(k) as tif:
            page = self._page(tif, k)
            return tuple(page.shape), np.dtype(page.dtype), self._data_offset(page) is not None

    @staticmethod
    def _data_offset(page: tifffile.TiffPage) -> int | None:
        """File offset of the pixel block when it is stored raw in one run (rows can be read by range)."""
        if page.compression != 1 or page.predictor != 1 or page.fillorder != 1 or page.is_tiled:
            return None
        offsets, counts = page.dataoffsets, page.databytecounts      # strips, in row order
        size = int(np.prod(page.shape)) * np.dtype(page.dtype).itemsize
        if not offsets or len(offsets) != len(counts) or sum(counts) != size:
            return None
        if any(o + c != nxt for o, c, nxt in zip(offsets, counts, offsets[1:])):
            return None
        return int(offsets[0])

    def _file_plane(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
        with self._tiff(k) as tif:
            page = self._page(tif, k)
            if tuple(page.shape) != self._page_shape or np.dtype(page.dtype) != self._file_dtype:
                raise ValueError(
                    f"{self._urls[k]} (file {k} of the run, number {_counter(self._urls[k])}): {page.dtype} "
                    f"{tuple(page.shape)} differs from the run's first file's {self._file_dtype} "
                    f"{self._page_shape}; all files of one array must agree. End this array before that file "
                    "(params.last) and bind the files from it on as another array (params.first)")
            offset = self._data_offset(page)
            if offset is None:
                return np.asarray(page.asarray())[y, x]
            ny, nx = self._page_shape
            rows = range(ny)[y] if isinstance(y, slice) else range(range(ny)[y], range(ny)[y] + 1)
            if len(rows) == 0:
                return np.empty((ny, nx), self.dtype)[y, x]
            lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
            stored = np.dtype(page.dtype).newbyteorder(tif.byteorder)
            fh = tif.filehandle
            fh.seek(offset + lo * nx * stored.itemsize)
            buf = fh.read((hi - lo) * nx * stored.itemsize)
        if len(buf) != (hi - lo) * nx * stored.itemsize:
            raise ValueError(f"{self._urls[k]}: file ends inside the pixel data")
        block = np.frombuffer(buf, stored).reshape(hi - lo, nx).astype(self.dtype, copy=False)
        if not isinstance(y, slice):
            return block[0, x]
        picked = block if rows.step == 1 else block[np.arange(rows.start, rows.stop, rows.step) - lo]
        return picked[:, x]


def _natural(url: str) -> list[tuple[int, str]]:
    """Sort key that compares the digit runs of a name as numbers: image2.tif < image10.tif."""
    return [(int(part), "") if part.isdigit() else (-1, part) for part in DIGITS.split(url)]


def _counter(url: str) -> int | None:
    """The number of a file in its series: the last digit run of the file name, or None without one."""
    m = COUNTER.search(url, url.rfind("/") + 1)
    return int(m.group()) if m else None


def _int(params: dict, name: str) -> int | None:
    value = params.get(name)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"params.{name} must be an integer, got {value!r}")
    return value


def _files(src: Source, io: IO) -> list[str]:
    """The run's file urls in z order: each url's files in number order, the urls in binding order, then the
    files numbered first..last of that list."""
    first, last, count = _int(src.params, "first"), _int(src.params, "last"), _int(src.params, "count")
    files: list[str] = []
    for url in src.urls:
        files += sorted(_retry(lambda: io.expand(url)), key=_natural)
    if not files:
        raise ValueError("tiff-series-glob-range needs at least one url")
    if len(set(files)) != len(files):
        raise ValueError("the urls name the same file twice")
    numbers = [_counter(f) for f in files]
    if len(files) > 1 or first is not None or last is not None:
        # the order check: a folder out of place in the binding would shuffle z silently
        for f, n in zip(files, numbers):
            if n is None:
                raise ValueError(f"{f}: no number in the file name to order the series by")
        for a, b, fa, fb in zip(numbers, numbers[1:], files, files[1:]):
            if b <= a:
                raise ValueError(f"{fb} (number {b}) follows {fa} (number {a}): the file numbers must increase "
                                 "along the urls; put the urls in series order")
    for name, bound in (("first", first), ("last", last)):
        if bound is not None and bound not in numbers:
            raise ValueError(f"params.{name} is {bound}, but none of the {len(files)} files the urls stand for "
                             f"has that number (they run from {numbers[0]} to {numbers[-1]})")
    if first is not None and last is not None and last < first:
        raise ValueError(f"params.last {last} is before params.first {first}")
    run = [f for f, n in zip(files, numbers)
           if (first is None or n >= first) and (last is None or n <= last)]
    if count is not None and len(run) != count:
        bounds = "" if first is None and last is None else (
            f" numbered {numbers[0] if first is None else first} to {numbers[-1] if last is None else last}")
        raise ValueError(f"the urls stand for {len(run)} files{bounds}, params.count says {count}")
    return run


def open_array(src: Source, io: IO) -> Array:
    return _Run(io, _files(src, io))
