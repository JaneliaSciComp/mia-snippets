"""One resolution level of an OME-Zarr v2 image (NGFF multiscales) at a plain HTTP(S) URL, one timepoint at a time.

The URL is a level array's directory or its `.zarray` object (`<store>.ome.zarr/0/.zarray`). The group one
directory up names the array's axes and its voxel size (`.zattrs` `multiscales`: `axes` and the level's `scale`).
The harness has no time axis, so `params.t` picks one timepoint and the array is what is left; `params.c` and
`params.z` pick one channel or one z plane the same way, and `params.z_project: max` replaces the z axis by the
maximum over all z planes, for a 2D label that was made on the raw's maximum intensity projection. Chunk keys are
derived from `.zarray`, so a store with no directory index (an S3 bucket over https) works; a chunk object the
server answers 404 for is an unwritten chunk and reads as `fill_value`.
"""
import json
import zlib
from typing import Callable

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

STORED_AXES = "tczyx"                            # the axis names an OME-Zarr image may have
CACHE_BYTES = 256 << 20                          # decoded chunks kept per array (most recently used)
NM_PER_UNIT = {"angstrom": 0.1, "nanometer": 1.0, "micrometer": 1e3, "millimeter": 1e6, "centimeter": 1e7,
               "meter": 1e9}                     # NGFF (UDUNITS-2) names of the length units read
DECODERS: dict[str, Callable[[bytes], bytes]] = {
    "zlib": zlib.decompress,
    "gzip": lambda b: zlib.decompress(b, 31),
    "blosc": lambda b: bytes(imagecodecs.blosc_decode(b)),
    "zstd": lambda b: bytes(imagecodecs.zstd_decode(b)),
}


class _Meta:
    """`.zarray` of one level array, and its axes and voxel size from the group's `.zattrs` multiscales."""

    def __init__(self, io: IO, url: str):
        self.url = _array_url(url)
        meta = _json(io, f"{self.url}/.zarray")
        if meta is None:
            raise FileNotFoundError(f"{self.url}/.zarray not found (HTTP 404): the url must be a level array of an "
                                    "OME-Zarr v2 image (<store>/0 or <store>/0/.zarray), not the group and not a "
                                    "Zarr v3 array (zarr.json)")
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
        self.axes, self.voxel_nm = _multiscale(io, self.url, len(self.shape))


class _OmeZarrArray:
    def __init__(self, io: IO, meta: _Meta, pins: dict[str, int], project: str | None):
        self._io, self._m = io, meta
        self._pins, self._project = pins, project   # stored axis -> its one index; the stored axis reduced by max
        self.axes = "".join(a for a in meta.axes if a not in pins and a != project)
        self.shape: tuple[int, ...] = tuple(n for a, n in zip(meta.axes, meta.shape) if a in self.axes)
        self.dtype = meta.stored.newbyteorder("=")
        size, chunk = dict(zip(meta.axes, meta.shape)), dict(zip(meta.axes, meta.chunks))
        self.access = "plane" if all(chunk[a] >= size[a] for a in "yx") else "chunk"
        voxel = {a: meta.voxel_nm[a] for a in self.axes if a in "zyx"} if meta.voxel_nm else None
        self.voxel_size_nm: dict[str, float] | None = voxel or None
        self._missing: set[str] = set()             # chunk keys the server answered 404 for
        self._cache: dict[str, np.ndarray] = {}     # decoded chunks, oldest first

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        sels: list[int | slice] = [self._pins[a] if a in self._pins else region.get(a, slice(None))
                                   for a in self._m.axes]
        if self._project is None:
            return self._select(sels)
        i = self._m.axes.index(self._project)
        out = None
        for k in range(self._m.shape[i]):           # one plane of the projected axis at a time
            sels[i] = k
            plane = self._select(sels)
            out = plane if out is None else np.maximum(out, plane)
        return np.asarray(out)

    def _select(self, sels: list[int | slice]) -> np.ndarray:
        """numpy-style selection of the stored array, one int or slice per stored axis; an int drops its axis."""
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
        """Voxels [lo, hi) of the stored array; chunks that were never written are fill_value."""
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


