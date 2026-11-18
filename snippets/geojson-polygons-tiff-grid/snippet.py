"""The polygons of one GeoJSON file (a QuPath export: a FeatureCollection, a bare list of features, or one
feature) filled onto the pixel grid of the TIFF they were drawn on, as yx instance ids or a binary mask.

The urls are the GeoJSON and its TIFF. Only the TIFF's first page header is read, for the shape and the pixel
size; the GeoJSON is fetched whole on the first read. Coordinates are pixels of that image, an integer coordinate
being a pixel corner, and a pixel belongs to a feature when its centre lies inside the feature's polygon
(even-odd rule over all its rings, so holes and MultiPolygons work). See README.md."""
import json
from collections import Counter
from collections.abc import Iterator
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form
JSON_ENDINGS = (".geojson", ".json")
BATCH = 1 << 22                 # pixels (summed bounding boxes) of the features filled in one numpy pass
OUTSIDE = 1.0                   # pixels a polygon may reach beyond the grid before the pairing is refused


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


def _ramp(first: np.ndarray, count: np.ndarray) -> np.ndarray:
    """first[0], first[0]+1, ... (count[0] values), first[1], ... : every run of integers, concatenated."""
    total = int(count.sum())
    return np.repeat(first - (np.cumsum(count) - count), count) + np.arange(total, dtype=np.int64)


def _features(text: str, name: str) -> Iterator[dict]:
    """The features of a GeoJSON text, decoded one at a time so that a file of 100 MB never becomes one tree of
    Python objects: the "features" of a top-level object, the elements of a top-level list, or a lone Feature."""
    decoder, n = json.JSONDecoder(), len(text)

    def skip(i: int) -> int:
        while i < n and text[i] in " \t\r\n﻿":
            i += 1
        return i

    def elements(i: int) -> Iterator[dict]:
        """The values of the array opening at text[i]; returns the position after its closing bracket."""
        i = skip(i + 1)
        while text[i:i + 1] != "]":
            value, i = decoder.raw_decode(text, i)
            yield value
            i = skip(i)
            if text[i:i + 1] == ",":
                i = skip(i + 1)
            elif text[i:i + 1] != "]":
                raise ValueError(f"{name}: expected ',' or ']' at character {i}")
        return i + 1

    try:
        i = skip(0)
        if text[i:i + 1] == "[":
            yield from elements(i)
            return
        if text[i:i + 1] != "{":
            raise ValueError(f"{name}: not GeoJSON (starts with {text[i:i + 20]!r})")
        top, found, i = {}, False, skip(i + 1)
        while text[i:i + 1] != "}":
            key, i = decoder.raw_decode(text, i)
            i = skip(i)
            if not isinstance(key, str) or text[i:i + 1] != ":":
                raise ValueError(f"{name}: expected a key and ':' at character {i}")
            i = skip(i + 1)
            if key == "features" and text[i:i + 1] == "[":
                found = True
                i = yield from elements(i)
            else:
                top[key], i = decoder.raw_decode(text, i)
            i = skip(i)
            if text[i:i + 1] == ",":
                i = skip(i + 1)
            elif text[i:i + 1] != "}":
                raise ValueError(f"{name}: expected ',' or '}}' at character {i}")
        if not found:
            if top.get("type") != "Feature":
                raise ValueError(f"{name}: a GeoJSON {top.get('type')!r} object, not a FeatureCollection, a list "
                                 "of features or a Feature")
            yield top
    except json.JSONDecodeError as e:
        raise ValueError(f"{name}: not valid JSON at character {e.pos}: {e.msg}") from None


def _rings(geometry: object, where: str) -> list[np.ndarray]:
    """Every ring of a Polygon or MultiPolygon as an (n, 2) array of x, y."""
    kind = geometry.get("type") if isinstance(geometry, dict) else None
    if kind == "Polygon":
        polygons = [geometry.get("coordinates")]
    elif kind == "MultiPolygon":
        polygons = geometry.get("coordinates")
    else:
        raise ValueError(f"{where} is a {kind!r} geometry, which encloses no pixels; only Polygon and "
                         "MultiPolygon are filled (leave such features out by their classification)")
    rings = []
    for polygon in polygons or []:
        for ring in polygon or []:
            xy = np.asarray(ring, dtype=np.float64)
            if xy.ndim != 2 or xy.shape[1] < 2 or len(xy) < 3 or not np.isfinite(xy[:, :2]).all():
                raise ValueError(f"{where} has a ring that is not 3 or more finite [x, y] positions")
            rings.append(xy[:, :2])
    return rings


