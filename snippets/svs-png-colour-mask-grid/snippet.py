"""One Aperio SVS whole-slide image at a plain URL, the first (full-resolution) page of the tiled RGB TIFF read
tile by tile by byte range as cyx uint8; or, with `colours` and `label_factor`, the slide's downsampled
colour-coded annotation PNG (8-bit RGB, one colour per class) as yx uint8 class ids on the slide's own grid: each
mask pixel is repeated label_factor x label_factor and never interpolated.

Such masks are often made by shrinking a full-resolution painting with interpolation, so the borders between
regions hold mixtures of the neighbouring colours. A pixel whose colour is in the table has that colour's id; a
pixel of any other colour takes the id of the nearest colour to its own among the colours of the pixels next to
it and the background colour, working inward from the exactly coloured pixels. A mixture is therefore read as one
of the classes that meet at it (or as background) and never as a third class whose colour happens to lie between
them. See README.md."""
import re
import struct
from typing import BinaryIO

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
PNG_HEADER = 33                 # the signature (8 bytes) and the IHDR chunk (length 4, type 4, data 13, CRC 4)
PNG_RGB = 2                     # IHDR colour type of a truecolour PNG without alpha
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
MAX_COLOURS = 256               # ids are uint8
KEY = 2 * MAX_COLOURS           # a candidate is compared as distance * KEY + id (the background: + MAX_COLOURS)
UNLISTED = 0xFFFF               # the lookup table's value for a colour params.colours does not list
MIXED_MAX = 0.5                 # a mask with a larger share of unlisted colours is not read
SHOWN = 8                       # colours named in an error
NEIGHBOURS = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
TILE_CACHE = 512                # decoded tiles kept (512 x 256 x 256 x 3 uint8 = 96 MiB)
TILE_CACHE_BYTES = 128 << 20    # and never more than this, whatever the tile size
MPP = re.compile(r"\|\s*MPP\s*=\s*([0-9.eE+-]+)")      # Aperio ImageDescription: "...|AppMag = 40|MPP = 0.2458|..."


def _span(key: int | slice, n: int) -> tuple[int, int, int | np.ndarray]:
    """What `key` asks of an axis of length n: (lo, hi, index into [lo, hi)); an int index drops the axis."""
    if isinstance(key, slice):
        idx = np.arange(n)[key]
        if idx.size == 0:
            return 0, 0, idx
        lo = int(idx.min())
        return lo, int(idx.max()) + 1, idx - lo
    k = int(key) + (n if key < 0 else 0)
    if not 0 <= k < n:
        raise IndexError(f"index {key} is outside an axis of length {n}")
    return k, k + 1, 0


