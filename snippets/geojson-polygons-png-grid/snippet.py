"""GeoJSON polygon annotations (a QuPath export: one FeatureCollection per image, Polygon or MultiPolygon features
in that image's pixel coordinates), chosen by glob (zip members, an HTTP directory) or listed one by one, stacked
along z, one file per plane.

Each file becomes a label plane on the grid of its PNG image, the file `params.image` names beside it, whose size
is read from the PNG header. A pixel belongs to a feature when its centre lies inside the outline (even-odd rule,
so the inner rings of a polygon are holes); pixel (row r, column c) covers x in [c, c+1) and y in [r, r+1), as in
QuPath. Without `classes` a feature is painted with its 1-based number in its file (instance ids); with `classes`
it is painted with the 1-based index of its `properties.classification.name` in that list (semantic ids). 0 is
background, and a file without features is an all-background plane. `open_array` reads the first file and its
PNG's header; `read` gets the others, one GeoJSON (and one PNG header) per z plane.
"""
import json

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
Rings = list[np.ndarray]                    # the rings of one feature, each (n, 2) float64 as x, y
Feature = tuple[int, str | None, Rings]     # 1-based number in the file, class name, rings


def _bytes(io: IO, url: str) -> bytes:
    try:
        with io.open(url) as f:
            return f.read()
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def _png_size(io: IO, url: str) -> tuple[int, int]:
    """(height, width) from a PNG's IHDR, the first chunk: 33 bytes by range, or of the staged file."""
    try:
        with io.open(url) as f:
            head = f.read(33)
    except NeedsFetch:
        with open(io.fetch(url), "rb") as f:
            head = f.read(33)
    if len(head) < 24 or head[:8] != PNG or head[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={head[:8]!r}; the grid of a label plane is read from "
                         "the PNG header of its image")
    return int.from_bytes(head[20:24], "big"), int.from_bytes(head[16:20], "big")


def _image_url(url: str, pattern: str) -> str:
    """The image of the GeoJSON at `url`: `pattern`, relative to that file's directory, with `*` replaced by its
    name without directory and extension. `..` steps up, within the zip or below the host."""
    container, sep, path = url.rpartition("::")
    if not sep:                                             # a plain url: keep scheme://host out of the walk
        scheme, mark, rest = path.partition("://")
        host, _, path = rest.partition("/")
        container = f"{scheme}{mark}{host}/"
    *parts, name = path.split("/")
    for step in pattern.replace("*", name.rsplit(".", 1)[0]).split("/"):
        if step == "..":
            if not parts:
                raise ValueError(f"params.image {pattern!r} leaves the container of {url}")
            parts.pop()
        elif step != ".":
            parts.append(step)
    return container + sep + "/".join(parts)


def _features(data: bytes, url: str) -> list[Feature]:
    """The features of one GeoJSON FeatureCollection with their rings; anything but (Multi)Polygon raises."""
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise ValueError(f"{url}: not JSON ({e}); header={data[:8]!r}") from None
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" or not isinstance(doc.get("features"), list):
        raise ValueError(f"{url}: not a GeoJSON FeatureCollection with a features list")
    out: list[Feature] = []
    for number, f in enumerate(doc["features"], 1):
        geom = f.get("geometry") if isinstance(f, dict) else None
        kind = geom.get("type") if isinstance(geom, dict) else None
        if kind not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"{url}: feature {number} has geometry {kind!r}; only Polygon and MultiPolygon "
                             "enclose pixels and are read")
        polygons = [geom.get("coordinates")] if kind == "Polygon" else geom.get("coordinates")
        try:
            rings = [np.asarray(ring, dtype=np.float64)[:, :2] for polygon in polygons for ring in polygon]
            good = bool(rings) and all(r.ndim == 2 and r.shape[1] == 2 and len(r) >= 3 for r in rings)
        except (TypeError, ValueError, IndexError):
            good = False
        if not good or not all(np.isfinite(r).all() for r in rings):
            raise ValueError(f"{url}: feature {number}: coordinates are not rings of at least 3 finite x, y positions")
        props = f.get("properties")
        cls = props.get("classification") if isinstance(props, dict) else None
        name = cls.get("name") if isinstance(cls, dict) else cls       # QuPath: {"name": ..., "color": ...}
        out.append((number, name if isinstance(name, str) else None, rings))
    return out


