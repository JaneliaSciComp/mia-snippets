"""One field of view stored as a folder of PNG files with a GeoJSON annotation, at plain URLs or as zip members.

Three forms, told apart by the urls and params of the binding entry:

- channels: grey 8/16-bit PNGs, one file per channel, as `cyx` in url order (`yx` for one file);
- sample (`sample: <k>`): one sample plane of one RGB, RGBA or grey+alpha PNG as `yx`, values unchanged;
- polygons (one `.json`/`.geojson` url and one PNG url): the Polygon and MultiPolygon features of a GeoJSON
  FeatureCollection filled onto the PNG's pixel grid as `yx` uint16 instance ids (1-based feature position).

A PNG is one deflate stream, so every file is read and decoded whole (`access: file`). `open_array` reads PNG
headers only (signature + IHDR); the GeoJSON is read and rasterised on the first `read`.
"""
import json
import struct

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SIGNATURE = b"\x89PNG\r\n\x1a\n"
HEADER = 33                 # signature (8) + IHDR chunk: length (4), type (4), 13 bytes of data, CRC (4)
SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}      # IHDR colour type -> samples per pixel (3, palette, is not read)
DTYPES = {8: np.dtype("uint8"), 16: np.dtype("uint16")}     # IHDR bit depth -> dtype, stored values unchanged
GEOJSON = (".json", ".geojson")
MAX_IDS = 65535


class _Png:
    """The channels form (grey files, `sample` None) and the sample form (one colour file, `sample` an index)."""

    def __init__(self, io: IO, urls: list[str], sample: int | None):
        self._io, self._urls, self._sample = io, urls, sample
        self._plane_shape, self.dtype, self._n = _header(_data(io, urls[0], HEADER), urls[0])
        if sample is None and self._n != 1:
            raise ValueError(f"{urls[0]}: {self._n} samples per pixel; the channels form reads grey PNGs (colour "
                             "type 0), one channel per file. Set params.sample to read one sample of this file")
        if sample is not None and self._n == 1:
            raise ValueError(f"{urls[0]}: a grey PNG has no sample axis; drop params.sample")
        if sample is not None and not 0 <= sample < self._n:
            raise ValueError(f"{urls[0]}: params.sample {sample} out of range for {self._n} samples per pixel")
        self.shape: tuple[int, ...] = (len(urls), *self._plane_shape) if len(urls) > 1 else self._plane_shape
        self.axes = "cyx" if len(urls) > 1 else "yx"
        self.voxel_size_nm = None       # pHYs, when present, is a print resolution, not a pixel size
        self.access = "file"            # one deflate stream per file: no rows or tiles to read by range
        self._last: tuple[int, np.ndarray] | None = None    # the file decoded last; patches of one file reuse it

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "c" not in self.axes:
            return self._plane(0)[y, x]
        sel = region.get("c", slice(None))
        if isinstance(sel, slice):
            picked = [self._plane(k)[y, x] for k in range(self.shape[0])[sel]]
            return np.stack(picked) if picked else np.empty((0, *self._plane_shape), self.dtype)[:, y, x]
        return self._plane(range(self.shape[0])[sel])[y, x]     # IndexError when out of range, like numpy

    def _plane(self, k: int) -> np.ndarray:
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        data = _data(self._io, url)
        if _header(data, url) != (self._plane_shape, self.dtype, self._n):
            raise ValueError(f"{url}: its header differs from the first file's {self.dtype} {self._plane_shape}, "
                             f"{self._n} samples; all channels of one field must agree")
        a = np.asarray(imagecodecs.png_decode(data))
        want = self._plane_shape if self._n == 1 else (*self._plane_shape, self._n)
        if a.shape != want or a.dtype != self.dtype:        # e.g. a tRNS chunk decoded as an extra alpha sample
            raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, its header says {self.dtype} {want}")
        plane = a if self._sample is None else np.ascontiguousarray(a[:, :, self._sample])
        self._last = (k, plane)
        return plane


class _Polygons:
    """The polygons form: GeoJSON features filled onto the grid of the PNG named beside the GeoJSON url."""

    def __init__(self, io: IO, geojson: str, png: str, y_up: bool):
        self._io, self._url, self._y_up = io, geojson, y_up
        self.shape, _, _ = _header(_data(io, png, HEADER), png)     # the grid only; no pixel of the PNG is read
        self.axes = "yx"
        self.dtype = np.dtype("uint16")
        self.voxel_size_nm = None
        self.access = "file"            # the whole GeoJSON is read and filled once, on the first read
        self._mask: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._mask is None:
            self._mask = _rasterise(_data(self._io, self._url), self._url, self.shape, self._y_up)
        return self._mask[region.get("y", slice(None)), region.get("x", slice(None))]


def _data(io: IO, url: str, n: int = -1) -> bytes:
    """The first `n` bytes of the file, or all of it: by range when the server allows, else staged whole."""
    try:
        with io.open(url) as fh:
            return fh.read(n)
    except NeedsFetch:      # a deflated zip member, or a server without Range / Content-Length
        data = io.fetch(url).read_bytes()
        return data if n < 0 else data[:n]


