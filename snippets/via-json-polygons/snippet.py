"""VGG Image Annotator (VIA) region JSON as instance labels: the polygon regions of each annotated JPEG,
rasterised on that JPEG's grid, one JPEG per z plane.

The urls are the JSON files (a glob over zip members or an HTTP directory, or listed one by one). A file is a
mapping of entries, a list of entries, or a VIA project holding the mapping under `_via_img_metadata`; an entry is
`{"filename": ..., "regions": [...]}` and a region's `shape_attributes` is `{"name": "polygon", "all_points_x":
[...], "all_points_y": [...]}` in pixels of the image. The JSON does not say how large an image is, so the images
are listed (`params.images`, a glob beside the JSON) and each plane's grid is read from the header of its JPEG;
no pixel of a JPEG is decoded. The planes follow the listing's order, which is the order a raw bound to the same
glob has. Pixel (y, x) holds the 1-based number, in its image's entry, of the polygon enclosing its centre, 0
where none does. `open_array` reads the JSON files, the listing and the first JPEG's header; `read` gets the rest.
"""
import json
import math
import posixpath

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MISSING = ("error", "background", "skip")
DUPLICATES = ("error", "first", "last")
HEAD = 1 << 16      # a JPEG's frame header is almost always within its first 64 KiB
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Polygon = tuple[tuple[float, ...], tuple[float, ...]]       # xs, ys


def _bytes(io: IO, url: str, limit: int | None = None) -> bytes:
    """The file, or its first `limit` bytes when it can be read by range."""
    try:
        with io.open(url) as f:
            return f.read() if limit is None else f.read(limit)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def _orientation(exif: bytes) -> int | None:
    """The Orientation tag (0x0112) of a TIFF-structured Exif block's first IFD, or None."""
    order = {b"II": "little", b"MM": "big"}.get(exif[:2])
    if order is None or len(exif) < 8:
        return None
    ifd = int.from_bytes(exif[4:8], order)
    for k in range(int.from_bytes(exif[ifd:ifd + 2], order)):
        entry = exif[ifd + 2 + 12 * k:ifd + 14 + 12 * k]
        if len(entry) == 12 and int.from_bytes(entry[:2], order) == 0x0112:
            return int.from_bytes(entry[8:10], order)
    return None


def _jpeg_header(data: bytes, url: str) -> tuple[tuple[int, int], int | None] | None:
    """((Y, X), Exif orientation or None) from the segments before the scan, or None when `data` ends before the
    frame header; nothing is decoded."""
    if data[:3] != b"\xff\xd8\xff":
        raise ValueError(f"{url}: not a JPEG file: header={data[:4]!r}")
    pos, orientation = 2, None
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:    # markers without a length
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00" and orientation is None:
            orientation = _orientation(data[pos + 10:pos + 2 + length])
        if marker in SOF and pos + 9 <= len(data):
            return (int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 7:pos + 9], "big")), \
                orientation
        if marker == 0xDA:                              # start of scan: no frame header came before it
            break
        pos += 2 + length
    return None


def _grid(io: IO, url: str) -> tuple[tuple[int, int], int | None]:
    """The JPEG's (Y, X) and Exif orientation: from its first 64 KiB, else from the whole file."""
    head = _bytes(io, url, HEAD)
    found = _jpeg_header(head, url)
    if found is None and len(head) >= HEAD:
        found = _jpeg_header(_bytes(io, url), url)
    if found is None:
        raise ValueError(f"{url}: no JPEG start-of-frame segment found")
    return found


def _polygons(entry: object, where: str) -> tuple[Polygon, ...]:
    """The polygons of one entry's regions, in region order."""
    regions = entry.get("regions") if isinstance(entry, dict) else None
    if isinstance(regions, dict):                       # VIA 1: {"0": region, "1": region}
        keys = sorted(regions, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else 0))
        regions = [regions[k] for k in keys]
    if not isinstance(regions, list):
        raise ValueError(f"{where}: no `regions` list or mapping")
    out = []
    for n, region in enumerate(regions, 1):
        shape = region.get("shape_attributes") if isinstance(region, dict) else None
        kind = shape.get("name") if isinstance(shape, dict) else None
        if kind != "polygon":
            raise ValueError(f"{where}: region {n} is a {kind!r} shape; this snippet reads polygon regions only")
        xs, ys = shape.get("all_points_x"), shape.get("all_points_y")
        if not (isinstance(xs, list) and isinstance(ys, list) and len(xs) == len(ys) >= 3
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                        for v in xs + ys)):
            raise ValueError(f"{where}: region {n} needs all_points_x and all_points_y, the same number (3 or "
                             "more) of finite numbers each")
        out.append((tuple(float(v) for v in xs), tuple(float(v) for v in ys)))
    if len(out) > 65535:
        raise ValueError(f"{where}: {len(out)} regions exceed uint16 ids")
    return tuple(out)