def _fill(rings: Rings, height: int, width: int) -> tuple[int, int, np.ndarray]:
    """(row0, col0, mask) of the pixels whose centres are inside the rings by the even-odd rule: per pixel row, the
    crossings of the row's centre line with every edge, sorted, bound the spans that are inside."""
    xa = np.concatenate([r[:, 0] for r in rings])
    ya = np.concatenate([r[:, 1] for r in rings])
    xb = np.concatenate([np.roll(r[:, 0], -1) for r in rings])      # each ring closed, whether or not the file
    yb = np.concatenate([np.roll(r[:, 1], -1) for r in rings])      # repeats its first position at the end
    flip = ya > yb
    xa, xb, ya, yb = np.where(flip, xb, xa), np.where(flip, xa, xb), np.where(flip, yb, ya), np.where(flip, ya, yb)
    row0, col0 = int(np.clip(np.floor(ya.min()), 0, height)), int(np.clip(np.floor(xa.min()), 0, width))
    rows = int(np.clip(np.ceil(yb.max()), row0, height)) - row0
    cols = int(np.clip(np.ceil(max(xa.max(), xb.max())), col0, width)) - col0
    # an edge crosses the centre line of row r when ya <= r + 0.5 < yb (half-open, so a vertex counts once)
    first = np.clip(np.ceil(ya - 0.5), row0, row0 + rows).astype(np.int64)
    last = np.clip(np.ceil(yb - 0.5), row0, row0 + rows).astype(np.int64)
    n = last - first
    edge = np.repeat(np.arange(len(n)), n)
    r = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n) + first[edge]
    with np.errstate(divide="ignore", invalid="ignore"):            # horizontal edges have n = 0 and are not used
        slope = (xb - xa) / (yb - ya)
    x = xa[edge] + (r + 0.5 - ya[edge]) * slope[edge]
    order = np.lexsort((x, r))
    r, x = r[order], x[order]                                       # every row holds an even number of crossings
    start = np.clip(np.ceil(x[0::2] - 0.5), col0, col0 + cols).astype(np.int64) - col0    # centre c + 0.5 >= x
    stop = np.clip(np.ceil(x[1::2] - 0.5), col0, col0 + cols).astype(np.int64) - col0     # centre c + 0.5 < x
    edges = np.zeros((rows, cols + 1), dtype=np.int32)
    np.add.at(edges, (r[0::2] - row0, start), 1)
    np.add.at(edges, (r[0::2] - row0, stop), -1)
    return row0, col0, np.cumsum(edges, axis=1)[:, :cols] > 0


class _Polygons:
    def __init__(self, io: IO, urls: list[str], image: str, classes: list[str] | None):
        self._io, self._urls, self._image, self._classes = io, urls, image, classes
        self._features_0 = _features(_bytes(io, urls[0]), urls[0])  # the first file is checked at open
        self._plane_shape = _png_size(io, _image_url(urls[0], image))
        self._last: tuple[int, np.ndarray] | None = None            # the last plane painted
        self.axes = ("z" if len(urls) > 1 else "") + "yx"
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + self._plane_shape
        self.dtype = np.dtype(np.uint16)
        self.voxel_size_nm = None
        self.access = "plane"           # one GeoJSON per z plane: any region of it costs the whole file

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in "yx")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._urls))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._urls))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """File k painted on its PNG's grid, which must be the first file's."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        features = self._features_0 if k == 0 else _features(_bytes(self._io, url), url)
        height, width = size = _png_size(self._io, _image_url(url, self._image))
        if size != self._plane_shape:
            raise ValueError(f"{_image_url(url, self._image)}: {size} (y, x) differs from the first file's image, "
                             f"{self._plane_shape}; all planes of one array must have one grid")
        if len(features) > np.iinfo(self.dtype).max:
            raise ValueError(f"{url}: {len(features)} features do not fit uint16 ids")
        masks: list[tuple[int, int, int, int, np.ndarray]] = []
        for number, name, rings in features:
            lo, hi = np.min([r.min(0) for r in rings], 0), np.max([r.max(0) for r in rings], 0)
            if lo.min() < 0 or hi[0] > width or hi[1] > height:
                raise ValueError(f"{url}: feature {number} spans x {lo[0]:g}..{hi[0]:g}, y {lo[1]:g}..{hi[1]:g}, "
                                 f"outside its image's {width} x {height} pixels: the coordinates are not this "
                                 "image's pixels")
            value = number
            if self._classes is not None:
                if name not in self._classes:
                    raise ValueError(f"{url}: feature {number} has classification {name!r}, which is not in "
                                     f"params.classes {self._classes}")
                value = self._classes.index(name) + 1
            row0, col0, mask = _fill(rings, height, width)
            masks.append((-int(mask.sum()), value, row0, col0, mask))
        plane = np.zeros(size, dtype=self.dtype)
        for _, value, row0, col0, mask in sorted(masks, key=lambda m: m[0]):    # largest first: where features
            plane[row0:row0 + mask.shape[0], col0:col0 + mask.shape[1]][mask] = value   # overlap, the smaller is on top
        self._last = (k, plane)
        return plane


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    image, classes = src.params.get("image"), src.params.get("classes")
    if not isinstance(image, str) or image.count("*") != 1:
        raise ValueError("params.image is required: the PNG of a GeoJSON file, relative to that file's directory, "
                         f"with one * for the file's name without extension (e.g. '../patches/*.png'); got {image!r}")
    if classes is not None and (not classes or not all(isinstance(c, str) for c in classes)
                                or len(set(classes)) != len(classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    return _Polygons(io, _expand(src.urls, io), image, classes)
