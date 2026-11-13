"""A z-stack of grey TIFF pages over HTTP: a numbered series of single-page files spread over several folders
(one file per z plane), or one multi-page file (one page per z plane), as zyx.

Each url is a plain file url or a glob over an HTTP directory index (`<dir url>/::<glob>`); `io.expand` resolves
it. The files of one glob are ordered by the numbers in their names (image2.tif before image10.tif), the urls
are concatenated in binding order, and z is the position in that list. So a 600-slice stack kept in six
sub-folders is six urls, not 600. When the urls stand for exactly one file, its pages are the z planes.

Every step that makes requests is tried again when the transport fails (a timeout, a dropped or cut
connection, a 5xx answer): the harness's client gives up after 60 s and does not retry, and one slow answer in
the hundred a crop job needs would otherwise end the job.
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
STACK_AXES = ("YX", "ZYX", "IYX", "QYX")  # tifffile's axes of a grey page or a stack of them
ATTEMPTS = 4                             # tries per step that makes requests
WHOLE = 64 << 20                         # a multi-page file up to this size is read whole at open ...
RUN = 8 << 20                            # ... in range requests of this size
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


class _Series:
    def __init__(self, io: IO, urls: list[str], count: int | None):
        self._io, self._urls = io, urls
        self._stack: tifffile.TiffFile | None = None    # the one multi-page file, kept open
        nz, self._page_shape, self._file_dtype, contiguous = _retry(self._probe)
        if count is not None and nz != count:
            raise ValueError(f"{urls[0]} has {nz} pages, params.count says {count}")
        ny, nx = self._page_shape
        self.shape: tuple[int, ...] = (nz, ny, nx) if nz > 1 else (ny, nx)
        self.axes = "zyx" if nz > 1 else "yx"
        self.dtype = self._file_dtype
        self.voxel_size_nm = None       # plain TIFF resolution tags describe print size, not pixels
        # rows by range in a file per plane; a page of a multi-page file is decoded whole
        self.access = "chunk" if contiguous and self._stack is None else "plane"

    def __del__(self) -> None:          # the harness never closes arrays; a staged file's handle goes with it
        if getattr(self, "_stack", None) is not None:
            self._stack.close()

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
        if self._stack is not None:
            stack = self._stack
            return _retry(lambda: np.asarray(stack.pages[k].asarray())[y, x])
        return _retry(lambda: self._file_plane(k, y, x))

    def _probe(self) -> tuple[int, tuple[int, ...], np.dtype, bool]:
        """(z planes, page shape, dtype, rows readable by range) from the first file's header; a lone
        multi-page file is opened as the stack."""
        tif = self._tiff(0)
        try:
            if len(self._urls) == 1 and tif.pages.is_multipage:
                tif.close()
                tif = self._open_stack()
                s = tif.series[0]
                nz = s.shape[0] if len(s.shape) == 3 else 1
                if len(tif.series) != 1 or s.axes not in STACK_AXES or len(tif.pages) != nz:
                    raise ValueError(f"{self._urls[0]}: tifffile axes {s.axes!r} {tuple(s.shape)}, "
                                     f"{len(tif.pages)} pages in {len(tif.series)} series; this snippet reads "
                                     "one multi-page grey stack with one page per z plane")
                self._stack, tif = tif, None
                return len(self._stack.pages), tuple(s.shape[-2:]), np.dtype(s.dtype), False
            page = self._page(tif, 0)
            return len(self._urls), tuple(page.shape), np.dtype(page.dtype), self._data_offset(page) is not None
        finally:
            if tif is not None:
                tif.close()

    def _open_stack(self) -> tifffile.TiffFile:
        """The one multi-page file. tifffile visits every IFD of a plain multi-page TIFF, and they lie between
        the pages, so a file up to WHOLE is read whole first, in a few large range requests (kept in the job's
        block cache) rather than one request per 1 MiB block the IFD walk touches."""
        url = self._urls[0]
        try:
            fh: BinaryIO = self._io.open(url)
        except NeedsFetch:      # a server without Range: stage the whole file
            return tifffile.TiffFile(self._io.fetch(url))
        size = fh.seek(0, 2)
        if size <= WHOLE:
            for pos in range(0, size, RUN):
                _retry(lambda: (fh.seek(pos), fh.read(RUN)))
        fh.seek(0)
        tif = tifffile.TiffFile(fh)
        len(tif.pages)          # the IFD walk, here where a failure in transit is retried
        return tif

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
                             "page(s); in a series of several files each is one single-page grey YX TIFF")
        page = tif.pages[0]
        if k > 0 and (tuple(page.shape) != self._page_shape or np.dtype(page.dtype) != self._file_dtype):
            raise ValueError(f"{self._urls[k]}: {page.dtype} {tuple(page.shape)} differs from the first file's "
                             f"{self._file_dtype} {self._page_shape}; all files of one series must agree")
        return page

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


def _files(src: Source, io: IO, count: int | None) -> list[str]:
    """The planes' file urls in z order: each url's files in number order, the urls in binding order."""
    files: list[str] = []
    for url in src.urls:
        files += sorted(_retry(lambda: io.expand(url)), key=_natural)
    if not files:
        raise ValueError("tiff-series-glob needs at least one url")
    if len(set(files)) != len(files):
        raise ValueError("the urls name the same file twice")
    if len(files) > 1:
        if count is not None and len(files) != count:
            raise ValueError(f"the urls stand for {len(files)} files, params.count says {count}")
        # the order check: a folder out of place in the binding would shuffle z silently
        numbers = [_counter(f) for f in files]
        for a, b, fa, fb in zip(numbers, numbers[1:], files, files[1:]):
            if a is None or b is None:
                raise ValueError(f"{fa if a is None else fb}: no number in the file name to order the series by")
            if b <= a:
                raise ValueError(f"{fb} (number {b}) follows {fa} (number {a}): the file numbers must increase "
                                 "along the urls; put the urls in series order")
    return files


def open_array(src: Source, io: IO) -> Array:
    count = src.params.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise ValueError(f"params.count must be an integer, got {count!r}")
    return _Series(io, _files(src, io, count), count)