def _multiscale(io: IO, url: str, rank: int) -> tuple[str, dict[str, float] | None]:
    """(axes, voxel size in nm per spatial axis or None) of the level array at `url`, from the `multiscales` of
    the group one directory up: the entry whose `datasets` name the array's directory."""
    group, _, path = url.rpartition("/")
    attrs = _json(io, f"{group}/.zattrs") or {}
    multiscales = attrs.get("multiscales")
    for ms in multiscales if isinstance(multiscales, list) else []:
        for ds in ms.get("datasets") or [] if isinstance(ms, dict) else []:
            if isinstance(ds, dict) and str(ds.get("path")).strip("/") == path:
                axes, units = _axes(ms.get("axes"), rank, group)
                return axes, _voxel_nm(axes, units, [ds.get("coordinateTransformations"),
                                                     ms.get("coordinateTransformations")])
    raise ValueError(f"{group}/.zattrs has no multiscales dataset with path {path!r}: the url must be a level "
                     "array directly inside an OME-Zarr v2 image group")


def _axes(listed: object, rank: int, group: str) -> tuple[str, list[str | None]]:
    """Axis letters in storage order and each axis's unit, from a multiscale's `axes` (NGFF 0.4 objects, 0.3
    names; before 0.3 there is none and an image is tczyx)."""
    if listed is None and rank == 5:
        return STORED_AXES, [None] * 5
    items = listed if isinstance(listed, list) else []
    names = [str(a.get("name") if isinstance(a, dict) else a).lower() for a in items]
    units = [a.get("unit") if isinstance(a, dict) else None for a in items]
    axes = "".join(names)
    if (len(names) != rank or any(n not in tuple(STORED_AXES) for n in names) or len(set(names)) != rank
            or not {"y", "x"} <= set(names)):
        raise ValueError(f"{group}/.zattrs multiscales axes {listed!r} are not {rank} distinct axes named "
                         f"t, c, z, y, x with y and x among them")
    return axes, units


def _voxel_nm(axes: str, units: list[str | None], transforms: list[object]) -> dict[str, float] | None:
    """The level's `scale` (times the multiscale's own, when it has one) in nm for each spatial axis; None when a
    scale is missing or not positive, or a spatial axis has no length unit this snippet knows."""
    scale, found = [1.0] * len(axes), False
    for listed in transforms:
        for tr in listed if isinstance(listed, list) else []:
            if isinstance(tr, dict) and tr.get("type") == "scale":
                s = tr.get("scale")
                if (not isinstance(s, list) or len(s) != len(axes)
                        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in s)):
                    return None
                scale, found = [a * float(b) for a, b in zip(scale, s)], True
    voxel = {a: s * NM_PER_UNIT.get(str(u), 0.0) for a, s, u in zip(axes, scale, units) if a in "zyx"}
    return voxel if found and all(v > 0 for v in voxel.values()) else None


def _index(params: dict, key: str, meta: _Meta) -> int | None:
    """params[key] as an index on the stored axis `key`, checked against its length; None when not given."""
    value = params.get(key)
    if value is None:
        return None
    if key not in meta.axes:
        raise ValueError(f"params.{key} is set but {meta.url} has no {key} axis (axes {meta.axes})")
    n = meta.shape[meta.axes.index(key)]
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < n:
        raise ValueError(f"params.{key} must be an index from 0 to {n - 1} on the {key} axis of {meta.url} "
                         f"({meta.axes} {meta.shape}), got {value!r}")
    return value


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"ome-zarr2-http reads one level array, got {len(src.urls)} urls")
    meta = _Meta(io, src.urls[0])
    pins = {a: i for a in "tcz" for i in [_index(src.params, a, meta)] if i is not None}
    if "t" in meta.axes and "t" not in pins:
        if meta.shape[meta.axes.index("t")] != 1:
            raise ValueError(f"{meta.url} has {meta.shape[meta.axes.index('t')]} timepoints ({meta.axes} "
                             f"{meta.shape}); set params.t to the one to read (with a reason in the binding)")
        pins["t"] = 0
    project = src.params.get("z_project")
    if project is not None:
        if project != "max":
            raise ValueError(f"params.z_project must be 'max', got {project!r}")
        if "z" in pins or "z" not in meta.axes:
            raise ValueError(f"params.z_project needs a z axis ({meta.url} has {meta.axes}) and no params.z")
    return _OmeZarrArray(io, meta, pins, "z" if project else None)
