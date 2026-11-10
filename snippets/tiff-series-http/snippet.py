"""A numbered series of single-page TIFF files over HTTP, one file per z plane, stacked as zyx.

The binding names the first file only; `params.last` is the number in the last file's name. The counter is the
last run of digits in the URL's file name, counted up one file at a time with the same zero padding, so a
1,937-slice stack is one URL and one integer instead of 1,937 URLs. Without `last`, the urls are the planes,
in binding order. `open_array` reads the first file's header only; `read` opens the files it needs, and reads
an uncompressed strip-contiguous page by row range instead of whole.
"""
import re
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

COUNTER = re.compile(r"\d+(?=\D*$)")     # the last digit run of the file name


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
            return np.stack([self._plane(k, y, x) for k in range(self.shape[0])[sel]])
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


def _numbered(url: str, last: int) -> list[str]:
    """`url` and its successors up to counter `last`: the last digit run of the file name, same zero padding."""
    name_at = url.rfind("/") + 1
    m = COUNTER.search(url, name_at)
    if "::" in url[url.find("://") + 3:] or m is None:
        raise ValueError(f"params.last needs a plain file url whose name holds a number, got {url!r}")
    first, width = int(m.group()), len(m.group())
    if last < first:
        raise ValueError(f"params.last {last} is below the first file's number {first}")
    if len(str(last)) > width and m.group().startswith("0"):
        raise ValueError(f"params.last {last} does not fit the zero-padded width {width} of {url!r}")
    pad = width if m.group().startswith("0") else 0
    return [f"{url[:m.start()]}{n:0{pad}d}{url[m.end():]}" for n in range(first, last + 1)]


def open_array(src: Source, io: IO) -> Array:
    last = src.params.get("last")
    urls = list(src.urls)
    if not urls:
        raise ValueError("tiff-series-http needs at least one url")
    if last is not None:
        if isinstance(last, bool) or not isinstance(last, int):
            raise ValueError(f"params.last must be an integer, got {last!r}")
        if len(urls) != 1:
            raise ValueError(f"params.last counts up from one url (the first file), got {len(urls)} urls")
        urls = _numbered(urls[0], last)
    if len(set(urls)) != len(urls):
        raise ValueError("the urls name the same file twice")
    return _Series(io, urls)
