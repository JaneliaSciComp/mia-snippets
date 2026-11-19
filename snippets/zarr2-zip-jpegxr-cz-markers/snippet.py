"""One Zarr v2 array of JPEG XR chunks stored as members of a zip, or a Zeiss ZEN graphics file on its grid.

Raw form, one url `<zip>::<array dir>` (or its `.zarray`): the array's chunks are zip members `<i>.<j>[.0]`,
each one JPEG XR image (compressor `imagecodecs_jpegxr`), presented as yx or yxc. A chunk member the zip does
not hold reads as `fill_value`.

Label form, two urls, `<a .cz url or zip member>` then the raw form's url: the `.cz` is a ZEN (blue)
GraphicsDocument XML whose Marker (point) or Rectangle elements are drawn on the array's yx grid as instance
ids, in the pixel coordinates the file gives. No chunk is read for a label.
"""
import json
import math
from xml.etree import ElementTree

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

CODEC = "imagecodecs_jpegxr"
CACHE_BYTES = 256 << 20         # decoded chunks kept per array (most recently used)
ELEMENTS = {"marker": "Marker", "rectangle": "Rectangle"}


class _Grid:
    """`.zarray` of one array directory inside a zip, and which of its chunk members exist."""

    def __init__(self, io: IO, url: str):
        url = url.rstrip("/")
        self.url = url[:-len("/.zarray")] if url.endswith("/.zarray") else url
        zip_url, sep, prefix = self.url.partition("::")
        if not sep or not prefix:
            raise ValueError(f"{url}: the array must be a zip member directory, <zip url>::<array dir>")
        self.members = {n[len(prefix) + 1:] for n in io.list(zip_url) if n.startswith(prefix + "/")}
        if ".zarray" not in self.members:
            raise FileNotFoundError(f"{self.url}/.zarray is not a member of the zip: the url must name a Zarr v2 "
                                    "array directory, not a group")
        meta = json.loads(_member(io, f"{self.url}/.zarray"))
        if not isinstance(meta, dict) or meta.get("zarr_format") != 2:
            raise ValueError(f"{self.url}: not a Zarr v2 .zarray")
        self.shape = tuple(int(n) for n in meta["shape"])
        self.chunks = tuple(int(n) for n in meta["chunks"])
        if len(self.shape) not in (2, 3) or len(self.chunks) != len(self.shape) or min(self.chunks) < 1:
            raise ValueError(f"{self.url}: shape {self.shape} with chunks {self.chunks}; JPEG XR chunks are images, "
                             "so the array is yx or yx + samples")
        if len(self.shape) == 3 and self.chunks[2] != self.shape[2]:
            raise ValueError(f"{self.url}: chunks {self.chunks} split the samples of shape {self.shape}")
        self.dtype = np.dtype(meta["dtype"]).newbyteorder("=")
        if self.dtype.kind not in "uf":
            raise ValueError(f"{self.url}: dtype {meta['dtype']!r} is not an unsigned integer or float type")
        if meta.get("filters"):
            raise ValueError(f"{self.url}: filters {meta['filters']!r} are not supported")
        compressor = meta.get("compressor") or {}
        if compressor.get("id") != CODEC:
            raise ValueError(f"{self.url}: compressor {compressor.get('id')!r}, this snippet reads {CODEC!r}")
        if meta.get("order", "C") != "C":
            raise ValueError(f"{self.url}: order {meta.get('order')!r}")
        self.separator: str = meta.get("dimension_separator") or "."
        self.fill = np.array(meta.get("fill_value") or 0).astype(self.dtype)[()]


class _Boxed:
    """`read` for an array that can produce any box [lo, hi) of its grid."""

    axes: str
    shape: tuple[int, ...]
    dtype: np.dtype

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        sels = [region.get(a, slice(None)) for a in self.axes]
        picks = [range(n)[s] if isinstance(s, slice) else range(range(n)[s], range(n)[s] + 1)
                 for s, n in zip(sels, self.shape)]     # an int out of range raises IndexError, like numpy
        if any(len(p) == 0 for p in picks):
            block = np.empty([len(p) for p in picks], self.dtype)
        else:
            lo = [min(p[0], p[-1]) for p in picks]
            hi = [max(p[0], p[-1]) + 1 for p in picks]
            block = self._box(lo, hi)
            for i, p in enumerate(picks):
                if p.step != 1:
                    block = np.take(block, np.asarray(p) - lo[i], axis=i)
        return block[tuple(slice(None) if isinstance(s, slice) else 0 for s in sels)]

    def _box(self, lo: list[int], hi: list[int]) -> np.ndarray:
        raise NotImplementedError


