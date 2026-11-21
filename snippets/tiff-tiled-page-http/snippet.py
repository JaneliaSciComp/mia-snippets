"""Page 0 of one tiled TIFF (a whole-slide pyramid), from a plain URL or a zip member, read tile by tile.

A whole-slide image is one tiled, JPEG-compressed page of 10^9..10^10 pixels followed by reduced-resolution
copies of it. `open_array` reads the TIFF header and the first IFD (with its tile offset and byte count tables)
and nothing else; `read` fetches by range and decodes only the level-0 tiles the region touches, so a 256x256
patch of a 26 GB slide costs one to four tiles. tifffile decodes each tile (`TiffPage.decode`), so JPEGTables,
YCbCr -> RGB and every other compression tifffile knows are handled as `tifffile.imread` would handle them.
"""
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

UNIT_NM = {2: 25.4e6, 3: 1e7}      # TIFF ResolutionUnit: inch, centimetre


class _TiledPage:
    def __init__(self, tif: tifffile.TiffFile, url: str, access: str, pixel_size_from_resolution: bool):
        self._tif, self.access = tif, access
        page = tif.pages.first                      # the first IFD only; the other pages are never visited
        if not page.is_tiled:
            raise ValueError(f"{url}: page 0 is stored in strips, not tiles; this snippet reads tiled pages "
                             "only, see tiff-zip-member-2 for other TIFFs")
        if page.is_reduced:
            raise ValueError(f"{url}: page 0 is marked as a reduced-resolution image, not the full resolution")
        if page.imagedepth != 1 or (page.samplesperpixel > 1 and page.planarconfig != 1):
            raise ValueError(f"{url}: page 0 has depth {page.imagedepth} and planar configuration "
                             f"{int(page.planarconfig)}; this snippet reads 2D pages with interleaved samples")
        self._page = page
        self._ny, self._nx, self._ns = page.imagelength, page.imagewidth, page.samplesperpixel
        self._th, self._tw = page.tilelength, page.tilewidth
        self._across = -(-self._nx // self._tw)
        tiles = -(-self._ny // self._th) * self._across
        if len(page.dataoffsets) != tiles or len(page.databytecounts) != tiles:
            raise ValueError(f"{url}: page 0 lists {len(page.dataoffsets)} tile offsets and "
                             f"{len(page.databytecounts)} byte counts for {tiles} tiles")
        self.dtype = np.dtype(page.dtype)
        self.axes = "yxc" if self._ns > 1 else "yx"
        self.shape = (self._ny, self._nx, self._ns) if self._ns > 1 else (self._ny, self._nx)
        self.voxel_size_nm = _pixel_size(page, url) if pixel_size_from_resolution else None

    def __del__(self) -> None:      # the harness never closes arrays; a staged file's handle goes with the array
        self._tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ysel, xsel = region.get("y", slice(None)), region.get("x", slice(None))
        yi, xi = _indices(ysel, self._ny, "y"), _indices(xsel, self._nx, "x")
        if len(yi) and len(xi):
            y0, x0 = int(yi.min()), int(xi.min())
            box = self._box(y0, int(yi.max()) + 1, x0, int(xi.max()) + 1)
            out = box[np.ix_(yi - y0, xi - x0)]                         # (len(yi), len(xi), samples)
        else:
            out = np.empty((len(yi), len(xi), self._ns), self.dtype)
        out = out[:, :, region.get("c", slice(None))] if "c" in self.axes else out[:, :, 0]
        return out[slice(None) if isinstance(ysel, slice) else 0, slice(None) if isinstance(xsel, slice) else 0]

    def _box(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        """Pixels [y0:y1, x0:x1] of the page as (y, x, samples), from the tiles that hold them."""
        page, th, tw = self._page, self._th, self._tw
        out = np.full((y1 - y0, x1 - x0, self._ns), page.nodata, self.dtype)   # a tile the file leaves out
        fh: BinaryIO = self._tif.filehandle
        tiles = [(ty, tx) for ty in range(y0 // th, (y1 - 1) // th + 1) for tx in range(x0 // tw, (x1 - 1) // tw + 1)]
        for ty, tx in sorted(tiles, key=lambda t: page.dataoffsets[t[0] * self._across + t[1]]):   # in file order
            index = ty * self._across + tx
            offset, count = page.dataoffsets[index], page.databytecounts[index]
            if not offset or not count:
                continue
            fh.seek(offset)
            data = fh.read(count)
            if len(data) != count:
                raise ValueError(f"tile {index} is cut short: {len(data)} of {count} bytes at offset {offset}")
            tile = page.decode(data, index, jpegtables=page.jpegtables)[0]
            if tile is None:
                continue
            tile = np.asarray(tile).reshape(tile.shape[-3:])    # (1, h, w, samples), cut at the image's edges
            ty0, tx0 = ty * th, tx * tw
            ya, yb = max(y0, ty0), min(y1, ty0 + tile.shape[0])
            xa, xb = max(x0, tx0), min(x1, tx0 + tile.shape[1])
            out[ya - y0:yb - y0, xa - x0:xb - x0] = tile[ya - ty0:yb - ty0, xa - tx0:xb - tx0]
        return out


def _indices(sel: int | slice, n: int, axis: str) -> np.ndarray:
    """The pixel indices a selection names on an axis of n pixels, as numpy would resolve it."""
    if isinstance(sel, slice):
        return np.arange(n)[sel]
    k = int(sel) + n if int(sel) < 0 else int(sel)
    if not 0 <= k < n:
        raise IndexError(f"{axis} {sel} is out of range for {n} pixels")
    return np.array([k])


def _pixel_size(page: tifffile.TiffPage, url: str) -> dict[str, float]:
    """nm per pixel from XResolution, YResolution and ResolutionUnit (inch or centimetre)."""
    unit = int(page.tags.valueof(296, 2))           # ResolutionUnit; the TIFF default is inch
    out: dict[str, float] = {}
    for axis, code in (("y", 283), ("x", 282)):     # YResolution, XResolution: pixels per unit, as a rational
        num, den = page.tags.valueof(code, (0, 0))
        if unit not in UNIT_NM or not num or not den:
            raise ValueError(f"{url}: params.pixel_size_from_resolution is set, but page 0 has no usable "
                             f"resolution (unit {unit}, {axis} resolution {num}/{den})")
        out[axis] = UNIT_NM[unit] * den / num
    return out


def open_array(src: Source, io: IO) -> Array:
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(urls) != 1:
        raise ValueError(f"tiff-tiled-page-http reads one file per array, got {len(urls)} (after glob expansion); "
                         "bind each file as its own array")
    from_resolution = src.params.get("pixel_size_from_resolution", False)
    if not isinstance(from_resolution, bool):
        raise ValueError(f"params.pixel_size_from_resolution must be true or false, got {from_resolution!r}")
    try:
        tif, access = tifffile.TiffFile(io.open(urls[0])), "chunk"
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        tif, access = tifffile.TiffFile(io.fetch(urls[0])), "file"
    return _TiledPage(tif, urls[0], access, from_resolution)
