"""One resolution level of an NGFF (OME-Zarr v2) image or label whose Zarr store is a directory inside a zip.

The url is `<zip url>::<array dir>` or `<zip url>::<array dir>/.zarray`, e.g. a SpatialData object zipped as
`X.zarr.zip`: `…/X.zarr.zip::X.zarr/images/fluorescence/0` (cyx) or `…::X.zarr/labels/tissue_regions_manual/0`
(yx). The array's chunks are zip members `<array dir>/<i>/<j>/<k>` (or `<i>.<j>.<k>`); a chunk member the zip
does not hold is an unwritten chunk and reads as `fill_value`. The array directory has no `.zattrs`: the group
one directory up names the axes and the pixel size (`.zattrs` `multiscales`: `axes` and the level's `scale`).
"""
import json
import zlib
from typing import Callable

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXES = "czyx"                                    # the axis names the harness knows
CACHE_BYTES = 128 << 20                          # decoded chunks kept per array (most recently used)
NM_PER_UNIT = {"angstrom": 0.1, "nanometer": 1.0, "micrometer": 1e3, "millimeter": 1e6, "centimeter": 1e7,
               "meter": 1e9}                     # NGFF (UDUNITS-2) names of the length units read
DECODERS: dict[str, Callable[[bytes], bytes]] = {
    "zlib": zlib.decompress,
    "gzip": lambda b: zlib.decompress(b, 31),
    "blosc": lambda b: bytes(imagecodecs.blosc_decode(b)),
    "zstd": lambda b: bytes(imagecodecs.zstd_decode(b)),
}


class _Meta:
    """`.zarray` of one level array inside a zip, which of its chunk members exist, and its axes and voxel size
    from the `.zattrs` multiscales of the group above it."""

    def __init__(self, io: IO, url: str):
        url = url.rstrip("/")
        self.url = url[:-len("/.zarray")] if url.endswith("/.zarray") else url
        zip_url, sep, prefix = self.url.partition("::")
        group, slash, path = prefix.rpartition("/")
        if not sep or not slash or not group or not path:
            raise ValueError(f"{url}: the url must be <zip url>::<group dir>/<array dir>, a level array inside an "
                             "NGFF image group that is a directory of the zip")
        names = set(io.list(zip_url))
        self.members = {n[len(prefix) + 1:] for n in names if n.startswith(prefix + "/")}
        if ".zarray" not in self.members:
            raise FileNotFoundError(f"{self.url}/.zarray is not a member of the zip: the url must name a level "
                                    "array of a Zarr v2 store (<group dir>/0), not the group and not a Zarr v3 "
                                    "array (zarr.json)")
        meta = _json(io, f"{self.url}/.zarray")
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
        if f"{group}/.zattrs" not in names:
            raise FileNotFoundError(f"{zip_url}::{group}/.zattrs is not a member of the zip: the array must sit "
                                    "directly inside an NGFF image group, whose multiscales name its axes")
        self.axes, self.voxel_nm = _multiscale(_json(io, f"{zip_url}::{group}/.zattrs"), f"{zip_url}::{group}",
                                               path, len(self.shape))


class _ZipZarrArray:
    def __init__(self, io: IO, meta: _Meta):
        self._io, self._m = io, meta
        self.axes = meta.axes
        self.shape: tuple[int, ...] = meta.shape
        self.dtype = meta.stored.newbyteorder("=")
        size, chunk = dict(zip(meta.axes, meta.shape)), dict(zip(meta.axes, meta.chunks))
        self.access = "plane" if all(chunk[a] >= size[a] for a in "yx") else "chunk"
        self.voxel_size_nm: dict[str, float] | None = meta.voxel_nm
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
        """Voxels [lo, hi) of the array; chunks that were never written are fill_value."""
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
        if key not in m.members:
            return None
        if key in self._cache:
            self._cache[key] = self._cache.pop(key)     # most recently used last
            return self._cache[key]
        data = _member(self._io, f"{m.url}/{key}")
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


def _member(io: IO, url: str) -> bytes:
    """The whole member: by range when the zip stores it, staged when it is deflated."""
    try:
        with io.open(url) as fh:
            return fh.read()
    except NeedsFetch:
        return io.fetch(url).read_bytes()


def _json(io: IO, url: str) -> dict:
    doc = json.loads(_member(io, url))
    if not isinstance(doc, dict):
        raise ValueError(f"{url} is not a JSON object")
    return doc


def _multiscale(attrs: dict, group: str, path: str, rank: int) -> tuple[str, dict[str, float] | None]:
    """(axes, voxel size in nm per spatial axis or None) of the level array `path` of `group`, from the entry of
    the group's `multiscales` whose `datasets` name that directory."""
    multiscales = attrs.get("multiscales")
    for ms in multiscales if isinstance(multiscales, list) else []:
        for ds in ms.get("datasets") or [] if isinstance(ms, dict) else []:
            if isinstance(ds, dict) and str(ds.get("path")).strip("/") == path:
                axes, units = _axes(ms.get("axes"), rank, group)
                return axes, _voxel_nm(axes, units, [ds.get("coordinateTransformations"),
                                                     ms.get("coordinateTransformations")])
    raise ValueError(f"{group}/.zattrs has no multiscales dataset with path {path!r}: the url must be a level "
                     "array directly inside an NGFF image group")


def _axes(listed: object, rank: int, group: str) -> tuple[str, list[str | None]]:
    """Axis letters in storage order and each axis's unit, from a multiscale's `axes` (NGFF 0.4 objects or 0.3
    names)."""
    items = listed if isinstance(listed, list) else []
    names = [str(a.get("name") if isinstance(a, dict) else a).lower() for a in items]
    units = [a.get("unit") if isinstance(a, dict) else None for a in items]
    if (len(names) != rank or any(n not in tuple(AXES) for n in names) or len(set(names)) != rank
            or not {"y", "x"} <= set(names)):
        raise ValueError(f"{group}/.zattrs multiscales axes {listed!r} are not {rank} distinct axes named "
                         "c, z, y, x with y and x among them (an image with a t axis is not read by this snippet)")
    return "".join(names), units


def _voxel_nm(axes: str, units: list[str | None], transforms: list[object]) -> dict[str, float] | None:
    """The level's `scale` (times the multiscale's own, when it has one) in nm for each spatial axis; None when a
    scale is missing or not positive, or a spatial axis has no length unit this snippet knows. Transformations
    of any other type (translation, SpatialData's identity and affine to named coordinate systems) are not read."""
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


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"ome-zarr2-zip-i303 reads one level array, got {len(src.urls)} urls")
    return _ZipZarrArray(io, _Meta(io, src.urls[0]))
