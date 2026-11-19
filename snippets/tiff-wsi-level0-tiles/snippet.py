"""The full-resolution level of one pyramidal whole-slide TIFF (VENTANA DP 200 style: pages `Label_Image`,
`Probability_Image`, then `level=0 ...` to `level=N ...`), as `cyx`, read tile by tile by byte range.

`open_array` walks the page list up to the page whose ImageDescription starts with `level=0` and reads that
page's tags only; `read` fetches and decodes just the tiles the region touches. Tiles are placed on the TIFF
tile grid, which is where these slides put them: the scanner stitched them (no `TileJointInfo` overlaps). A
slide that does carry tile-joint metadata (an older BIF with overlapping, serpentine tiles) is refused.
"""
import re
from collections import OrderedDict
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

LEVEL0 = re.compile(r"level=0(\D|$)")
MAX_PAGES = 64                  # pages looked at before giving up on finding level 0
CACHE_TILES = 32                # decoded tiles kept (32 x 1024 x 1024 x 3 bytes = 96 MiB)
NM_PER_UNIT = {2: 25_400_000.0, 3: 10_000_000.0}    # TIFF ResolutionUnit: inch, centimetre


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _span(sel: int | slice, n: int) -> tuple[int, int, int | slice]:
    """The run [start, stop) of an axis of length n that `sel` touches, and `sel` rewritten relative to start."""
    if isinstance(sel, slice):
        r = range(n)[sel]
        if len(r) == 0:
            return 0, 0, slice(0, 0)
        lo, hi = min(r[0], r[-1]), max(r[0], r[-1]) + 1
        return lo, hi, slice(r.start - lo, r.stop - lo if r.stop >= lo else None, r.step)
    i = range(n)[sel]           # IndexError when out of range, like numpy
    return i, i + 1, 0


class _Slide:
    def __init__(self, io: IO, url: str):
        self._url = url
        self._fh: BinaryIO = io.open(url)       # NeedsFetch is not caught: staging a multi-GB slide is no reader
        self._tif = tifffile.TiffFile(self._fh)
        self._page = self._level0()
        p = self._page
        if not p.is_tiled or p.axes != "YXS" or p.tiledepth != 1:
            raise ValueError(f"{url}: the level-0 page is {p.axes} {tuple(p.shape)}, tiled={p.is_tiled}; this "
                             "snippet reads a tiled colour (YXS) page only")
        xmp = p.tags.get("XMP")
        if xmp is not None and "TileJointInfo" in _text(xmp.value):
            raise ValueError(f"{url}: the level-0 page carries TileJointInfo (overlapping BIF tiles in scan "
                             "order); this snippet places tiles on the TIFF grid and does not read such a slide")
        ny, nx, ns = p.shape
        self.axes = "cyx"                       # c in the file's sample order (R, G, B)
        self.shape = (ns, ny, nx)
        self.dtype = np.dtype(p.dtype)
        self.access = "chunk"                   # a region costs the tiles it touches
        self.voxel_size_nm = self._voxel_size()
        self._across = -(-nx // p.tilewidth)
        # tifffile loads tag values on first use, moving the file position: take the tile table and build the
        # page's decoder now, at open, so `read` only seeks to tiles
        self._offsets, self._counts = tuple(p.dataoffsets), tuple(p.databytecounts)
        self._decode = p.decode
        self._tiles: OrderedDict[int, np.ndarray] = OrderedDict()

    def _level0(self) -> tifffile.TiffPage:
        seen = []
        for i in range(MAX_PAGES):
            try:
                page = self._tif.pages[i]
            except IndexError:
                break
            if not isinstance(page, tifffile.TiffPage):
                continue
            if LEVEL0.match(page.description):
                return page
            seen.append(page.description[:40])
        raise ValueError(f"{self._url}: no page whose ImageDescription starts with 'level=0' (found {seen}); "
                         "this snippet reads pyramidal slides that label their levels that way")

    def _voxel_size(self) -> dict[str, float] | None:
        """From XResolution/YResolution (pixels per inch or centimetre) of the level-0 page, else None."""
        tags = self._page.tags
        unit = tags.get("ResolutionUnit")
        per = NM_PER_UNIT.get(int(unit.value)) if unit is not None else None
        out = {}
        for axis, name in (("y", "YResolution"), ("x", "XResolution")):
            tag = tags.get(name)
            if per is None or tag is None:
                return None
            num, den = tag.value
            if num <= 0 or den <= 0:
                return None
            out[axis] = per * den / num
        return out

    def _tile(self, index: int) -> np.ndarray:
        """Decoded tile `index` (row-major on the TIFF tile grid) as (rows, cols, S), full size also at the
        image edge (`read` takes the part inside). A tile the file does not store (byte count 0) is zeros, as
        tifffile fills it."""
        if index in self._tiles:
            self._tiles.move_to_end(index)
            return self._tiles[index]
        offset, count = self._offsets[index], self._counts[index]
        data = None
        if count:
            self._fh.seek(offset)
            data = self._fh.read(count)
            if len(data) != count:
                raise ValueError(f"{self._url}: tile {index} is cut short ({len(data)} of {count} bytes)")
        segment, _, shape = self._decode(data, index)
        tile = np.zeros(shape[1:], self.dtype) if segment is None else np.asarray(segment)[0]
        self._tiles[index] = tile
        if len(self._tiles) > CACHE_TILES:
            self._tiles.popitem(last=False)
        return tile

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ns, ny, nx = self.shape
        y0, y1, ysel = _span(region.get("y", slice(None)), ny)
        x0, x1, xsel = _span(region.get("x", slice(None)), nx)
        c = region.get("c", slice(None))
        th, tw = self._page.tilelength, self._page.tilewidth
        box = np.empty((y1 - y0, x1 - x0, ns), self.dtype)
        if box.size:
            for ty in range(y0 // th, (y1 - 1) // th + 1):
                for tx in range(x0 // tw, (x1 - 1) // tw + 1):
                    tile = self._tile(ty * self._across + tx)
                    ya, yb = max(y0, ty * th), min(y1, ty * th + tile.shape[0])
                    xa, xb = max(x0, tx * tw), min(x1, tx * tw + tile.shape[1])
                    box[ya - y0:yb - y0, xa - x0:xb - x0] = tile[ya - ty * th:yb - ty * th, xa - tx * tw:xb - tx * tw]
        return np.ascontiguousarray(np.moveaxis(box, 2, 0)[c][..., ysel, xsel])


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1 or "::" in src.urls[0]:
        raise ValueError("this snippet reads one slide per array: exactly one plain URL, no zip member and no "
                         f"glob (slides differ in shape, so each is its own raw entry); got {list(src.urls)}")
    return _Slide(io, src.urls[0])
