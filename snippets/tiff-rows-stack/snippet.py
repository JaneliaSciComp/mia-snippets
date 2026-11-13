"""Single-page greyscale TIFFs, one file per z plane (plain URLs or zip members, members also by glob), stacked
along z. An uncompressed page is read by rows: one byte range per patch, located from the strip offsets in the
IFD, instead of the whole page. A compressed page or a deflated zip member is decoded whole, as a plane.

Files are opened lazily: `open_array` reads the first file's header only; `read` opens the files it needs.
"""
from fnmatch import fnmatchcase
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

GLOB_CHARS = "*?["


class _Slices:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        tif, ranged = self._open(0)
        with tif:
            page, rows = self._page(tif, 0)
            ny, nx = page.shape
            self._page_shape = (ny, nx)                     # every file must match the first
            self._file_dtype = np.dtype(page.dtype)
        self.dtype = self._file_dtype
        self.shape = (len(urls), ny, nx) if len(urls) > 1 else (ny, nx)
        self.axes = "zyx" if len(urls) > 1 else "yx"
        # the first file speaks for the stack: rows by range -> a patch costs its rows ("chunk"); a compressed
        # page or a file staged whole (deflated member) costs the file for any y/x region ("plane"). A later
        # file that differs is still read correctly, each file by what it is.
        self.access = "chunk" if rows and ranged else "plane"
        self.voxel_size_nm = None                           # ponytail: resolution tags are not parsed

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0, y, x)
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            return np.stack([self._plane(k, y, x) for k in range(self.shape[0])[z]])
        return self._plane(range(self.shape[0])[z], y, x)   # IndexError when out of range, like numpy

    def _plane(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
        """Plane k indexed [y, x], fetching only rows min(y)..max(y) when the page can be read by rows."""
        ny, nx = self._page_shape
        tif, _ = self._open(k)
        with tif:
            page, by_rows = self._page(tif, k)
            if not by_rows:
                return np.asarray(page.asarray())[y, x]
            rows = range(ny)[y]                             # normalises negatives; IndexError like numpy
            if isinstance(rows, int):
                lo, hi, pick = rows, rows + 1, 0
            elif len(rows) == 0:
                return np.empty((0, nx), self.dtype)[:, x]
            else:
                lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
                pick = np.asarray(rows) - lo
            file_dtype = np.dtype(tif.byteorder + self.dtype.char)
            row = nx * file_dtype.itemsize
            fh = tif.filehandle
            fh.seek(page.dataoffsets[0] + lo * row)         # rows lo..hi: one byte range
            buf = fh.read((hi - lo) * row)
            if len(buf) != (hi - lo) * row:
                raise ValueError(f"{self._urls[k]}: file ends inside rows {lo}..{hi}")
            block = np.frombuffer(buf, file_dtype).reshape(hi - lo, nx)
            return block[pick][..., x].astype(self.dtype)

    def _open(self, k: int) -> tuple[tifffile.TiffFile, bool]:
        """(file k, whether it is read by range requests rather than staged whole)."""
        url = self._urls[k]
        try:
            fh: BinaryIO = self._io.open(url)
            return tifffile.TiffFile(fh), True
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            return tifffile.TiffFile(self._io.fetch(url)), False

    def _page(self, tif: tifffile.TiffFile, k: int) -> tuple[tifffile.TiffPage, bool]:
        """(the file's one page, whether its rows sit in the file in order, uncompressed, back to back)."""
        url = self._urls[k]
        s = tif.series[0]
        if len(tif.series) != 1 or len(tif.pages) != 1 or s.axes != "YX":
            raise ValueError(f"{url}: {len(tif.pages)} pages, tifffile axes {s.axes!r} {tuple(s.shape)}; this "
                             "snippet reads single-page greyscale YX files only (tiff-zip-member for stacks, "
                             "tiff-zip-glob-rgb for RGB tiles)")
        page = tif.pages[0]
        if k > 0 and (tuple(page.shape) != self._page_shape or np.dtype(page.dtype) != self._file_dtype):
            raise ValueError(f"{url}: {page.dtype} {tuple(page.shape)} differs from the first file's "
                             f"{self._file_dtype} {self._page_shape}; all files of one array must agree")
        nbytes = page.shape[0] * page.shape[1] * np.dtype(page.dtype).itemsize
        by_rows = bool(page.is_final and not page.is_tiled and page.samplesperpixel == 1
                       and sum(page.databytecounts) == nbytes)
        return page, by_rows


def _split(url: str) -> tuple[str, str | None]:
    """(file url, zip member or None): `::` after the path starts, as the harness writes zip members."""
    path = url.find("/", url.find("://") + 3)
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each `<zip>::<glob>` becomes its matching members, sorted by name; other urls pass through in order."""
    out: list[str] = []
    for u in urls:
        zip_url, member = _split(u)
        if member is not None and any(ch in member for ch in GLOB_CHARS):
            names = sorted(n for n in io.list(zip_url) if fnmatchcase(n, member))
            if not names:
                raise ValueError(f"no member of {zip_url} matches {member!r}")
            out += [f"{zip_url}::{n}" for n in names]
        else:
            out.append(u)
    if not out:
        raise ValueError("tiff-rows-stack needs at least one url")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    return _Slices(io, _expand(src.urls, io))
