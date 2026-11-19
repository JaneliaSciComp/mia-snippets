"""Page 0 of one tiled (pyramidal) TIFF or BigTIFF at a plain URL, read tile by tile by byte range, as cyx (colour)
or yx (grey); or, with a GeoJSON url beside the TIFF, the file's Polygon features filled onto that page's pixel
grid as yx uint16 semantic ids.

Raw form, `urls: [<one .tif>]`: only the tiles a region touches are fetched (one byte range each) and decoded
with the page's own codec (JPEG tables included); the other pages, the reduced-resolution levels of a pyramid,
are never read.

Label form, `urls: [<one .json or .geojson>, <its .tif>]`: the TIFF's header gives the grid; a pixel holds the id
(`properties.data.id`, an ontology id) of the polygon that encloses its centre, 0 where none does. GeoJSON
coordinates are (x, -y) of page-0 pixels: x to the right, y negated (GeoJSON's y points up, the image's down).
Where polygons overlap, the one with the smaller area is kept, so a structure drawn inside another shows.
Point and line features enclose no pixels: they are an error unless `params.skip_lines` is true, which leaves
them out.
"""
import json
from collections import OrderedDict
from typing import Any, BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

TILE_CACHE_BYTES = 256 << 20        # decoded tiles kept between reads
JSON_ENDINGS = (".json", ".geojson")
ID_MAX = 65535                      # uint16
LINES = ("Point", "MultiPoint", "LineString", "MultiLineString")    # geometries that enclose no pixels


def _span(sel: int | slice, n: int) -> tuple[range, bool]:
    """The indices `sel` picks on an axis of length n, and whether it is an int (the axis is dropped)."""
    if isinstance(sel, slice):
        return range(n)[sel], False
    k = range(n)[sel]               # IndexError when out of range, like numpy
    return range(k, k + 1), True


def _pick(block: np.ndarray, lo: int, picked: range, drop: bool, axis: int) -> np.ndarray:
    """`block` covers indices lo.. of `axis` contiguously; return the `picked` ones, dropping the axis for an int."""
    if drop:
        return np.take(block, picked[0] - lo, axis=axis)
    if len(picked) == block.shape[axis] and picked.step == 1:
        return block
    return np.take(block, np.arange(picked.start, picked.stop, picked.step) - lo, axis=axis)


def _open_tiff(io: IO, url: str) -> tuple[tifffile.TiffFile, bool]:
    """(the TIFF, whether it is read by range). A server without Range support has the file staged whole."""
    try:
        fh: BinaryIO = io.open(url)
        return tifffile.TiffFile(fh), True
    except NeedsFetch:
        return tifffile.TiffFile(io.fetch(url)), False


