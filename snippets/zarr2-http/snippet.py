"""One Zarr v2 array at a plain HTTP(S) URL, read chunk by chunk without a directory listing.

The URL is the array's directory or its `.zarray` object. Chunk keys are derived from `.zarray` (shape, chunks,
dimension_separator), so a store with no directory index (an S3 bucket over https) works; a chunk object the
server answers 404 for is an unwritten chunk and reads as `fill_value`. `.zattrs` `resolution` gives the voxel
size. With `params.grid` the array is presented on another array's voxel grid, at the index its `.zattrs`
`offset` puts it: a label that is an offset crop of its raw then has the raw's shape.
"""
import json
import zlib
from typing import Callable

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

DEFAULT_AXES = {2: "yx", 3: "zyx", 4: "czyx"}   # by array rank, unless params.axes says otherwise
CACHE_BYTES = 128 << 20                          # decoded chunks kept per array (most recently used)
DECODERS: dict[str, Callable[[bytes], bytes]] = {
    "zlib": zlib.decompress,
    "gzip": lambda b: zlib.decompress(b, 31),
    "blosc": lambda b: bytes(imagecodecs.blosc_decode(b)),
    "zstd": lambda b: bytes(imagecodecs.zstd_decode(b)),
}


class _Meta:
    """`.zarray` and `.zattrs` of one array directory."""

    def __init__(self, io: IO, url: str):
        self.url = _array_url(url)
        meta = _json(io, f"{self.url}/.zarray")
        if meta is None:
            raise FileNotFoundError(f"{self.url}/.zarray not found (HTTP 404): the url must be a Zarr v2 array "
                                    "directory or its .zarray, not a group and not a Zarr v3 array (zarr.json)")
        if meta.get("zarr_format") != 2:
            raise ValueError(f"{self.url}: zarr_format {meta.get('zarr_format')!r}, this snippet reads Zarr v2")
        self.shape = tuple(int(n) for n in meta["shape"])
        self.chunks = tuple(int(n) for n in meta["chunks"])
        if not self.shape or len(self.chunks) != len(self.shape) or min(self.chunks) < 1:
            raise ValueError(f"{self.url}: shape {self.shape} with chunks {self.chunks}")
        self.stored = np.dtype(meta["dtype"])       # as in the chunks, byte order included
        if self.stored.kind not in "biuf":
            raise ValueError(f"{self.url}: dtype {meta['dtype']!r} is not a plain bool, integer or float type")
        if meta.get("filters"):
            raise ValueError(f"{self.url}: filters {meta['filters']!r} are not supported")
        compressor = meta.get("compressor")
        if compressor is not None and compressor.get("id") not in DECODERS:
            raise ValueError(f"{self.url}: compressor {compressor.get('id')!r} is not one of {sorted(DECODERS)}")
        self.decode: Callable[[bytes], bytes] | None = DECODERS[compressor["id"]] if compressor else None
        self.order: str = meta.get("order", "C")
        if self.order not in ("C", "F"):
            raise ValueError(f"{self.url}: order {self.order!r}")
        self.separator: str = meta.get("dimension_separator") or "."
        fill = meta.get("fill_value")
        self.fill = np.zeros((), self.stored.newbyteorder("="))[()] if fill is None else \
            np.array(float(fill) if isinstance(fill, str) else fill).astype(self.stored.newbyteorder("="))[()]
        attrs = _json(io, f"{self.url}/.zattrs") or {}
        self.resolution = _per_axis(attrs.get("resolution"), len(self.shape))
        self.offset = _per_axis(attrs.get("offset"), len(self.shape))


