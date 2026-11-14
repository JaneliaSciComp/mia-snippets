"""A z stack stored as one single-page grey TIFF per slice under consecutively numbered plain URLs.

The binding names the first file and `params.last` the number of the last one; the snippet derives the URLs in
between by counting up the last run of digits in the file name, keeping its zero padding. A binding of a
721-slice volume is then one URL and one param instead of 721 URLs. Without `last`, the URLs are stacked as
given. `open_array` reads the first file's header only; `read` fetches the planes it is asked for.
"""
import re
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

_COUNTER = re.compile(r"(\d+)(\D*)$")       # the last run of digits in the file name, and what follows it


def _numbered(url: str, last: int) -> list[str]:
    """`url`, then the URLs whose counter (the last digit run of the file name) runs up to `last`."""
    cut = url.rfind("/") + 1
    name = url[cut:]
    if "?" in name or "#" in name or "::" in url[url.find("://") + 3:]:
        raise ValueError(f"{url}: params.last counts plain file URLs; no query, fragment or zip member")
    m = _COUNTER.search(name)
    if m is None:
        raise ValueError(f"{url}: the file name has no digits to count up to params.last {last}")
    digits = m.group(1)
    first = int(digits)
    if last < first:
        raise ValueError(f"params.last {last} is below the first file's number {first} ({url})")
    if len(str(last)) > len(digits) and digits.startswith("0"):
        raise ValueError(f"params.last {last} does not fit the {len(digits)} zero-padded digits of {name!r}")
    head, tail = url[:cut] + name[:m.start(1)], m.group(2)
    return [f"{head}{n:0{len(digits)}d}{tail}" for n in range(first, last + 1)]


class _Slices:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        with self._tiff(0) as tif:                      # header only: no voxel data at open
            self._page_shape, self._file_dtype = self._describe(tif, urls[0])
        self.shape: tuple[int, ...] = (len(urls), *self._page_shape) if len(urls) > 1 else self._page_shape
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.dtype = self._file_dtype
        self.access = "plane"           # one file per plane; a y/x region costs that file
        self.voxel_size_nm = None       # plain TIFF resolution tags describe print size, and z is in no file

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0)[y, x]
        sel = region.get("z", slice(None))
        if isinstance(sel, slice):
            ks = range(self.shape[0])[sel]
            if not ks:
                return np.empty((0, *np.empty(self._page_shape, bool)[y, x].shape), self.dtype)
            return np.stack([self._plane(k)[y, x] for k in ks])
        return self._plane(range(self.shape[0])[sel])[y, x]     # IndexError when out of range, like numpy

    def _tiff(self, k: int) -> tifffile.TiffFile:
        url = self._urls[k]
        try:
            fh: BinaryIO = self._io.open(url)
            return tifffile.TiffFile(fh)
        except NeedsFetch:              # a server without Range: stage the whole file
            return tifffile.TiffFile(self._io.fetch(url))

    @staticmethod
    def _describe(tif: tifffile.TiffFile, url: str) -> tuple[tuple[int, ...], np.dtype]:
        s = tif.series[0]
        if len(tif.series) != 1 or s.axes != "YX":
            raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.series)} series; "
                             "this snippet reads one single-page grey (YX) TIFF per slice; see tiff-zip-member "
                             "for stacks and tiff-zip-glob-rgb for RGB tiles")
        return tuple(int(n) for n in s.shape), np.dtype(s.dtype)

    def _plane(self, k: int) -> np.ndarray:
        """The decoded page of file k, checked against file 0."""
        url = self._urls[k]
        with self._tiff(k) as tif:
            shape, dtype = self._describe(tif, url)
            if shape != self._page_shape or dtype != self._file_dtype:
                raise ValueError(f"{url}: {dtype} {shape} differs from the first file's {self._file_dtype} "
                                 f"{self._page_shape}; all slices of one array must agree")
            return np.asarray(tif.series[0].asarray())


def open_array(src: Source, io: IO) -> Array:
    urls, last = list(src.urls), src.params.get("last")
    if not urls:
        raise ValueError("no urls")
    if last is not None:
        if isinstance(last, bool) or not isinstance(last, int):
            raise ValueError(f"params.last must be an integer file number, got {last!r}")
        if len(urls) != 1:
            raise ValueError(f"params.last counts up from one url (the first file); got {len(urls)} urls")
        urls = _numbered(urls[0], last)
    if len(set(urls)) != len(urls):
        raise ValueError("the urls name the same file twice")
    return _Slices(io, urls)