def _entries(data: bytes, url: str) -> list[tuple[str, tuple[Polygon, ...]]]:
    """(file name, polygons) of every entry of one JSON file, in file order."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except ValueError as e:
        raise ValueError(f"{url}: not JSON: {e}") from None
    if isinstance(doc, dict) and isinstance(doc.get("_via_img_metadata"), dict):
        doc = doc["_via_img_metadata"]                  # a VIA project file
    items = list(doc.values()) if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError(f"{url}: neither a mapping nor a list of VIA entries")
    out = []
    for n, entry in enumerate(items, 1):
        name = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError(f"{url}: entry {n} has no `filename`; is this a VIA region file?")
        out.append((name, _polygons(entry, f"{url}: {name!r}")))
    return out


def _merge(io: IO, urls: list[str], duplicates: str) -> dict[str, tuple[Polygon, ...]]:
    """One entry per file name over all JSON files, read in url order and in each file's own order. Entries of
    one name with the same polygons are one entry; with other polygons `duplicates` decides."""
    merged: dict[str, tuple[Polygon, ...]] = {}
    origin: dict[str, str] = {}
    for url in urls:
        for name, polygons in _entries(_bytes(io, url), url):
            if name in merged and merged[name] != polygons:
                if duplicates == "error":
                    raise ValueError(f"{name!r} has different regions in {origin[name]} and in {url}; the files "
                                     "do not say which is meant. Set params.duplicates to first or last (url "
                                     "order, then entry order within a file), or bind fewer files")
                if duplicates == "first":
                    continue
            if name not in merged or duplicates == "last":
                merged[name], origin[name] = polygons, url
    return merged


def _images_url(url: str, images: str) -> str:
    """The glob url of the images of one JSON file: `images` taken from the JSON's folder."""
    prefix, sep, path = url.rpartition("::")
    if not sep:                                         # a plain file url: a glob over its directory's index
        if "/" in images:
            raise ValueError(f"params.images {images!r}: beside a JSON that is not a zip member the images are "
                             "found in its own directory only, by a glob without a folder")
        return f"{url.rpartition('/')[0]}/::{images}"
    path = posixpath.dirname(path)
    parts = [p for p in path.split("/") if p]
    for step in images.split("/")[:-1]:
        if step == "..":
            if not parts:
                raise ValueError(f"params.images {images!r} leaves the top folder of {url}")
            parts.pop()
        elif step not in ("", "."):
            parts.append(step)
    return f"{prefix}::{'/'.join(parts + [images.rpartition('/')[2]])}"


def _fill(xs: tuple[float, ...], ys: tuple[float, ...]) -> tuple[int, int, np.ndarray]:
    """(y0, x0, mask) of the closed polygon: mask[j, i] is True when the centre (x0 + i + 0.5, y0 + j + 0.5) of
    pixel (y0 + j, x0 + i) is inside by the even-odd rule."""
    x0, y0 = math.floor(min(xs)), math.floor(min(ys))
    mask = np.zeros((math.ceil(max(ys)) - y0, math.ceil(max(xs)) - x0), bool)
    cx = np.arange(x0, x0 + mask.shape[1]) + 0.5
    for xa, ya, xb, yb in zip(xs, ys, xs[1:] + xs[:1], ys[1:] + ys[:1]):
        r0, r1 = math.ceil(min(ya, yb) - 0.5), math.ceil(max(ya, yb) - 0.5)     # rows whose centre the edge spans
        if r0 < r1:
            cross = xa + (xb - xa) * (np.arange(r0, r1) + 0.5 - ya) / (yb - ya)
            mask[r0 - y0:r1 - y0] ^= cx[None, :] < cross[:, None]
    return y0, x0, mask