class _JxrZarr(_Boxed):
    def __init__(self, io: IO, grid: _Grid):
        self._io, self._g = io, grid
        self.axes = "yxc"[:len(grid.shape)]
        self.shape = grid.shape
        self.dtype = grid.dtype
        self.voxel_size_nm = None                   # the array has no .zattrs
        self.access = "chunk"
        self._cache: dict[str, np.ndarray] = {}     # decoded chunks, oldest first

    def _box(self, lo: list[int], hi: list[int]) -> np.ndarray:
        g = self._g
        out = np.full([h - l for l, h in zip(lo, hi)], g.fill, self.dtype)
        spans = [range(l // c, (h - 1) // c + 1) for l, h, c in zip(lo, hi, g.chunks)]
        for idx in np.ndindex(*[len(s) for s in spans]):
            cidx = [s[i] for s, i in zip(spans, idx)]
            chunk = self._chunk(cidx)
            if chunk is None:
                continue
            src, dst = [], []
            for k, c, l, h in zip(cidx, g.chunks, lo, hi):
                start, stop = max(l, k * c), min(h, (k + 1) * c)
                src.append(slice(start - k * c, stop - k * c))
                dst.append(slice(start - l, stop - l))
            out[tuple(dst)] = chunk[tuple(src)]
        return out

    def _chunk(self, cidx: list[int]) -> np.ndarray | None:
        """One decoded chunk (always the full chunk shape, as Zarr v2 stores it), or None when it was never written."""
        g = self._g
        key = g.separator.join(str(k) for k in cidx)
        if key not in g.members:
            return None
        if key in self._cache:
            self._cache[key] = self._cache.pop(key)     # most recently used last
            return self._cache[key]
        chunk = np.asarray(imagecodecs.jpegxr_decode(_member(self._io, f"{g.url}/{key}")))
        if chunk.shape != g.chunks and chunk.shape + (1,) == g.chunks:
            chunk = chunk[..., None]
        if chunk.shape != g.chunks or chunk.dtype != self.dtype:
            raise ValueError(f"{g.url}/{key}: decoded as {chunk.dtype} {chunk.shape}, .zarray says {self.dtype} "
                             f"chunks of {g.chunks}")
        self._cache[key] = chunk
        while len(self._cache) > 1 and sum(c.nbytes for c in self._cache.values()) > CACHE_BYTES:
            del self._cache[next(iter(self._cache))]
        return chunk


class _Graphics(_Boxed):
    """Marker or Rectangle elements of a ZEN GraphicsDocument as instance ids on a yx grid. An element's id is its
    1-based position among the elements of its kind, in file order."""

    def __init__(self, io: IO, url: str, grid: _Grid, element: str, radius: int):
        root = ElementTree.fromstring(_member(io, url))
        if root.tag != "GraphicsDocument":
            raise ValueError(f"{url}: root element <{root.tag}>, not a ZEN <GraphicsDocument>")
        self.axes = "yx"
        self.shape = grid.shape[:2]
        self.access = "chunk"                       # any box is computed from the element list
        scale = {d.get("Id"): float(d.findtext("Value") or 0) * 1e9 for d in root.iter("Distance")}   # m -> nm
        ok = scale.get("X", 0) > 0 and scale.get("Y", 0) > 0
        self.voxel_size_nm = {"y": scale["Y"], "x": scale["X"]} if ok else None
        found = list(root.iter(ELEMENTS[element]))
        self.dtype = np.dtype("uint16" if len(found) <= 65535 else "uint32")
        self._radius = radius
        ny, nx = self.shape
        # markers: (x, y) of the pixel holding the point; rectangles: centre x, y, half width, half height, cos, sin
        self._items: list[tuple[float, ...]] = []
        for n, el in enumerate(found, 1):
            geometry = el.find("Geometry")
            geo = {c.tag: float(c.text or "nan") for c in (geometry if geometry is not None else ())}
            if element == "marker":
                x, y = geo.get("X", math.nan), geo.get("Y", math.nan)
                if not (0 <= x < nx and 0 <= y < ny):       # also refuses nan
                    raise ValueError(f"{url}: Marker {n} at x={x} y={y} is outside the {ny}x{nx} grid of "
                                     f"{grid.url}: the file was not drawn on this image")
                self._items.append((math.floor(x), math.floor(y)))
            else:
                left, top, w, h = (geo.get(k, math.nan) for k in ("Left", "Top", "Width", "Height"))
                cx, cy = left + w / 2, top + h / 2
                if not (w > 0 and h > 0 and 0 <= cx < nx and 0 <= cy < ny):
                    raise ValueError(f"{url}: Rectangle {n} (left={left} top={top} width={w} height={h}) is not "
                                     f"centred on the {ny}x{nx} grid of {grid.url}")
                a = math.radians(float(el.findtext("Attributes/Rotation") or 0))
                self._items.append((cx, cy, w / 2, h / 2, math.cos(a), math.sin(a)))
        self._marker = element == "marker"

    def _box(self, lo: list[int], hi: list[int]) -> np.ndarray:
        (y0, x0), (y1, x1) = lo, hi
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for n, item in enumerate(self._items, 1):
            if self._marker:
                r = self._radius
                reach = (r, r)
                cx, cy = item[0] + 0.5, item[1] + 0.5
            else:
                cx, cy, hw, hh, cos, sin = item
                reach = (abs(hw * cos) + abs(hh * sin), abs(hw * sin) + abs(hh * cos))
            # the element's bounding box, clipped to the box asked for
            xa, xb = max(math.floor(cx - reach[0]), x0), min(math.ceil(cx + reach[0]), x1)
            ya, yb = max(math.floor(cy - reach[1]), y0), min(math.ceil(cy + reach[1]), y1)
            if xa >= xb or ya >= yb:
                continue
            dx = np.arange(xa, xb)[None, :] + 0.5 - cx      # pixel centres relative to the element's centre
            dy = np.arange(ya, yb)[:, None] + 0.5 - cy
            if self._marker:
                inside = dx * dx + dy * dy <= r * r         # radius 0: the one pixel that holds the point
            else:   # rotate back into the rectangle's own frame (Rotation turns x towards y, clockwise on screen)
                inside = (np.abs(dx * cos + dy * sin) <= hw) & (np.abs(dy * cos - dx * sin) <= hh)
            out[ya - y0:yb - y0, xa - x0:xb - x0][inside] = n
        return out


def _member(io: IO, url: str) -> bytes:
    """The whole file: by range when it is stored, staged when it is deflated."""
    try:
        with io.open(url) as fh:
            return fh.read()
    except NeedsFetch:
        return io.fetch(url).read_bytes()


def open_array(src: Source, io: IO) -> Array:
    label = src.urls[0].lower().endswith(".cz")
    if len(src.urls) != (2 if label else 1):
        raise ValueError("zarr2-zip-jpegxr-cz-markers reads one array url (raw), or a .cz url followed by the "
                         f"array url whose grid it is drawn on (label); got {list(src.urls)}")
    element, radius = src.params.get("element"), src.params.get("radius")
    if not label:
        if element is not None or radius is not None:
            raise ValueError("params.element and params.radius belong to the label form (a .cz url first)")
        return _JxrZarr(io, _Grid(io, src.urls[0]))
    element = "marker" if element is None else element
    if element not in ELEMENTS:
        raise ValueError(f"params.element {element!r} is not one of {sorted(ELEMENTS)}")
    radius = 0 if radius is None else radius
    if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0 or (radius and element != "marker"):
        raise ValueError(f"params.radius {radius!r} must be a whole number of pixels >= 0, with element marker")
    return _Graphics(io, src.urls[0], _Grid(io, src.urls[1]), element, radius)
