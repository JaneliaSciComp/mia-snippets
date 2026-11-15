"""One resolution level of an OME-Zarr image (Zarr v2) at a plain HTTP(S) URL, at one timepoint.

The URL is the level's array directory or its `.zarray` object (`<image>.ome.zarr/0/.zarray`). The image group
one directory up gives, in its `.zattrs` `multiscales` (OME-NGFF 0.3 / 0.4), the axis order and this level's
`scale`; an array of rank 5 without that metadata is `tczyx` (OME-NGFF 0.1 / 0.2). A time axis is not presented:
`params.t` picks one index on it (optional when the axis has one entry), and the array is the rest, e.g. `czyx`.
A channel axis with one entry is not presented either (`zyx`), so a one-channel label pairs with a raw of several.
Chunk keys are derived from `.zarray`, so a store with no directory index (an S3 bucket over https) works; a
chunk object the server answers 404 for is an unwritten chunk and reads as `fill_value`.
"""
import json
import zlib
from typing import Callable

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

STORED_AXES = "tczyx"                            # letters an OME-Zarr axis name may map to; t is taken by params.t
NGFF_01_AXES = "tczyx"                           # OME-NGFF 0.1 / 0.2: every array is 5D in this order
CACHE_BYTES = 128 << 20                          # decoded chunks kept per array (most recently used)
NM_PER_UNIT: dict[str, float] = {                # OME-NGFF 0.4 space units (UDUNITS-2 names) this snippet converts
    "picometer": 1e-3, "angstrom": 0.1, "nanometer": 1.0, "micrometer": 1e3, "millimeter": 1e6,
    "centimeter": 1e7, "decimeter": 1e8, "meter": 1e9,
}
DECODERS: dict[str, Callable[[bytes], bytes]] = {
    "zlib": zlib.decompress,
    "gzip": lambda b: zlib.decompress(b, 31),
    "blosc": lambda b: bytes(imagecodecs.blosc_decode(b)),
    "zstd": lambda b: bytes(imagecodecs.zstd_decode(b)),
}


class _Meta:
    """`.zarray` of one level array, and what the image group's `.zattrs` says about it."""

    def __init__(self, io: IO, url: str):
        self.url = _array_url(url)
        meta = _json(io, f"{self.url}/.zarray")
        if meta is None:
            raise FileNotFoundError(f"{self.url}/.zarray not found (HTTP 404): the url must be one level array of "
                                    "the OME-Zarr image (<image>.zarr/0) or its .zarray, not the image group and "
                                    "not a Zarr v3 array (zarr.json)")
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
        group, _, level = self.url.rpartition("/")
        self.axes, self.voxel_size_nm = _ngff(_json(io, f"{group}/.zattrs") or {}, group, level, self.url, self.shape)


