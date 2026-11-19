"""A set of multi-page channel TIFFs (PerkinElmer/Akoya QPTIFF component files and the like), one file per z:
each file's full-resolution channel pages, tifffile's series[0] with axes CYX, become one `cyx` plane stack, and
the files, chosen by glob over an HTTP directory index or a zip (or listed one by one), are stacked as `zcyx`.

The other series of a file (a QPTIFF's RGB thumbnail, its reduced-resolution pages, macro and label images) are
not read. Files are opened lazily: `open_array` lists the files and reads the first one's header; `read` opens
the files it needs, checks each against the first (channel count, yx shape, dtype, pixel size) and, for a tiled
page, fetches only the tiles the region touches.

One array is files of one shape. A set whose files differ in yx shape is bound as one array per shape, and when
the names do not tell the shapes apart (tissue microarray cores cut out at three field sizes), each array lists
its files as plain urls.
"""
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

UNIT_NM = {2: 25.4e6, 3: 1e7}       # TIFF ResolutionUnit: 2 inch, 3 centimetre, in nm
PIXEL_TOLERANCE = 1e-3              # relative difference in pixel size that counts as another pixel size


class _Files:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._open: tuple[int, tifffile.TiffFile, list[tifffile.TiffPage]] | None = None   # the last file opened
        self._plane_shape: tuple[int, ...] | None = None
        pages = self._pages(0)
        page = pages[0]
        channels = (len(pages),) if len(pages) > 1 else ()
        self._plane_shape = channels + tuple(page.shape)        # (C, Y, X) or (Y, X); every file must match
        self.dtype = np.dtype(page.dtype)
        self.voxel_size_nm = _pixel_size(self._open[1], page)
        many = len(urls) > 1
        self.axes = ("z" if many else "") + ("c" if channels else "") + "yx"
        self.shape: tuple[int, ...] = ((len(urls),) if many else ()) + self._plane_shape
        # a tiled page is read tile by tile, by range; a stripped one is decoded whole. A file staged with
        # io.fetch (a deflated zip member) has been paid for whole, whatever is read from it
        self.access = "file" if self._staged else "chunk" if all(_tiled(p) for p in pages) else "plane"

    def __del__(self) -> None:      # the harness never closes arrays; the open file's handle goes with the array
        if getattr(self, "_open", None) is not None:
            self._open[1].close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if "z" not in self.axes:
            return self._file(0, region)
        n = len(self._urls)
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._file(range(n)[z], region)      # IndexError when out of range, like numpy
        parts = [self._file(k, region) for k in range(n)[z]]
        if not parts:
            idx = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(parts)

    def _file(self, k: int, region: dict[str, int | slice]) -> np.ndarray:
        """The region of file k, as (c, y, x) without the axes the region indexes by an integer."""
        pages = self._pages(k)
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "c" not in self.axes:
            return self._window(pages[0], y, x)
        c = region.get("c", slice(None))
        if not isinstance(c, slice):
            return self._window(pages[range(len(pages))[c]], y, x)
        parts = [self._window(pages[i], y, x) for i in range(len(pages))[c]]
        if not parts:
            return np.empty((0,) + np.empty(self._plane_shape[1:], self.dtype)[y, x].shape, self.dtype)
        return np.stack(parts)

    def _pages(self, k: int) -> list[tifffile.TiffPage]:
        """The channel pages of file k, checked against file 0. The file stays open until another is asked for."""
        if self._open is not None and self._open[0] == k:
            return self._open[2]
        if self._open is not None:
            self._open[1].close()
            self._open = None
        url = self._urls[k]
        try:
            fh: BinaryIO = self._io.open(url)
            tif, self._staged = tifffile.TiffFile(fh), False
        except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
            tif, self._staged = tifffile.TiffFile(self._io.fetch(url)), True
        try:
            pages = self._checked(tif, url)
        except BaseException:
            tif.close()
            raise
        self._open = (k, tif, pages)
        return pages

    def _checked(self, tif: tifffile.TiffFile, url: str) -> list[tifffile.TiffPage]:
        s = tif.series[0]
        if s.axes not in ("CYX", "YX"):
            raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)}; this snippet reads files whose "
                             "first series is channel pages (CYX, or YX for one channel), see tiff-zip-member-2 "
                             "for z stacks and image-tiles-glob for RGB tiles")
        pages = list(s.pages)
        want = (s.shape[0], s.shape[1:]) if s.axes == "CYX" else (1, s.shape)
        if len(pages) != want[0] or any(p is None or tuple(p.shape) != tuple(want[1]) for p in pages):
            raise ValueError(f"{url}: series[0] {s.axes} {tuple(s.shape)} is not one YX page per channel")
        if self._plane_shape is None:       # file 0: the others are compared with it
            return pages
        shape = ((len(pages),) if len(pages) > 1 else ()) + tuple(pages[0].shape)
        dtype = np.dtype(pages[0].dtype)
        if shape != self._plane_shape or dtype != self.dtype:
            raise ValueError(f"{url}: {dtype} {shape} differs from the first file's {self.dtype} "
                             f"{self._plane_shape} (c, y, x); all files of one array must agree, so bind the "
                             "files of another shape as their own array (a narrower glob)")
        size = _pixel_size(tif, pages[0])
        if (size is None) != (self.voxel_size_nm is None) or (size is not None and any(
                abs(size[a] - self.voxel_size_nm[a]) > PIXEL_TOLERANCE * self.voxel_size_nm[a] for a in "yx")):
            raise ValueError(f"{url}: pixel size {size} nm differs from the first file's {self.voxel_size_nm} "
                             f"({self._urls[0]}); all files of one array must agree")
        return pages

    def _window(self, page: tifffile.TiffPage | tifffile.TiffFrame, y: int | slice, x: int | slice) -> np.ndarray:
        """page[y, x], decoding only the tiles the window touches when the page is tiled."""
        ny, nx = page.shape
        rows = range(ny)[y] if isinstance(y, slice) else range(range(ny)[y], range(ny)[y] + 1)
        cols = range(nx)[x] if isinstance(x, slice) else range(range(nx)[x], range(nx)[x] + 1)
        if len(rows) == 0 or len(cols) == 0:
            return np.empty((ny, nx), self.dtype)[y, x]
        if not _tiled(page):
            return np.asarray(page.asarray())[y, x]
        y0, y1 = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
        x0, x1 = min(cols[0], cols[-1]), max(cols[0], cols[-1]) + 1
        key = page.keyframe     # a TiffFrame (a later page of an OME or ImageJ series) keeps its layout there
        th, tw = key.tilelength, key.tilewidth
        across = -(-nx // tw)
        block = np.zeros((y1 - y0, x1 - x0), self.dtype)        # a tile without data is zeros, as tifffile reads it
        fh = page.parent.filehandle
        for ty in range(y0 // th, -(-y1 // th)):
            for tx in range(x0 // tw, -(-x1 // tw)):
                index = ty * across + tx
                offset, count = page.dataoffsets[index], page.databytecounts[index]
                if not count:
                    continue
                fh.seek(offset)
                data = fh.read(count)
                if len(data) != count:
                    raise ValueError(f"{fh.name}: file ends inside tile {index} of page {page.index}")
                tile = key.decode(data, index)[0]
                if tile is None:
                    continue
                tile = np.asarray(tile).reshape(tile.shape[-3], tile.shape[-2])     # (depth 1, h, w, sample 1)
                ya, yb = max(y0, ty * th), min(y1, ty * th + tile.shape[0])
                xa, xb = max(x0, tx * tw), min(x1, tx * tw + tile.shape[1])
                block[ya - y0:yb - y0, xa - x0:xb - x0] = tile[ya - ty * th:yb - ty * th, xa - tx * tw:xb - tx * tw]
        return block[np.ix_(np.asarray(rows) - y0, np.asarray(cols) - x0)][
            slice(None) if isinstance(y, slice) else 0, slice(None) if isinstance(x, slice) else 0]


def _tiled(page: tifffile.TiffPage | tifffile.TiffFrame) -> bool:
    """True for a tiled page of one sample and one depth plane: the layout `_window` reads tile by tile."""
    key = page.keyframe
    return bool(key.is_tiled and key.samplesperpixel == 1 and key.imagedepth == 1 and key.tiledepth == 1
                and len(page.dataoffsets) == -(-key.imagelength // key.tilelength)
                * -(-key.imagewidth // key.tilewidth))


def _pixel_size(tif: tifffile.TiffFile, page: tifffile.TiffPage) -> dict[str, float] | None:
    """Pixel size from X/YResolution, for a QPTIFF only: that format defines the tags as the scanner's pixels per
    unit. In any other TIFF they describe print size, so the answer is None."""
    if not tif.is_qpi:
        return None
    unit = UNIT_NM.get(int(page.tags["ResolutionUnit"].value)) if "ResolutionUnit" in page.tags else None
    out: dict[str, float] = {}
    for a, name in (("y", "YResolution"), ("x", "XResolution")):
        num, den = page.tags[name].value if name in page.tags else (0, 0)
        if unit and num and den:
            out[a] = unit * den / num
    return out if len(out) == 2 else None


def _files(src: Source, io: IO) -> list[str]:
    """The file urls in z order: each glob url (`<dir>/::<glob>`, `<zip>::<glob>`) as its matches sorted by name,
    the urls in binding order."""
    files = [u for url in src.urls for u in io.expand(url)]
    if not files:
        raise ValueError("tiff-channels-glob needs at least one url")
    if len(set(files)) != len(files):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return files


def open_array(src: Source, io: IO) -> Array:
    return _Files(io, _files(src, io))