def _paint(shape: tuple[int, int], polygons: tuple[Polygon, ...], where: str) -> np.ndarray:
    """Region n of the entry (from 1) painted as id n; larger polygons first, so where two overlap the smaller is
    on top (region order breaks ties). 0 is background."""
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    masks = []
    for n, (xs, ys) in enumerate(polygons, 1):
        if min(xs) < 0 or min(ys) < 0 or max(xs) > nx or max(ys) > ny:
            raise ValueError(f"{where}: region {n} spans x {min(xs):g}..{max(xs):g}, y {min(ys):g}..{max(ys):g}, "
                             f"outside the {nx}x{ny} image; is this the image the regions were drawn on?")
        masks.append((n, *_fill(xs, ys)))
    for n, y0, x0, mask in sorted(masks, key=lambda m: -int(m[3].sum())):
        out[y0:y0 + mask.shape[0], x0:x0 + mask.shape[1]][mask] = n
    return out


class _Planes:
    """One image per z plane (no z axis when there is one image), uint16 instance ids on the image's grid."""

    def __init__(self, io: IO, planes: list[tuple[str, tuple[Polygon, ...]]]):
        self._io, self._planes = io, planes
        self._last: tuple[int, np.ndarray] | None = None
        self._grid_shape = _grid(io, planes[0][0])[0]
        self.axes = "zyx" if len(planes) > 1 else "yx"
        self.shape = ((len(planes),) if len(planes) > 1 else ()) + self._grid_shape
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # a y/x region costs one image's header and its polygons, drawn whole
        self.voxel_size_nm = None       # neither the VIA JSON nor a JPEG header states a pixel size

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = (region.get("y", slice(None)), region.get("x", slice(None)))
        if "z" not in self.axes:
            return self._plane(0)[inner]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(len(self._planes))[z])[inner]      # IndexError when out of range, like numpy
        planes = [self._plane(k)[inner] for k in range(len(self._planes))[z]]
        if not planes:
            return np.zeros((0,) + np.zeros(self._grid_shape, bool)[inner].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url, polygons = self._planes[k]
        shape, orientation = _grid(self._io, url)
        if shape != self._grid_shape:
            raise ValueError(f"{url}: {shape} (y, x) differs from the first image's {self._grid_shape}; all "
                             "images of one array must agree")
        if polygons and orientation not in (None, 1):
            raise ValueError(f"{url}: Exif orientation {orientation}, so the stored pixels are turned against "
                             "the picture the regions were drawn on; this snippet cannot tell which of the two "
                             "the coordinates mean")
        a = _paint(shape, polygons, url)
        self._last = (k, a)
        return a


def open_array(src: Source, io: IO) -> Array:
    images, count = src.params.get("images", "*.jpg"), src.params.get("count")
    missing, duplicates = src.params.get("missing", "error"), src.params.get("duplicates", "error")
    if missing not in MISSING:
        raise ValueError(f"params.missing must be one of {MISSING}, got {missing!r}")
    if duplicates not in DUPLICATES:
        raise ValueError(f"params.duplicates must be one of {DUPLICATES}, got {duplicates!r}")
    if not isinstance(images, str) or not images.rpartition("/")[2] or "::" in images or images.startswith("/"):
        raise ValueError(f"params.images must be a glob relative to the JSON files' folder, got {images!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if not urls:
        raise ValueError("the urls match no file")
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    globs = {_images_url(u, images) for u in urls}
    if len(globs) != 1:
        raise ValueError(f"the JSON files are in several folders, so params.images names several image sets: "
                         f"{sorted(globs)}; bind one array per folder")
    listed = io.expand(globs.pop())
    by_name = {u.rpartition("/")[2].rpartition("::")[2]: u for u in listed}
    if len(by_name) != len(listed):
        raise ValueError(f"params.images {images!r} matches two files of one name; entries are found by file name")
    entries = _merge(io, urls, duplicates)
    if unknown := sorted(set(entries) - set(by_name)):
        raise ValueError(f"{len(unknown)} annotated file(s) are not among the {len(listed)} images "
                         f"params.images {images!r} matches, e.g. {unknown[0]!r}")
    without = [name for name in by_name if name not in entries]
    if without and missing == "error":
        raise ValueError(f"{len(without)} of the {len(listed)} images have no entry in the JSON, e.g. "
                         f"{without[0]!r}. Set params.missing: skip leaves them out (bind the raw to the "
                         "annotated files only), background makes them all-background planes")
    planes = [(u, entries.get(name, ())) for name, u in by_name.items() if name in entries or missing != "skip"]
    if not planes:
        raise ValueError("no image has an entry")
    if count is not None and (isinstance(count, bool) or count != len(planes)):
        raise ValueError(f"params.count is {count!r}, but the array has {len(planes)} planes "
                         f"({len(listed)} images listed, {len(without)} without an entry, missing: {missing})")
    return _Planes(io, planes)