class _Polygons:
    """yx. Instance form: pixel = the 1-based position in the file of the feature whose polygon encloses the
    pixel's centre (the later feature where two overlap), 0 where none does. Binary form: 1 inside any."""

    def __init__(self, io: IO, url: str, shape: tuple[int, int], voxel_size_nm: dict[str, float] | None,
                 member: str, binary: bool, keep: list | None, drop: list, empty: bool, empty_ok: bool):
        self._io, self._url, self._member, self._binary = io, url, member, binary
        self._keep, self._drop, self._empty, self._empty_ok = keep, drop, empty, empty_ok
        self._loaded = False
        self.axes, self.shape = "yx", shape
        self.dtype = np.dtype("uint8" if binary else "uint32")
        self.voxel_size_nm = voxel_size_nm
        self.access = "plane"           # the whole GeoJSON is read on the first read, whatever the region

    def _load(self) -> None:
        """Parse the file into the non-horizontal edges of the kept features, feature after feature."""
        name = self._url.rsplit("/", 1)[-1]
        text = "" if self._empty else self._io.fetch(self._url).read_bytes().decode("utf-8-sig")
        if not text.strip() and not self._empty_ok:
            raise ValueError(f"{name} is empty: it states no objects. Leave it out of the binding, or set "
                             "params.empty_ok: true to read it as a label with nothing in it")
        ids, starts, boxes, edges, total = [], [], [], [], 0
        seen, kept, without = Counter(), 0, 0
        for number, feature in enumerate(_features(text, name) if text.strip() else (), 1):
            where = f"{name}: feature {number}"
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                raise ValueError(f"{where} is not a GeoJSON Feature")
            cls = (feature.get("properties") or {}).get("classification")
            cls = cls.get("name") if isinstance(cls, dict) else cls
            seen[cls] += 1
            if (self._keep is not None and cls not in self._keep) or cls in self._drop:
                continue
            kept += 1
            if feature.get(self._member) is None:
                without += 1
                continue
            rings = _rings(feature[self._member], f"{where} {self._member}")
            if not rings:
                without += 1
                continue
            a = np.concatenate(rings)
            b = np.concatenate([np.roll(r, -1, axis=0) for r in rings])        # every ring closed onto itself
            slanted = a[:, 1] != b[:, 1]                                       # a horizontal edge crosses no row
            ids.append(number)
            starts.append(total)
            boxes.append((a[:, 1].min(), a[:, 1].max(), a[:, 0].min(), a[:, 0].max()))
            edges.append(np.concatenate([a[slanted], b[slanted]], axis=1))
            total += int(slanted.sum())
        classes = ", ".join(f"{k!r} ({v})" for k, v in sorted(seen.items(), key=lambda kv: str(kv[0])))
        if seen and not kept:
            raise ValueError(f"{name}: params.keep_classes / drop_classes leave none of its {sum(seen.values())} "
                             f"features; their classifications are {classes}")
        if kept and not ids:
            raise ValueError(f"{name}: none of the {kept} kept features has a {self._member!r} polygon")
        self._ids = np.array(ids, dtype=np.int64)
        self._starts = np.array(starts + [total], dtype=np.int64)
        self._boxes = np.array(boxes, dtype=np.float64).reshape(-1, 4)          # y min, y max, x min, x max
        self._edges = np.concatenate(edges) if edges else np.zeros((0, 4))      # xa, ya, xb, yb
        if len(ids):
            lo, hi, (ny, nx) = self._boxes.min(axis=0), self._boxes.max(axis=0), self.shape
            if lo[0] < -OUTSIDE or lo[2] < -OUTSIDE or hi[1] > ny + OUTSIDE or hi[3] > nx + OUTSIDE:
                raise ValueError(f"{name}: the polygons span x {lo[2]:g}..{hi[3]:g}, y {lo[0]:g}..{hi[1]:g}, "
                                 f"outside the {nx} x {ny} pixels of the paired image: not its pixel frame")
        self._loaded = True

    def _fill(self, out: np.ndarray, feats: np.ndarray, y0: int, x0: int) -> None:
        """Fill the features `feats` (ascending) into `out`, whose corner is image pixel (y0, x0)."""
        (ny, nx), count = out.shape, self._starts[feats + 1] - self._starts[feats]
        owner = np.repeat(np.arange(len(feats)), count)
        xa, ya, xb, yb = self._edges[_ramp(self._starts[feats], count)].T
        # an edge crosses the rows whose centre r + 0.5 lies in [min y, max y): each vertex counts once
        first = np.maximum(np.ceil(np.minimum(ya, yb) - 0.5).astype(np.int64), y0)
        rows = np.maximum(np.minimum(np.ceil(np.maximum(ya, yb) - 0.5).astype(np.int64), y0 + ny) - first, 0)
        edge, row = np.repeat(np.arange(len(first)), rows), _ramp(first, rows)
        cross = xa[edge] + (row + 0.5 - ya[edge]) * (xb[edge] - xa[edge]) / (yb[edge] - ya[edge])
        # the first column whose centre is at or right of the crossing, kept within the region
        col = np.clip(np.ceil(cross - 0.5), x0, x0 + nx).astype(np.int64)
        owner = owner[edge]
        order = np.lexsort((col, row, owner))
        a, b = order[0::2], order[1::2]             # per feature and row, crossings pair up into inside spans
        if len(order) % 2 or (row[a] != row[b]).any() or (owner[a] != owner[b]).any():
            raise ValueError("a polygon has an odd number of row crossings")       # closed rings never do
        length = col[b] - col[a]
        a, length = a[length > 0], length[length > 0]
        pixels = _ramp((row[a] - y0) * nx + col[a] - x0, length)
        # spans are in feature order and a repeated index keeps its last value: the later feature wins an overlap
        out.reshape(-1)[pixels] = 1 if self._binary else np.repeat(self._ids[feats][owner[a]], length)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if not self._loaded:
            self._load()
        y0, y1, yi = _span(region.get("y", slice(None)), self.shape[0])
        x0, x1, xi = _span(region.get("x", slice(None)), self.shape[1])
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        if out.size and len(self._ids):
            ymin, ymax, xmin, xmax = self._boxes.T
            feats = np.flatnonzero((ymax > y0) & (ymin < y1) & (xmax > x0) & (xmin < x1))
            area = ((np.minimum(ymax[feats], y1) - np.maximum(ymin[feats], y0) + 1)
                    * (np.minimum(xmax[feats], x1) - np.maximum(xmin[feats], x0) + 1))
            cuts = np.flatnonzero(np.diff(np.cumsum(area) // BATCH)) + 1
            for batch in np.split(feats, cuts):
                if len(batch):
                    self._fill(out, batch, y0, x0)
        return np.array(out[yi][..., xi])


def _open(io: IO, url: str) -> BinaryIO:
    try:
        return io.open(url)
    except NeedsFetch:          # a server without Range: stage the whole file
        return open(io.fetch(url), "rb")


def _grid(io: IO, url: str) -> tuple[tuple[int, int], dict[str, float] | None]:
    """(y, x) shape and pixel size of the TIFF's first page: OME PhysicalSize*, else ImageJ unit + resolution."""
    with _open(io, url) as fh, tifffile.TiffFile(fh) as tif:
        page, out = tif.pages[0], {}
        if tif.is_ome:
            image = tifffile.xml2dict(tif.ome_metadata)["OME"]["Image"]
            px = (image[0] if isinstance(image, list) else image)["Pixels"]
            for a in "yx":
                v, unit = px.get(f"PhysicalSize{a.upper()}"), px.get(f"PhysicalSize{a.upper()}Unit", "µm")
                if v and unit in NM:
                    out[a] = float(v) * NM[unit]
        elif tif.is_imagej and tif.imagej_metadata.get("unit") in NM:
            for a, tag in (("y", "YResolution"), ("x", "XResolution")):
                num, den = page.tags[tag].value if tag in page.tags else (0, 0)
                if num:
                    out[a] = den / num * NM[tif.imagej_metadata["unit"]]
        return (int(page.imagelength), int(page.imagewidth)), out or None


def _names(params: dict, key: str) -> list | None:
    value = params.get(key)
    if value is not None and not (isinstance(value, list) and value
                                  and all(v is None or isinstance(v, str) for v in value)):
        raise ValueError(f"params.{key} must be a list of classification names (null for none), got {value!r}")
    return value


def open_array(src: Source, io: IO) -> Array:
    params = src.params
    for key in ("binary", "empty_ok"):
        if not isinstance(params.get(key, False), bool):
            raise ValueError(f"params.{key} must be true or false, got {params[key]!r}")
    member = params.get("member", "geometry")
    if not isinstance(member, str) or not member:
        raise ValueError(f"params.member must name the feature key that holds the polygons, got {member!r}")
    keep, drop = _names(params, "keep_classes"), _names(params, "drop_classes") or []
    urls = [u for url in src.urls for u in io.expand(url)]
    jsons = [u for u in urls if u.split("?", 1)[0].split("#", 1)[0].lower().endswith(JSON_ENDINGS)]
    rest = [u for u in urls if u not in jsons]
    if len(jsons) != 1 or len(rest) != 1:
        raise ValueError("urls must be one GeoJSON (.geojson or .json) and the one TIFF it was drawn on, got "
                         f"{len(jsons)} and {len(rest)}")
    empty = False
    try:                        # a file of 0 bytes is told from its size, before anything is fetched
        with io.open(jsons[0]) as fh:
            fh.seek(0, 2)
            empty = fh.tell() == 0
    except NeedsFetch:
        pass
    empty_ok = bool(params.get("empty_ok", False))
    if empty and not empty_ok:
        raise ValueError(f"{jsons[0]} is a file of 0 bytes: it states no objects. Leave it out of the binding, "
                         "or set params.empty_ok: true to read it as a label with nothing in it")
    shape, voxel_size_nm = _grid(io, rest[0])
    return _Polygons(io, jsons[0], shape, voxel_size_nm, member, bool(params.get("binary", False)), keep, drop,
                     empty, empty_ok)
