"""The full-resolution level of one whole-slide image that is a tiled TIFF (Aperio SVS, also as BigTIFF;
Hamamatsu NDPI), read tile by tile over range requests, as cyx (colour) or yx (grey).

A slide is tens of gigabytes decoded, so nothing here ever decodes a page: `open_array` reads the first IFD
(the level-0 page; the pyramid's other pages are never visited) and `read` fetches and decodes only the tiles
a region touches. tifffile supplies the page's tile table and the tile decoder (JPEG tables, YCbCr to RGB, and
the restart intervals of an NDPI's single JPEG stream, which it presents as tiles); the bytes come through `io`.

One slide is one array: slides differ in shape, so a folder of them is not a stack. The url is one file, or a
`<dir>/::<glob>` whose match number `index` (in name order) is the slide.
"""
import re
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

GAP = 1 << 20          # tiles closer than this in the file are fetched in one read (io's block size)
RUN = 32 << 20         # ... up to this many bytes per read
MPP = re.compile(r"\|\s*MPP\s*=\s*([0-9.eE+-]+)")     # Aperio ImageDescription: microns per pixel


class _Slide:
    def __init__(self, tif: tifffile.TiffFile, url: str, access: str):
        self._tif, self._url, self.access = tif, url, access
        page = self._page = tif.pages[0]            # level 0; only this IFD is read
        if not isinstance(page, tifffile.TiffPage):
            raise ValueError(f"{url}: the first IFD is not an image page")
        if page.axes not in ("YXS", "YX") or page.tiledepth != 1 or page.planarconfig != 1:
            raise ValueError(f"{url}: first page has tifffile axes {page.axes!r} {tuple(page.shape)}, planar "
                             f"configuration {page.planarconfig}; this snippet reads a 2D page with contiguous "
                             "samples")
        if not page.is_tiled:
            raise ValueError(f"{url}: the first page is not tiled ({page.compression!r}, {tuple(page.shape)}); "
                             "tiff-zip-member-2 reads a stripped page whole")
        self._ny, self._nx = int(page.imagelength), int(page.imagewidth)
        self._th, self._tw = int(page.tilelength), int(page.tilewidth)
        self._across, down = -(-self._nx // self._tw), -(-self._ny // self._th)
        if not len(page.dataoffsets) == len(page.databytecounts) == self._across * down:
            raise ValueError(f"{url}: {len(page.dataoffsets)} tile offsets and {len(page.databytecounts)} byte "
                             f"counts for {down}x{self._across} tiles of {self._th}x{self._tw}")
        self._samples = int(page.shape[2]) if page.axes == "YXS" else None
        self.axes = "yx" if self._samples is None else "cyx"
        self.shape: tuple[int, ...] = (self._ny, self._nx) if self._samples is None \
            else (self._samples, self._ny, self._nx)
        self.dtype = np.dtype(page.dtype)
        self.voxel_size_nm = _voxel_size(page)

    def __del__(self) -> None:      # the harness never closes arrays; the file handle goes with the array
        if getattr(self, "_tif", None) is not None:
            self._tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ysel, y0, y1 = _span(region.get("y", slice(None)), self._ny)
        xsel, x0, x1 = _span(region.get("x", slice(None)), self._nx)
        block = self._box(y0, y1, x0, x1)[ysel][:, xsel] if isinstance(ysel, slice) \
            else self._box(y0, y1, x0, x1)[ysel][xsel]          # (y, x, samples) with y and x as asked
        if self._samples is None:
            return block
        return np.moveaxis(block, -1, 0)[region.get("c", slice(None))]

    def _box(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        """Pixels [y0, y1) x [x0, x1) as (y, x) or (y, x, samples), from the tiles that cover them."""
        out = np.zeros((y1 - y0, x1 - x0) + (() if self._samples is None else (self._samples,)), self.dtype)
        if out.size == 0:
            return out
        th, tw, page = self._th, self._tw, self._page
        tiles = [ty * self._across + tx for ty in range(y0 // th, (y1 - 1) // th + 1)
                 for tx in range(x0 // tw, (x1 - 1) // tw + 1)]
        for index, data in self._stored(tiles):
            tile = page.decode(data, index, jpegtables=page.jpegtables, jpegheader=page.jpegheader,
                               _fullsize=True)[0]
            if tile is None:        # a tile the file does not store stays 0, as tifffile fills it
                continue
            tile = np.asarray(tile).reshape((th, tw) + out.shape[2:])       # also what tells a bad decode
            if tile.dtype != self.dtype:
                raise ValueError(f"{self._url}: tile {index} decodes to {tile.dtype}, the page says {self.dtype}")
            ty, tx = divmod(index, self._across)
            top, left = ty * th, tx * tw                                     # the tile's origin in the page
            ya, yb, xa, xb = max(y0, top), min(y1, top + th), max(x0, left), min(x1, left + tw)
            out[ya - y0:yb - y0, xa - x0:xb - x0] = tile[ya - top:yb - top, xa - left:xb - left]
        return out

    def _stored(self, tiles: list[int]) -> list[tuple[int, bytes | None]]:
        """(tile index, its stored bytes or None for a tile of no bytes), in file order. Tiles that lie close
        together in the file come from one read, so `io` asks for their blocks in one request."""
        offsets, counts = self._page.dataoffsets, self._page.databytecounts
        fh: BinaryIO = self._tif.filehandle
        out: list[tuple[int, bytes | None]] = [(i, None) for i in tiles if counts[i] == 0]
        todo = sorted((offsets[i], counts[i], i) for i in tiles if counts[i] > 0)
        k = 0
        while k < len(todo):
            start = todo[k][0]
            end, n = start + todo[k][1], k + 1
            while n < len(todo) and todo[n][0] - end <= GAP and todo[n][0] + todo[n][1] - start <= RUN:
                end, n = max(end, todo[n][0] + todo[n][1]), n + 1
            fh.seek(start)
            buf = fh.read(end - start)
            if len(buf) != end - start:
                raise ValueError(f"{self._url}: file ends inside tile data at offset {start}")
            out += [(i, buf[o - start:o - start + c]) for o, c, i in todo[k:n]]
            k = n
        return out


def _span(sel: int | slice, n: int) -> tuple[int | slice, int, int]:
    """A selection on an axis of n pixels as (the same selection inside the box, box start, box stop), the box
    being the pixels [start, stop) the selection lies in."""
    if not isinstance(sel, slice):
        k = range(n)[sel]           # IndexError when out of range, like numpy
        return 0, k, k + 1
    r = range(n)[sel]
    if len(r) == 0:
        return slice(0, 0), 0, 0
    lo, hi = min(r[0], r[-1]), max(r[0], r[-1]) + 1
    stop = r[0] - lo + r.step * len(r)
    return slice(r[0] - lo, stop if stop >= 0 else None, r.step), lo, hi


def _voxel_size(page: tifffile.TiffPage) -> dict[str, float] | None:
    """The scanner's pixel size: `MPP` of an Aperio (SVS) description, or the resolution tags of an NDPI, which
    are the scan's pixels per centimetre. None for anything else: plain TIFF resolution describes print size."""
    m = MPP.search(page.description or "")
    if m:
        try:
            mpp = float(m.group(1))
        except ValueError:
            return None
        return {"y": mpp * 1e3, "x": mpp * 1e3} if mpp > 0 else None
    if not page.is_ndpi or page.tags.valueof(296) != 3:        # ResolutionUnit: centimetre
        return None
    out: dict[str, float] = {}
    for axis, code in (("y", 283), ("x", 282)):                # YResolution, XResolution
        value = page.tags.valueof(code)
        if isinstance(value, tuple) and len(value) == 2 and value[0] > 0 and value[1] > 0:
            out[axis] = 1e7 * value[1] / value[0]              # nm per pixel from pixels per cm
    return out if len(out) == 2 else None


def _slide_url(src: Source, io: IO) -> str:
    """The one file the binding names: its url, or match number `index` of its glob."""
    if len(src.urls) != 1:
        raise ValueError(f"wsi-tiff-tiles reads one slide per array, got {len(src.urls)} urls; bind each slide "
                         "as its own raw")
    index = src.params.get("index")
    if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
        raise ValueError(f"params.index must be an integer, got {index!r}")
    files = io.expand(src.urls[0])
    if index is None:
        if len(files) != 1:
            raise ValueError(f"{src.urls[0]} stands for {len(files)} files and each slide is its own array: "
                             "set params.index (0-based, in name order), one raw per slide")
        return files[0]
    if not 0 <= index < len(files):
        raise ValueError(f"params.index {index} out of range for the {len(files)} file(s) of {src.urls[0]}")
    return files[index]


def open_array(src: Source, io: IO) -> Array:
    url = _slide_url(src, io)
    try:
        return _Slide(tifffile.TiffFile(io.open(url)), url, "chunk")
    except NeedsFetch:      # a server without Range: stage the whole file (the job's byte cap decides)
        return _Slide(tifffile.TiffFile(io.fetch(url)), url, "file")