class _Tiled:
    """Page 0 of a tiled TIFF. `plane` is (y, x) and `samples` the number of colour samples per pixel."""

    def __init__(self, io: IO, url: str):
        self._url = url
        self._tif, ranged = _open_tiff(io, url)
        page = self._tif.pages[0]
        if not isinstance(page, tifffile.TiffPage) or not page.is_tiled:
            raise ValueError(f"{url}: page 0 is not tiled; this snippet reads tiled TIFFs tile by tile")
        if page.imagedepth != 1 or page.tiledepth != 1:
            raise ValueError(f"{url}: page 0 has depth {page.imagedepth} (tile depth {page.tiledepth}); only 2D pages")
        if page.planarconfig != 1 and page.samplesperpixel != 1:
            raise ValueError(f"{url}: page 0 stores its {page.samplesperpixel} samples in separate planes; only "
                             "contiguous (interleaved) samples are read")
        self._page = page
        self.plane: tuple[int, int] = (int(page.imagelength), int(page.imagewidth))
        self._tile: tuple[int, int] = (int(page.tilelength), int(page.tilewidth))
        self.samples = int(page.samplesperpixel)
        self.dtype = np.dtype(page.dtype)
        self._across = -(-self.plane[1] // self._tile[1])
        down = -(-self.plane[0] // self._tile[0])
        if len(page.dataoffsets) != self._across * down or len(page.databytecounts) != self._across * down:
            raise ValueError(f"{url}: page 0 lists {len(page.dataoffsets)} tiles, its {self.plane} pixels in "
                             f"{self._tile} tiles need {self._across * down}")
        self.access = "chunk" if ranged else "file"
        tile_bytes = self._tile[0] * self._tile[1] * self.samples * self.dtype.itemsize
        self._keep = max(4, TILE_CACHE_BYTES // tile_bytes)
        self._tiles: OrderedDict[int, np.ndarray] = OrderedDict()

    def tile(self, index: int) -> np.ndarray:
        """Tile `index` (row-major) as (tile y, tile x, samples), cut to the image at its right and bottom edges."""
        if index in self._tiles:
            self._tiles.move_to_end(index)
            return self._tiles[index]
        page, (th, tw) = self._page, self._tile
        ty, tx = divmod(index, self._across)
        h, w = min(th, self.plane[0] - ty * th), min(tw, self.plane[1] - tx * tw)
        offset, count = int(page.dataoffsets[index]), int(page.databytecounts[index])
        if count == 0:              # a tile the writer left out: TIFF readers fill it with zeros
            out = np.zeros((h, w, self.samples), self.dtype)
        else:
            fh = self._tif.filehandle
            fh.seek(offset)
            data = fh.read(count)
            if len(data) != count:
                raise ValueError(f"{self._url}: file ends inside tile {index} of page 0")
            decoded = page.decode(data, index, jpegtables=page.jpegtables)[0]
            if decoded is None:
                raise ValueError(f"{self._url}: tile {index} of page 0 did not decode")
            out = np.asarray(decoded).reshape(decoded.shape[-3], decoded.shape[-2], self.samples)[:h, :w]
            if out.shape != (h, w, self.samples):
                raise ValueError(f"{self._url}: tile {index} of page 0 decoded to {out.shape}, not {(h, w, self.samples)}")
            out = out.astype(self.dtype, copy=False)
        self._tiles[index] = out
        while len(self._tiles) > self._keep:
            self._tiles.popitem(last=False)
        return out

    def block(self, ys: range, xs: range) -> np.ndarray:
        """Pixels [min(ys), max(ys)] x [min(xs), max(xs)] as (y, x, samples), from the tiles they touch."""
        y0, y1, x0, x1 = min(ys), max(ys) + 1, min(xs), max(xs) + 1
        th, tw = self._tile
        out = np.empty((y1 - y0, x1 - x0, self.samples), self.dtype)
        for ty in range(y0 // th, (y1 - 1) // th + 1):
            for tx in range(x0 // tw, (x1 - 1) // tw + 1):
                t = self.tile(ty * self._across + tx)
                a0, a1 = max(y0, ty * th), min(y1, ty * th + t.shape[0])
                b0, b1 = max(x0, tx * tw), min(x1, tx * tw + t.shape[1])
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = t[a0 - ty * th:a1 - ty * th, b0 - tx * tw:b1 - tx * tw]
        return out


class _Raw:
    voxel_size_nm = None            # plain TIFF resolution tags describe print size, not pixels

    def __init__(self, io: IO, url: str):
        self._t = _Tiled(io, url)
        ny, nx = self._t.plane
        colour = self._t.samples > 1
        self.axes = "cyx" if colour else "yx"
        self.shape: tuple[int, ...] = (self._t.samples, ny, nx) if colour else (ny, nx)
        self.dtype = self._t.dtype
        self.access = self._t.access

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self._t.plane
        ys, ydrop = _span(region.get("y", slice(None)), ny)
        xs, xdrop = _span(region.get("x", slice(None)), nx)
        cs, cdrop = _span(region.get("c", slice(None)), self._t.samples)
        if "c" not in self.axes:
            cdrop = True
        if not len(ys) or not len(xs) or not len(cs):
            shape = [n for n, drop in ((len(cs), cdrop), (len(ys), ydrop), (len(xs), xdrop)) if not drop]
            return np.empty(shape, self.dtype)
        block = self._t.block(ys, xs)                       # (y, x, samples)
        block = _pick(block, min(xs), xs, xdrop, 1)
        block = _pick(block, min(ys), ys, ydrop, 0)
        block = _pick(block, 0, cs, cdrop, block.ndim - 1)
        if not cdrop:
            block = np.moveaxis(block, -1, 0)
        return np.ascontiguousarray(block)


def _rings(geometry: Any, where: str) -> list[list[np.ndarray]]:
    """The polygons of a GeoJSON geometry, each a list of rings ((n, 2) float arrays of x and stored y)."""
    kind = geometry.get("type") if isinstance(geometry, dict) else None
    if kind == "Polygon":
        polygons = [geometry["coordinates"]]
    elif kind == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        raise ValueError(f"{where}: geometry {kind!r}; only Polygon and MultiPolygon features are filled")
    out = []
    for polygon in polygons:
        rings = [np.asarray(ring, float)[:, :2] for ring in polygon if len(ring) >= 3]
        if rings:
            out.append(rings)
    return out


def _area(rings: list[np.ndarray]) -> float:
    """Enclosed area: the outer ring's less its holes' (shoelace)."""
    areas = [abs(float(np.sum(r[:, 0] * np.roll(r[:, 1], -1) - np.roll(r[:, 0], -1) * r[:, 1]))) / 2 for r in rings]
    return areas[0] - sum(areas[1:])


def _fill(rings: list[np.ndarray], y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """Even-odd mask of the pixels of rows y0..y1-1 and columns x0..x1-1 whose centres (x + 0.5, y + 0.5) the
    rings enclose. Ring vertices are image coordinates (x right, y down), an integer being a pixel corner."""
    rows = np.arange(y0, y1) + 0.5
    flips = np.zeros((y1 - y0, x1 - x0 + 1), np.uint8)      # parity changes when walking right along a row
    for ring in rings:
        ax, ay = ring[:, 0], ring[:, 1]
        bx, by = np.roll(ax, -1), np.roll(ay, -1)           # the ring closes itself, repeated end point or not
        r, e = np.nonzero((ay[None, :] <= rows[:, None]) != (by[None, :] <= rows[:, None]))
        cross = ax[e] + (rows[r] - ay[e]) * (bx[e] - ax[e]) / (by[e] - ay[e])
        cols = np.clip(np.ceil(cross - 0.5), x0, x1).astype(np.int64) - x0      # first column right of the edge
        np.add.at(flips, (r, cols), 1)
    return (np.cumsum(flips[:, :-1], axis=1, dtype=np.uint8) & 1).astype(bool)


class _Labels:
    dtype = np.dtype(np.uint16)
    axes = "yx"
    access = "chunk"

    def __init__(self, io: IO, json_url: str, tiff_url: str, skip_lines: bool):
        self._io, self._url, self._skip_lines = io, json_url, skip_lines
        t = _Tiled(io, tiff_url)                            # header only: the grid the polygons are filled onto
        self.shape: tuple[int, ...] = t.plane
        self.voxel_size_nm = None
        self._shapes: list[tuple[int, list[np.ndarray], tuple[float, float, float, float]]] | None = None

    def _load(self) -> list[tuple[int, list[np.ndarray], tuple[float, float, float, float]]]:
        """(id, rings in image coordinates, (y min, y max, x min, x max)) per polygon, largest area first."""
        try:
            with self._io.open(self._url) as fh:
                text = fh.read()
        except NeedsFetch:
            text = self._io.fetch(self._url).read_bytes()
        doc = json.loads(text)
        if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
            raise ValueError(f"{self._url}: not a GeoJSON FeatureCollection")
        shapes = []
        for k, feature in enumerate(doc.get("features") or []):
            where = f"{self._url}: feature {k}"
            kind = (feature.get("geometry") or {}).get("type")
            if kind in LINES:
                if self._skip_lines:
                    continue
                raise ValueError(f"{where}: a {kind} encloses no pixels; set params.skip_lines to leave such "
                                 "features out of the label")
            data = (feature.get("properties") or {}).get("data") or {}
            ident = data.get("id")
            if isinstance(ident, bool) or not isinstance(ident, int) or not 0 < ident <= ID_MAX:
                raise ValueError(f"{where}: properties.data.id is {ident!r}; a structure id from 1 to {ID_MAX} is needed")
            for rings in _rings(feature.get("geometry"), where):
                rings = [np.column_stack([r[:, 0], -r[:, 1]]) for r in rings]      # stored y is minus the image's
                pts = np.concatenate(rings)
                box = (float(pts[:, 1].min()), float(pts[:, 1].max()), float(pts[:, 0].min()), float(pts[:, 0].max()))
                shapes.append((_area(rings), ident, rings, box))
        shapes.sort(key=lambda s: -s[0])                    # stable: equal areas keep file order
        return [(ident, rings, box) for _, ident, rings, box in shapes]

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self.shape
        ys, ydrop = _span(region.get("y", slice(None)), ny)
        xs, xdrop = _span(region.get("x", slice(None)), nx)
        if not len(ys) or not len(xs):
            return np.empty([n for n, drop in ((len(ys), ydrop), (len(xs), xdrop)) if not drop], self.dtype)
        if self._shapes is None:
            self._shapes = self._load()
        y0, y1, x0, x1 = min(ys), max(ys) + 1, min(xs), max(xs) + 1
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for ident, rings, (ymin, ymax, xmin, xmax) in self._shapes:      # smaller polygons are drawn later, on top
            a0, a1 = max(y0, int(np.floor(ymin))), min(y1, int(np.ceil(ymax)) + 1)
            b0, b1 = max(x0, int(np.floor(xmin))), min(x1, int(np.ceil(xmax)) + 1)
            if a0 >= a1 or b0 >= b1:
                continue
            out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][_fill(rings, a0, a1, b0, b1)] = ident
        out = _pick(out, x0, xs, xdrop, 1)
        out = _pick(out, y0, ys, ydrop, 0)
        return np.ascontiguousarray(out)


def open_array(src: Source, io: IO) -> Array:
    urls = [u for url in src.urls for u in io.expand(url)]
    skip_lines = src.params.get("skip_lines", False)
    if not isinstance(skip_lines, bool):
        raise ValueError(f"params.skip_lines must be true or false, not {skip_lines!r}")
    vectors = [u for u in urls if u.split("#")[0].split("?")[0].lower().endswith(JSON_ENDINGS)]
    tiffs = [u for u in urls if u not in vectors]
    if len(tiffs) != 1 or len(vectors) > 1:
        raise ValueError(f"expected one TIFF url (raw), or one GeoJSON url and its TIFF (label); got "
                         f"{len(tiffs)} TIFF and {len(vectors)} GeoJSON urls")
    if vectors:
        return _Labels(io, vectors[0], tiffs[0], skip_lines)
    return _Raw(io, tiffs[0])