class _Slide:
    """The first page of a TIFF, which must be tiled 8-bit RGB (or YCbCr JPEG) with interleaved samples, as cyx
    uint8 with c = red, green, blue."""

    axes = "cyx"
    dtype = np.dtype("uint8")

    def __init__(self, fh: BinaryIO, ranged: bool, url: str):
        self._fh, self._cache = fh, {}
        self.access = "chunk" if ranged else "file"     # file: the server gave no ranges, so it was staged whole
        self._tif = tifffile.TiffFile(fh)
        page = self._tif.pages[0]           # only this IFD is parsed; the pyramid, label and macro pages are not
        if not isinstance(page, tifffile.TiffPage):
            raise ValueError(f"{url}: the first IFD is not an image page")
        if not page.is_tiled or page.samplesperpixel != 3 or page.planarconfig != 1 or page.dtype != self.dtype \
                or page.imagedepth != 1 or page.tiledepth != 1 or page.photometric.name not in ("RGB", "YCBCR"):
            raise ValueError(f"{url}: the first page is {page.shape} {page.dtype}, photometric "
                             f"{page.photometric.name}, planar configuration {int(page.planarconfig)}, "
                             f"{'tiled' if page.is_tiled else 'in strips'}; only a tiled 8-bit RGB page with "
                             "interleaved samples is read")
        self._page = page
        self._th, self._tw = page.tilelength, page.tilewidth
        self._across = -(-page.imagewidth // self._tw)
        down = -(-page.imagelength // self._th)
        if len(page.dataoffsets) != self._across * down or len(page.databytecounts) != self._across * down:
            raise ValueError(f"{url}: the first page lists {len(page.dataoffsets)} tiles, not "
                             f"{down}x{self._across}")
        self._keep = max(1, min(TILE_CACHE, TILE_CACHE_BYTES // (self._th * self._tw * 3)))
        self.shape = (3, page.imagelength, page.imagewidth)
        mpp = MPP.search(page.description or "")
        nm = float(mpp.group(1)) * 1000 if mpp else 0.0
        self.voxel_size_nm = {"y": nm, "x": nm} if nm > 0 else None

    def __del__(self) -> None:      # the harness never closes arrays; the handle goes with the array
        self._fh.close()

    def _decode(self, key: int) -> np.ndarray:
        """Tile `key` (row-major on the tile grid) as (rows, columns, 3) uint8, cut at the image border."""
        offset, size = self._page.dataoffsets[key], self._page.databytecounts[key]
        ty, tx = divmod(key, self._across)
        h = min(self._th, self.shape[1] - ty * self._th)
        w = min(self._tw, self.shape[2] - tx * self._tw)
        if size == 0:                       # a tile the file does not store
            return np.zeros((h, w, 3), self.dtype)
        self._fh.seek(offset)
        data = self._fh.read(size)
        if len(data) != size:
            raise ValueError(f"tile {key} at byte {offset} read back short ({len(data)} of {size})")
        tile = self._page.decode(data, key, jpegtables=self._page.jpegtables)[0]
        if tile is None or tile.ndim != 4 or tile.shape[0] != 1 or tile.shape[1] < h or tile.shape[2] < w \
                or tile.shape[3] != 3 or tile.dtype != self.dtype:
            raise ValueError(f"tile {key} decodes to {None if tile is None else (tile.shape, tile.dtype)}, "
                             f"not at least {h}x{w} RGB uint8 pixels")
        return tile[0, :h, :w]

    def _tile(self, key: int) -> np.ndarray:
        if key in self._cache:
            self._cache[key] = self._cache.pop(key)         # most recently used last
            return self._cache[key]
        tile = self._decode(key)
        self._cache[key] = tile
        while len(self._cache) > self._keep:
            self._cache.pop(next(iter(self._cache)))
        return tile

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nc, ny, nx = self.shape
        c0, c1, ci = _span(region.get("c", slice(None)), nc)
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0, c1 - c0), self.dtype)
        if out.size:
            for r in range(y0 // self._th, -(-y1 // self._th)):
                for c in range(x0 // self._tw, -(-x1 // self._tw)):
                    tile, ty, tx = self._tile(r * self._across + c), r * self._th, c * self._tw
                    a0, a1 = max(y0, ty), min(y1, ty + tile.shape[0])
                    b0, b1 = max(x0, tx), min(x1, tx + tile.shape[1])
                    out[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = tile[a0 - ty:a1 - ty, b0 - tx:b1 - tx, c0:c1]
        return np.array(np.moveaxis(out, 2, 0)[ci][..., yi, :][..., xi])


def _colours(colours: object) -> list[list[int]]:
    """params.colours checked: 1 to 256 different [r, g, b] colours, in id order."""
    if not isinstance(colours, list) or not 1 <= len(colours) <= MAX_COLOURS:
        raise ValueError(f"params.colours must be a list of 1 to {MAX_COLOURS} [r, g, b] colours, id 0 first, "
                         f"got {colours!r}")
    for k, c in enumerate(colours):
        if not (isinstance(c, list) and len(c) == 3 and all(type(v) is int and 0 <= v <= 255 for v in c)):
            raise ValueError(f"params.colours[{k}] must be [r, g, b] with integers 0..255, got {c!r}")
    if len({tuple(c) for c in colours}) != len(colours):
        twice = sorted({tuple(c) for c in colours if colours.count(c) > 1})
        raise ValueError("params.colours lists a colour twice: " + ", ".join(str(list(c)) for c in twice))
    return colours


def _rgb(key: int) -> str:
    return f"[{key >> 16}, {key >> 8 & 255}, {key & 255}]"


def _mask_shape(head: bytes, url: str) -> tuple[int, int]:
    """(height, width) from the first PNG_HEADER bytes of an 8-bit RGB PNG; any other file raises."""
    if head[:8] != PNG or len(head) < PNG_HEADER or head[8:16] != b"\x00\x00\x00\rIHDR":
        raise ValueError(f"{url}: not a PNG that starts with a 13-byte IHDR chunk: header={head[:16]!r}")
    width, height, depth, colour = struct.unpack(">IIBB", head[16:26])
    if colour != PNG_RGB or depth != 8:
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; only an 8-bit RGB mask (colour "
                         "type 2) is read")
    return height, width


def _class_ids(a: np.ndarray, colours: list[list[int]], url: str) -> np.ndarray:
    """(Y, X, 3) uint8 colours as (Y, X) uint8 ids. A listed colour is its position in `colours`. The other
    pixels are settled in rounds: in each round every unsettled pixel that touches (8 neighbours) pixels settled
    before the round takes the id of the nearest colour to its own (squared distance over r, g, b) among those
    neighbours' colours and the background colours[0]; on a tie the lower neighbour id, the background last."""
    packed = a[:, :, 0].astype(np.uint32) << 16
    packed |= a[:, :, 1].astype(np.uint32) << 8
    packed |= a[:, :, 2]
    table = np.full(1 << 24, UNLISTED, np.uint16)       # 24-bit colour -> id
    table[[c[0] << 16 | c[1] << 8 | c[2] for c in colours]] = np.arange(len(colours), dtype=np.uint16)
    ids = table[packed]
    del table
    mixed = ids == UNLISTED
    count = int(mixed.sum())
    if count == 0:
        return ids.astype(np.uint8)
    if count > MIXED_MAX * ids.size:
        keys, n = np.unique(packed[mixed], return_counts=True)
        most = ", ".join(f"{_rgb(int(keys[i]))} ({int(n[i])} px)" for i in np.argsort(-n)[:SHOWN])
        raise ValueError(f"{url}: {count} of {ids.size} pixels ({keys.size} colours) are not in params.colours, "
                         f"more than {MIXED_MAX:.0%}; the most frequent are {most}. This is not a colour-coded "
                         "mask of these classes")
    # a 3x3 block of one unlisted colour is a painted region, not a border mixture: its colour is missing
    flat = mixed[:-1, :-1] & (packed[:-1, :-1] == packed[1:, :-1]) & (packed[:-1, :-1] == packed[:-1, 1:]) \
        & (packed[:-1, :-1] == packed[1:, 1:])                                  # 2x2 blocks, by top-left pixel
    flat = flat[:-1, :-1] & flat[1:, :-1] & flat[:-1, 1:] & flat[1:, 1:]        # 3x3 blocks
    if flat.any():
        keys, n = np.unique(packed[:-2, :-2][flat], return_counts=True)
        named = ", ".join(f"{_rgb(int(keys[i]))} ({int(n[i])} blocks)" for i in np.argsort(-n)[:SHOWN])
        raise ValueError(f"{url}: the mask holds 3x3 blocks of a colour that params.colours does not list: "
                         f"{named}. That is a class colour missing from the table, not a mixture at a border; "
                         "append it (the ids already given keep their meaning)")
    listed = np.asarray(colours, np.int64)
    grid = np.full((ids.shape[0] + 2, ids.shape[1] + 2), UNLISTED, np.uint16)   # a border of 'unsettled'
    grid[1:-1, 1:-1] = ids
    # a mixture of two colours can be a third listed colour exactly (half magenta on black is purple); such a
    # pixel stands alone, so a listed colour with no neighbour of its own colour is settled like a mixture
    alone = ~mixed
    for dy, dx in NEIGHBOURS:
        alone &= grid[1 + dy:1 + dy + ids.shape[0], 1 + dx:1 + dx + ids.shape[1]] != ids
    grid[1:-1, 1:-1][alone] = UNLISTED
    ys, xs = np.nonzero(mixed | alone)
    own = a[ys, xs].astype(np.int64)                    # the unsettled pixels' colours
    nothing = np.iinfo(np.int64).max
    while ys.size:
        best = np.full(ys.size, nothing, np.int64)      # (distance, id) of the best candidate, as one key
        for dy, dx in NEIGHBOURS:
            near = grid[ys + 1 + dy, xs + 1 + dx]
            ok = near != UNLISTED
            distance = ((own[ok] - listed[near[ok]]) ** 2).sum(axis=1)
            best[ok] = np.minimum(best[ok], distance * KEY + near[ok])
        done = best != nothing
        if not done.any():      # only a mask without two touching pixels of one listed colour gets here
            raise ValueError(f"{url}: {ys.size} pixels of unlisted colours touch no pixel of a listed colour")
        # the background (colours[0]) is a candidate too, wherever it is: a gap between two regions that is
        # thinner than a mask pixel is a dark mixture with no background pixel beside it. It loses a tie
        canvas = ((own[done] - listed[0]) ** 2).sum(axis=1) * KEY + MAX_COLOURS
        grid[ys[done] + 1, xs[done] + 1] = np.minimum(best[done], canvas) % MAX_COLOURS
        ys, xs, own = ys[~done], xs[~done], own[~done]
    return grid[1:-1, 1:-1].astype(np.uint8)


class _MaskOnGrid:
    """A colour-coded PNG mask as class ids on a slide's yx grid: mask pixel (i, j) covers the slide's pixels
    (i*k .. i*k+k-1, j*k .. j*k+k-1)."""

    axes = "yx"
    dtype = np.dtype("uint8")

    def __init__(self, io: IO, url: str, head: bytes, colours: list[list[int]], factor: int, slide: _Slide):
        self._io, self._url, self._colours = io, url, colours
        self._mask_shape = _mask_shape(head, url)
        grid = slide.shape[1:]
        for axis, n, m in zip("yx", grid, self._mask_shape):
            if m not in (n // factor, -(-n // factor)):
                raise ValueError(f"{url}: the mask is {self._mask_shape} (y, x); with label_factor {factor} the "
                                 f"slide's {grid} needs {n // factor} or {-(-n // factor)} on {axis} (the "
                                 "slide's size divided by the factor, rounded down or up)")
        self.shape = grid
        self.voxel_size_nm = slide.voxel_size_nm    # the array is on the slide's grid; a PNG states no size
        self.access = "chunk"                       # a region costs its own pixels; the small mask is decoded once
        # slide index -> mask index; slide rows/columns past k*m (rounded-down mask) take the last mask row/column
        self._index = tuple(np.minimum(np.arange(n) // factor, m - 1) for n, m in zip(grid, self._mask_shape))
        self._ids: np.ndarray | None = None

    def _mask(self) -> np.ndarray:
        if self._ids is None:
            try:
                with self._io.open(self._url) as fh:
                    data = fh.read()
            except NeedsFetch:      # a deflated zip member, or a server without Range: staged whole
                data = self._io.fetch(self._url).read_bytes()
            a = np.asarray(imagecodecs.png_decode(data))
            if a.shape != self._mask_shape + (3,) or a.dtype != self.dtype:
                raise ValueError(f"{self._url}: decoded to {a.dtype} {a.shape}, not uint8 "
                                 f"{self._mask_shape + (3,)} as its header says (a tRNS chunk adds an alpha "
                                 "sample, which this snippet does not read)")
            self._ids = _class_ids(a, self._colours, self._url)
        return self._ids

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        iy, ix = self._index
        return np.array(self._mask()[iy[region.get("y", slice(None))]][..., ix[region.get("x", slice(None))]])


def _open(io: IO, url: str) -> tuple[BinaryIO, bool]:
    """(file, read by range?)"""
    try:
        return io.open(url), True
    except NeedsFetch:          # a deflated zip member, or a server without Range: staged whole
        return open(io.fetch(url), "rb"), False


def open_array(src: Source, io: IO) -> Array:
    colours, factor = src.params.get("colours"), src.params.get("label_factor")
    slides: list[tuple[str, BinaryIO, bool]] = []
    masks: list[tuple[str, bytes]] = []
    for url in src.urls:
        fh, ranged = _open(io, url)
        head = fh.read(PNG_HEADER)
        fh.seek(0)
        if head[:4] in TIFF_MAGIC:
            slides.append((url, fh, ranged))
            continue
        fh.close()
        if head[:8] != PNG:
            raise ValueError(f"{url} starts with {head[:8]!r}: neither a TIFF (SVS) nor a PNG")
        masks.append((url, head))
    if colours is None and factor is None:
        if len(src.urls) != 1 or len(slides) != 1:
            raise ValueError("without params this snippet reads one SVS url, got "
                             f"{len(slides)} TIFF and {len(masks)} PNG urls")
        return _Slide(slides[0][1], slides[0][2], slides[0][0])
    if colours is None or factor is None:
        raise ValueError("the label form needs both params.colours and params.label_factor")
    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 1:
        raise ValueError(f"params.label_factor must be a positive integer, got {factor!r}")
    if len(src.urls) != 2 or len(slides) != 1 or len(masks) != 1:
        raise ValueError("with params.colours and params.label_factor this snippet reads two urls, the mask PNG "
                         f"and the SVS whose grid it is placed on, got {len(slides)} TIFF and {len(masks)} PNG")
    # the slide gives the grid and the voxel size; its handle is closed with it, no slide pixel is read
    return _MaskOnGrid(io, masks[0][0], masks[0][1], _colours(colours), factor, _Slide(*slides[0][1:], slides[0][0]))
