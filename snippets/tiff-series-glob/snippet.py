"""A numbered series of single-page TIFF files spread over several folders, one file per z plane, stacked as zyx.

Each url is a plain file url or a glob over an HTTP directory index (`<dir url>/::<glob>`); `io.expand` resolves
it. The files of one glob are ordered by the numbers in their names (image2.tif before image10.tif), the urls
are concatenated in binding order, and z is the position in that list. So a 600-slice stack kept in six
sub-folders is six urls, not 600. `open_array` reads the directory indexes and the first file's header only;
`read` opens the files it needs, and reads an uncompressed strip-contiguous page by row range instead of whole.
"""
import re
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

COUNTER = re.compile(r"\d+(?=\D*$)")     # the last digit run of the file name
DIGITS = re.compile(r"(\d+)")


class _Series:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        with self._tiff(0) as tif:
            page = self._page(tif, 0)
            self._page_shape, self._file_dtype = tuple(page.shape), np.dtype(page.dtype)
            contiguous = self._data_offset(page) is not None
        ny, nx = self._page_shape
        self.shape: tuple[int, ...] = (len(urls), ny, nx) if len(urls) > 1 else (ny, nx)
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.dtype = self._file_dtype
        self.voxel_size_nm = None       # plain TIFF resolution tags describe print size, not pixels
        self.access = "chunk" if contiguous else "plane"    # rows by range, or the whole decoded page

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0, y, x)
        sel = region.get("z", slice(None))
        if isinstance(sel, slice):
            planes = [self._plane(k, y, x) for k in range(self.shape[0])[sel]]
            return np.stack(planes) if planes else np.empty((0, *self._page_shape), self.dtype)[:, y, x]
        return self._plane(range(self.shape[0])[sel], y, x)     # IndexError when out of range, like numpy

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
                             "page(s); this snippet reads one single-page grey YX TIFF per z plane")
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

    def _plane(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
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


def _files(src: Source, io: IO) -> list[str]:
    """The planes' file urls in z order: each url's files in number order, the urls in binding order."""
    files: list[str] = []
    for url in src.urls:
        files += sorted(io.expand(url), key=_natural)
    if not files:
        raise ValueError("tiff-series-glob needs at least one url")
    if len(set(files)) != len(files):
        raise ValueError("the urls name the same file twice")
    count = src.params.get("count")
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError(f"params.count must be an integer, got {count!r}")
        if len(files) != count:
            raise ValueError(f"the urls stand for {len(files)} files, params.count says {count}")
    if len(files) > 1:      # the order check: a folder out of place in the binding would shuffle z silently
        numbers = [_counter(f) for f in files]
        for a, b, fa, fb in zip(numbers, numbers[1:], files, files[1:]):
            if a is None or b is None:
                raise ValueError(f"{fa if a is None else fb}: no number in the file name to order the series by")
            if b <= a:
                raise ValueError(f"{fb} (number {b}) follows {fa} (number {a}): the file numbers must increase "
                                 "along the urls; put the urls in series order")
    return files


def open_array(src: Source, io: IO) -> Array:
    return _Series(io, _files(src, io))