class _ZarrArray:
    def __init__(self, io: IO, meta: _Meta, axes: str, grid: _Meta | None):
        self._io, self._m = io, meta
        self.axes = axes
        self.dtype = meta.stored.newbyteorder("=")
        self.access = "chunk"
        self._shift = (0,) * len(meta.shape)        # index of this array's voxel 0 on the presented grid
        self.shape: tuple[int, ...] = meta.shape
        if grid is not None:
            self._shift, self.shape = _placement(meta, grid), grid.shape
        res = meta.resolution
        spatial = [(a, r) for a, r in zip(axes, res or ()) if a in "zyx"]
        self.voxel_size_nm = {a: r for a, r in spatial} if spatial and all(r > 0 for _, r in spatial) else None
        self._missing: set[str] = set()             # chunk keys the server answered 404 for
        self._cache: dict[str, np.ndarray] = {}     # decoded chunks, oldest first

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
        """Voxels [lo, hi) of the presented grid; whatever lies outside the stored array is fill_value."""
        m = self._m
        out = np.full([h - l for l, h in zip(lo, hi)], m.fill, self.dtype)
        # the same box in the stored array's own index, clipped to its extent
        a_lo = [max(l - s, 0) for l, s in zip(lo, self._shift)]
        a_hi = [min(h - s, n) for h, s, n in zip(hi, self._shift, m.shape)]
        if any(h <= l for l, h in zip(a_lo, a_hi)):
            return out
        spans = [range(l // c, (h - 1) // c + 1) for l, h, c in zip(a_lo, a_hi, m.chunks)]
        for idx in np.ndindex(*[len(s) for s in spans]):
            cidx = [s[i] for s, i in zip(spans, idx)]
            chunk = self._chunk(cidx)
            if chunk is None:
                continue
            src, dst = [], []
            for k, c, l, h, s, o in zip(cidx, m.chunks, a_lo, a_hi, self._shift, lo):
                start, stop = max(l, k * c), min(h, (k + 1) * c)        # in the stored array's index
                src.append(slice(start - k * c, stop - k * c))
                dst.append(slice(start + s - o, stop + s - o))
            out[tuple(dst)] = chunk[tuple(src)]
        return out

    def _chunk(self, cidx: list[int]) -> np.ndarray | None:
        """One decoded chunk (always the full chunk shape, as Zarr v2 stores it), or None when it was never written."""
        m = self._m
        key = m.separator.join(str(k) for k in cidx)
        if key in self._missing:
            return None
        if key in self._cache:
            self._cache[key] = self._cache.pop(key)     # most recently used last
            return self._cache[key]
        data = _body(self._io, f"{m.url}/{key}")
        if data is None:
            self._missing.add(key)
            return None
        if m.decode is not None:
            data = m.decode(data)
        if len(data) != int(np.prod(m.chunks)) * m.stored.itemsize:
            raise ValueError(f"{m.url}/{key}: {len(data)} bytes decoded, a {m.stored} chunk of {m.chunks} holds "
                             f"{int(np.prod(m.chunks)) * m.stored.itemsize}")
        chunk = np.frombuffer(data, m.stored).reshape(m.chunks, order=m.order).astype(self.dtype, copy=False)
        self._cache[key] = chunk
        while len(self._cache) > 1 and sum(c.nbytes for c in self._cache.values()) > CACHE_BYTES:
            del self._cache[next(iter(self._cache))]
        return chunk


def _array_url(url: str) -> str:
    """The array directory's url without a trailing slash, from the directory url or its `.zarray` url."""
    url = url.rstrip("/")
    return url[:-len("/.zarray")] if url.endswith("/.zarray") else url


def _body(io: IO, url: str) -> bytes | None:
    """The whole object, or None when the server says 404 (in a Zarr store: this key was never written)."""
    try:
        try:
            with io.open(url) as fh:
                return fh.read()
        except NeedsFetch:      # no Content-Length, or a server without Range: stage the object whole
            return io.fetch(url).read_bytes()
    except Exception as e:      # io raises its HTTP client's status error; 404 is the only status read as absence
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return None
        raise


def _json(io: IO, url: str) -> dict | None:
    data = _body(io, url)
    if data is None:
        return None
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise ValueError(f"{url} is not a JSON object")
    return doc


def _per_axis(value: object, rank: int) -> tuple[float, ...] | None:
    """A `.zattrs` list with one number per array dimension, else None."""
    if not isinstance(value, list) or len(value) != rank:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return None
    return tuple(float(v) for v in value)


def _placement(meta: _Meta, grid: _Meta) -> tuple[int, ...]:
    """Index on `grid` of `meta`'s voxel 0: (offset - grid offset) / resolution, which must be whole voxels."""
    if len(meta.shape) != len(grid.shape):
        raise ValueError(f"{meta.url} has rank {len(meta.shape)}, params.grid {grid.url} has rank {len(grid.shape)}")
    for m in (meta, grid):
        if m.offset is None or m.resolution is None or min(m.resolution) <= 0:
            raise ValueError(f"params.grid needs .zattrs 'offset' and 'resolution' (one number per dimension) on "
                             f"both arrays; {m.url} has offset {m.offset}, resolution {m.resolution}")
    if meta.resolution != grid.resolution:
        raise ValueError(f"{meta.url} has resolution {meta.resolution}, params.grid {grid.url} has "
                         f"{grid.resolution}; an array is only placed on a grid of its own voxel size")
    shift = [(o - g) / r for o, g, r in zip(meta.offset, grid.offset, meta.resolution)]
    if any(abs(s - round(s)) > 1e-6 for s in shift):
        raise ValueError(f"{meta.url} offset {meta.offset} is not a whole number of voxels from params.grid "
                         f"{grid.url} offset {grid.offset} at resolution {meta.resolution}")
    return tuple(int(round(s)) for s in shift)


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"zarr2-http reads one array, got {len(src.urls)} urls")
    meta = _Meta(io, src.urls[0])
    axes = src.params.get("axes")
    if axes is None:
        axes = DEFAULT_AXES.get(len(meta.shape))
        if axes is None:
            raise ValueError(f"{meta.url} has rank {len(meta.shape)} {meta.shape}; set params.axes (one czyx "
                             "letter per dimension, with a reason in the binding)")
    if not isinstance(axes, str) or len(axes) != len(meta.shape):
        raise ValueError(f"params.axes {axes!r} does not give one letter per dimension of {meta.shape}")
    grid = src.params.get("grid")
    if grid is not None and not isinstance(grid, str):
        raise ValueError(f"params.grid must be the url of a Zarr v2 array, got {grid!r}")
    return _ZarrArray(io, meta, axes, _Meta(io, grid) if grid is not None else None)