def _header(data: bytes, url: str) -> tuple[tuple[int, int], np.dtype, int]:
    """(height, width), dtype and samples per pixel from the signature and IHDR chunk; ValueError for a palette
    PNG or a bit depth other than 8 or 16."""
    if len(data) < HEADER or data[:8] != SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    if colour not in SAMPLES or depth not in DTYPES:
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; this snippet reads grey, RGB, "
                         "RGBA and grey+alpha PNGs (colour types 0, 2, 6, 4) of 8 or 16 bits")
    return (height, width), DTYPES[depth], SAMPLES[colour]


def _rasterise(data: bytes, url: str, shape: tuple[int, int], y_up: bool) -> np.ndarray:
    """uint16 ids on `shape`: pixel (row, col) holds the 1-based position of the last feature whose polygon
    encloses the pixel's centre, 0 where none does."""
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise ValueError(f"{url}: not JSON: {e}") from e
    features = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError(f"{url}: not a GeoJSON FeatureCollection with a features list")
    if len(features) > MAX_IDS:
        raise ValueError(f"{url}: {len(features)} features exceed the {MAX_IDS} ids of a uint16 label")
    out = np.zeros(shape, np.uint16)
    for number, feature in enumerate(features, 1):
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        kind = geometry.get("type") if isinstance(geometry, dict) else None
        if kind not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"{url}: feature {number} has geometry {kind!r}; only Polygon and MultiPolygon "
                             "features enclose pixels")
        polygons = geometry.get("coordinates")
        for polygon in [polygons] if kind == "Polygon" else polygons:
            try:
                rings = [np.asarray(ring, dtype=np.float64)[:, :2] for ring in polygon]
            except (TypeError, ValueError, IndexError) as e:
                raise ValueError(f"{url}: feature {number}: coordinates are not rings of [x, y] positions") from e
            if any(r.shape[0] < 3 or r.shape[1] != 2 or not np.isfinite(r).all() for r in rings):
                raise ValueError(f"{url}: feature {number}: a ring needs at least 3 finite [x, y] positions")
            if y_up:
                rings = [np.column_stack([r[:, 0], shape[0] - r[:, 1]]) for r in rings]
            _fill(out, rings, number)
    return out


def _fill(out: np.ndarray, rings: list[np.ndarray], number: int) -> None:
    """Set `out` to `number` at every pixel whose centre (col, row) lies inside the rings by the even-odd rule
    (so a ring inside another is a hole). Vertices are (x, y) = (col, row); an integer coordinate is a pixel
    centre. A centre exactly on an outline belongs to the polygon on its left and upper sides only."""
    h, w = out.shape
    every = np.concatenate(rings)
    r0, r1 = max(int(np.ceil(every[:, 1].min())), 0), min(int(np.ceil(every[:, 1].max())), h)   # rows r0..r1-1
    c0, c1 = max(int(np.ceil(every[:, 0].min())), 0), min(int(np.ceil(every[:, 0].max())), w)   # cols c0..c1-1
    if r0 >= r1 or c0 >= c1:
        return                                  # entirely off the grid, or thinner than a pixel centre
    toggles = np.zeros((r1 - r0, c1 - c0 + 1), np.int32)    # +1 at the first column right of each edge crossing
    for ring in rings:
        x0, y0 = ring[:, 0], ring[:, 1]
        x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
        lo = np.clip(np.ceil(np.minimum(y0, y1)), r0, r1).astype(np.int64)      # an edge crosses the rows
        hi = np.clip(np.ceil(np.maximum(y0, y1)), r0, r1).astype(np.int64)      # lo <= row < hi
        n = hi - lo                                                             # 0 for a horizontal edge
        if not n.sum():
            continue
        edge = np.repeat(np.arange(len(n)), n)
        row = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + lo[edge]
        cross = x0[edge] + (row - y0[edge]) * (x1[edge] - x0[edge]) / (y1[edge] - y0[edge])
        col = np.clip(np.ceil(cross), c0, c1).astype(np.int64)
        np.add.at(toggles, (row - r0, col - c0), 1)
    inside = np.cumsum(toggles, axis=1)[:, :-1] % 2 == 1
    out[r0:r1, c0:c1][inside] = number


def _is_geojson(url: str) -> bool:
    return url.split("?", 1)[0].split("#", 1)[0].lower().endswith(GEOJSON)


def open_array(src: Source, io: IO) -> Array:
    sample, y_up = src.params.get("sample"), src.params.get("y_up")
    if sample is not None and (isinstance(sample, bool) or not isinstance(sample, int)):
        raise ValueError(f"params.sample must be an integer sample index, got {sample!r}")
    if y_up is not None and not isinstance(y_up, bool):
        raise ValueError(f"params.y_up must be true or false, got {y_up!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    documents = [u for u in urls if _is_geojson(u)]
    images = [u for u in urls if not _is_geojson(u)]
    if documents:
        if len(documents) != 1 or len(images) != 1:
            raise ValueError("the polygons form takes exactly one GeoJSON url and one PNG url (the grid), got "
                             f"{len(documents)} and {len(images)}")
        if sample is not None:
            raise ValueError("params.sample is for the sample form (one colour PNG), not for GeoJSON polygons")
        return _Polygons(io, documents[0], images[0], bool(y_up))
    if y_up is not None:
        raise ValueError("params.y_up is for the polygons form (a .json or .geojson url); got PNG urls only")
    if not images:
        raise ValueError("png-field-sample-geojson needs at least one url")
    if sample is not None and len(images) != 1:
        raise ValueError(f"the sample form reads one PNG, got {len(images)} urls")
    return _Png(io, images, sample)