class _Timepoint:
    """The stored array with its time axis, if it has one, fixed at one index, and a channel axis of one entry at 0."""

    def __init__(self, io: IO, meta: _Meta, t: int | None):
        self._io, self._m = io, meta
        self._fixed: dict[str, int] = {}            # stored axis -> the one index read on it; not presented
        if "t" in meta.axes:
            n = meta.shape[meta.axes.index("t")]
            if t is None:
                if n != 1:
                    raise ValueError(f"{meta.url} has {n} timepoints ({meta.axes} {meta.shape}); set params.t "
                                     f"(0..{n - 1}, the same on a raw and its labels, with a reason in the binding)")
                t = 0
            if not 0 <= t < n:
                raise ValueError(f"params.t {t} is outside the {n} timepoints of {meta.url} ({meta.axes} {meta.shape})")
            self._fixed["t"] = t
        elif t is not None:
            raise ValueError(f"params.t {t} given, but {meta.url} has no time axis ({meta.axes} {meta.shape})")
        if "c" in meta.axes and meta.shape[meta.axes.index("c")] == 1:
            self._fixed["c"] = 0    # the harness reads a label that has a c axis at its raw's channel index
        self.axes = "".join(a for a in meta.axes if a not in self._fixed)
        self.shape: tuple[int, ...] = tuple(n for a, n in zip(meta.axes, meta.shape) if a not in self._fixed)
        self.dtype = meta.stored.newbyteorder("=")
        self.voxel_size_nm = meta.voxel_size_nm
        self.access = "chunk"
        self._missing: set[str] = set()             # chunk keys the server answered 404 for
        self._cache: dict[str, np.ndarray] = {}     # decoded chunks, oldest first

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        unknown = sorted(set(region) - set(self.axes))
        if unknown:
            raise ValueError(f"region keys {unknown} are not axes of this array ({self.axes})")
        sels = [self._fixed[a] if a in self._fixed else region.get(a, slice(None)) for a in self._m.axes]
        picks = [range(n)[s] if isinstance(s, slice) else range(range(n)[s], range(n)[s] + 1)
                 for s, n in zip(sels, self._m.shape)]  # an int out of range raises IndexError, like numpy
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
        """Voxels [lo, hi) of the stored array (every stored dimension, the fixed ones included)."""
        m = self._m
        out = np.full([h - l for l, h in zip(lo, hi)], m.fill, self.dtype)
        spans = [range(l // c, (h - 1) // c + 1) for l, h, c in zip(lo, hi, m.chunks)]
        for idx in np.ndindex(*[len(s) for s in spans]):
            cidx = [s[i] for s, i in zip(spans, idx)]
            chunk = self._chunk(cidx)
            if chunk is None:
                continue
            src, dst = [], []
            for k, c, l, h in zip(cidx, m.chunks, lo, hi):
                start, stop = max(l, k * c), min(h, (k + 1) * c)
                src.append(slice(start - k * c, stop - k * c))
                dst.append(slice(start - l, stop - l))
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


def _ngff(attrs: dict, group: str, level: str, url: str, shape: tuple[int, ...]) -> tuple[str, dict[str, float] | None]:
    """(stored axes, voxel size in nm or None) of the level array `level` from its image group's `.zattrs`."""
    entries = attrs.get("multiscales")
    found = [(m, d) for m in entries if isinstance(m, dict) for d in m.get("datasets") or []
             if isinstance(d, dict) and str(d.get("path", "")).strip("/") == level] if isinstance(entries, list) else []
    image, dataset = found[0] if found else ({}, {})
    listed = image.get("axes")
    if isinstance(listed, list) and listed:
        names = [str(a.get("name") if isinstance(a, dict) else a).lower() for a in listed]
        if any(len(n) != 1 or n not in STORED_AXES for n in names) or len(set(names)) != len(names):
            raise ValueError(f"{group}/.zattrs: multiscales axes {names} are not distinct names from {STORED_AXES}")
        axes = "".join(names)
        if len(axes) != len(shape):
            raise ValueError(f"{group}/.zattrs: multiscales axes {axes!r} do not give one axis per dimension of "
                             f"{url} {shape}")
    elif len(shape) == len(NGFF_01_AXES):
        axes = NGFF_01_AXES     # 0.1 / 0.2 name no axes: always tczyx. Also a rank-5 array with no group metadata
    else:
        raise ValueError(f"{group}/.zattrs names no multiscales axes for dataset {level!r}, and {url} has rank "
                         f"{len(shape)} {shape}: only a rank-5 array is read without them (as {NGFF_01_AXES})")
    if not {"y", "x"} <= set(axes):
        raise ValueError(f"{group}/.zattrs: multiscales axes {axes!r} have no y and x")
    scale = _scale(dataset.get("coordinateTransformations"), len(shape))
    outer = _scale(image.get("coordinateTransformations"), len(shape))    # 0.4: applied after the dataset's own
    if scale is None or "coordinateTransformations" in image and outer is None:
        return axes, None
    voxel: dict[str, float] = {}
    for i, a in enumerate(axes):
        if a not in "zyx":
            continue
        unit = listed[i].get("unit") if isinstance(listed, list) and isinstance(listed[i], dict) else None
        size = scale[i] * (outer[i] if outer else 1.0) * NM_PER_UNIT.get(str(unit), 0.0)
        if not size > 0:        # no unit, a unit this snippet does not convert, or a non-positive scale
            return axes, None
        voxel[a] = size
    return axes, voxel or None


def _scale(transforms: object, rank: int) -> tuple[float, ...] | None:
    """The `scale` of an OME-NGFF 0.4 coordinateTransformations list (one number per dimension), else None."""
    if not isinstance(transforms, list):
        return None
    scales = [t.get("scale") for t in transforms if isinstance(t, dict) and t.get("type") == "scale"]
    if len(scales) != 1 or not isinstance(scales[0], list) or len(scales[0]) != rank:
        return None             # none, several, or a scale given as a path to a binary file
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in scales[0]):
        return None
    return tuple(float(v) for v in scales[0])


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"ome-zarr2-http-timepoint reads one level array, got {len(src.urls)} urls")
    t = src.params.get("t")
    if t is not None and (isinstance(t, bool) or not isinstance(t, int)):
        raise ValueError(f"params.t must be an integer index on the time axis, got {t!r}")
    return _Timepoint(io, _Meta(io, src.urls[0]), t)
