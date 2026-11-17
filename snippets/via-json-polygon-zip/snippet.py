"""VGG Image Annotator (VIA) polygon regions from one JSON zip member, filled onto the grid of the JPEG images
that sit beside it in the zip: one z plane per image, in member-name order.

The url names the JSON (`<zip url>::<member>.json`). The planes are the JPEG members of the JSON's own folder
that `params.images` picks, sorted by name; each must have exactly one VIA entry with its file name, and every
entry must name a file of that folder. A pixel is
filled when its centre lies inside a polygon: with the region's 1-based number in its image (`fill: instance`)
or with the 1-based index of its `attribute` value in `classes` (`fill: semantic`). `open_array` reads the zip's
directory, the JSON and the first image's JPEG header; `read` fills the planes it is asked for and reads no
further byte.
"""
import json
from fnmatch import fnmatchcase
from pathlib import Path
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

FILLS = ("instance", "semantic")
HEAD = 1 << 16      # bytes of a JPEG read first for its frame header; the whole file only when that is not enough
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Polygon = tuple[np.ndarray, np.ndarray, str | None]     # x and y of the vertices, the region's class or None


def _source(io: IO, url: str) -> BinaryIO | Path:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member: stage it whole
        return io.fetch(url)


def _read(src: BinaryIO | Path, n: int = -1) -> bytes:
    """The first `n` bytes of the file, all of it by default."""
    if isinstance(src, Path):
        with src.open("rb") as f:
            return f.read(n)
    src.seek(0)
    return src.read(n)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(Y, X) from the start-of-frame segment, or None when `data` ends before it; nothing is decoded."""
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
        elif marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:      # markers without a length
            pos += 2
        elif marker in SOF:
            if pos + 9 > len(data):
                break
            return int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 7:pos + 9], "big")
        elif marker == 0xDA:                            # start of scan: no frame header came before it
            break
        else:
            pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    return None


def _grid(io: IO, url: str) -> tuple[int, int]:
    """(Y, X) of the JPEG at `url`, from its header."""
    src = _source(io, url)
    head = _read(src, HEAD)
    if head[:3] != b"\xff\xd8\xff":
        raise ValueError(f"{url} is not a JPEG (starts {head[:4]!r}); the grid is read from a JPEG header")
    size = _jpeg_size(head) or _jpeg_size(_read(src))
    if size is None or min(size) <= 0:
        raise ValueError(f"{url}: no JPEG start-of-frame segment with an image size found")
    return size


def _entries(doc: object, url: str) -> dict[str, list[dict]]:
    """VIA file name -> its regions, from an annotation export (`{<name><size>: entry}`) or a project file
    (the same mapping under `_via_img_metadata`); VIA 1 stores an entry's regions as a mapping, VIA 2 as a list."""
    if isinstance(doc, dict) and isinstance(doc.get("_via_img_metadata"), dict):
        doc = doc["_via_img_metadata"]
    if not isinstance(doc, dict) or not doc:
        raise ValueError(f"{url}: not a VIA JSON (a mapping of image entries)")
    out: dict[str, list[dict]] = {}
    for key, e in doc.items():
        name = e.get("filename") if isinstance(e, dict) else None
        regions = e.get("regions") if isinstance(e, dict) else None
        if isinstance(regions, dict):
            regions = list(regions.values())
        if not isinstance(name, str) or not isinstance(regions, list):
            raise ValueError(f"{url}: entry {key!r} is not a VIA image entry with 'filename' and 'regions'")
        if name in out:
            raise ValueError(f"{url}: two entries for file name {name!r}")
        out[name] = regions
    return out


def _polygons(regions: list[dict], attribute: str | None, classes: list[str] | None, where: str) -> list[Polygon]:
    """The regions of one image as polygons, in file order, each checked: a polygon of 3 or more finite vertices
    whose `attribute` value (when one is asked for) is in `classes`."""
    out: list[Polygon] = []
    for i, r in enumerate(regions, 1):
        shape = r.get("shape_attributes") if isinstance(r, dict) else None
        if not isinstance(shape, dict) or shape.get("name") != "polygon":
            kind = shape.get("name") if isinstance(shape, dict) else None
            raise ValueError(f"{where} region {i}: shape {kind!r}; this snippet fills polygons only")
        try:
            x = np.asarray(shape.get("all_points_x"), dtype=np.float64)
            y = np.asarray(shape.get("all_points_y"), dtype=np.float64)
        except (TypeError, ValueError):
            x = y = np.empty(0)
        if x.ndim != 1 or x.shape != y.shape or len(x) < 3 or not (np.isfinite(x).all() and np.isfinite(y).all()):
            raise ValueError(f"{where} region {i}: all_points_x / all_points_y are not the 3 or more vertices "
                             "of a polygon")
        cls = None
        if attribute is not None:
            attrs = r.get("region_attributes")
            cls = attrs.get(attribute) if isinstance(attrs, dict) else None
            if not isinstance(cls, str) or cls not in classes:
                raise ValueError(f"{where} region {i}: region attribute {attribute!r} is {cls!r}, which is not in "
                                 f"params.classes {classes}")
        out.append((x, y, cls))
    return out


def _inside(x: np.ndarray, y: np.ndarray, ny: int, nx: int) -> tuple[int, int, np.ndarray]:
    """(row, column, mask) of the pixels of an (ny, nx) grid whose centres lie inside the closed polygon (even-odd
    rule; pixel (i, j) has its centre at x = j + 0.5, y = i + 0.5). The part outside the grid is cut off."""
    r0, r1 = (int(np.clip(v, 0, ny)) for v in (np.floor(y.min()), np.ceil(y.max())))
    c0, c1 = (int(np.clip(v, 0, nx)) for v in (np.floor(x.min()), np.ceil(x.max())))
    if r1 <= r0 or c1 <= c0:
        return r0, c0, np.zeros((0, 0), bool)
    xa, ya, xb, yb = x, y, np.roll(x, -1), np.roll(y, -1)       # edge k runs from vertex k to vertex k + 1
    yc = np.arange(r0, r1)[:, None] + 0.5                       # pixel-centre rows
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = xa + (yc - ya) * (xb - xa) / (yb - ya)             # where each edge meets each row
    rows, edges = np.nonzero((ya <= yc) != (yb <= yc))          # the edges that cross the row (never horizontal)
    first = np.clip(np.ceil(xc[rows, edges] - 0.5), c0, c1).astype(np.int64) - c0   # first centre at or right of it
    flips = np.zeros((r1 - r0, c1 - c0 + 1), np.int32)
    np.add.at(flips, (rows, first), 1)
    return r0, c0, (np.cumsum(flips[:, :-1], axis=1) % 2).astype(bool)   # inside: an odd number of edges to the left


class _Planes:
    """One (Y, X) uint16 plane per image, axes [z]yx, filled from that image's regions when it is read."""

    def __init__(self, names: list[str], regions: list[list[dict]], grid: tuple[int, int], fill: str,
                 attribute: str | None, classes: list[str] | None):
        self._names, self._regions, self._grid = names, regions, grid
        self._fill, self._attribute, self._classes = fill, attribute, classes
        self._last: tuple[int, np.ndarray] | None = None        # the last plane filled
        many = len(names) > 1
        self.axes = "zyx" if many else "yx"
        self.shape = ((len(names),) if many else ()) + grid
        self.dtype = np.dtype("uint16")
        self.voxel_size_nm = None       # a VIA file has pixel coordinates only
        self.access = "plane"           # a y/x region costs filling its image's polygons; no bytes are fetched

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0)[y, x]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(len(self._names))[z])[y, x]     # IndexError when out of range, like numpy
        planes = [self._plane(k)[y, x] for k in range(len(self._names))[z]]
        if planes:
            return np.stack(planes)
        return np.empty((0, *np.empty(self._grid, self.dtype)[y, x].shape), self.dtype)     # an empty z range

    def _plane(self, k: int) -> np.ndarray:
        """Image k's polygons, largest first, so the smaller of two overlapping regions is on top (file order
        breaks ties). 0 is background."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        polygons = _polygons(self._regions[k], self._attribute, self._classes, self._names[k])
        if len(polygons) > 65535:
            raise ValueError(f"{self._names[k]}: {len(polygons)} regions exceed uint16 ids")
        masks = [_inside(px, py, *self._grid) for px, py, _ in polygons]
        out = np.zeros(self._grid, np.uint16)
        for i in sorted(range(len(masks)), key=lambda i: -int(masks[i][2].sum())):
            r0, c0, mask = masks[i]
            value = i + 1 if self._fill == "instance" else self._classes.index(polygons[i][2]) + 1
            out[r0:r0 + mask.shape[0], c0:c0 + mask.shape[1]][mask] = value
        self._last = (k, out)
        return out


def _member_url(src: Source, io: IO) -> tuple[str, str]:
    """(zip url, member name) of the one JSON the binding names."""
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(urls) != 1:
        raise ValueError(f"the urls name {len(urls)} files; one array reads one VIA JSON, bind each as its own label")
    url = urls[0]
    i = url.find("::", max(url.find("/", url.find("://") + 3), 0))
    if i < 0:
        raise ValueError(f"{url}: the VIA JSON must be a zip member, '<zip url>::<member>.json'; its images are "
                         "looked up beside it in the zip")
    return url[:i], url[i + 2:]


def open_array(src: Source, io: IO) -> Array:
    fill, attribute, classes = src.params.get("fill"), src.params.get("attribute"), src.params.get("classes")
    images = src.params.get("images", "*.jpg")
    if fill not in FILLS:
        raise ValueError(f"params.fill must be one of {FILLS}, got {fill!r}")
    if (attribute is None) != (classes is None) or (fill == "semantic" and classes is None):
        raise ValueError("params.attribute and params.classes go together, and params.fill 'semantic' needs both")
    if attribute is not None and (not isinstance(attribute, str) or not attribute):
        raise ValueError(f"params.attribute must be a region attribute name, got {attribute!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    if not isinstance(images, str) or not images or "/" in images:
        raise ValueError(f"params.images must be a file name pattern such as '*.jpg', got {images!r}")
    zip_url, member = _member_url(src, io)
    folder = member[:member.rfind("/") + 1]
    beside = {n[len(folder):] for n in io.list(zip_url) if n.startswith(folder) and "/" not in n[len(folder):]}
    names = sorted(n for n in beside if n and fnmatchcase(n, images))
    if not names:
        raise ValueError(f"no member of {zip_url} in {folder!r} matches {images!r}")
    url = f"{zip_url}::{member}"
    try:
        entries = _entries(json.loads(_read(_source(io, url))), url)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{url} is not JSON: {e}") from None
    unannotated, unknown = sorted(set(names) - set(entries)), sorted(set(entries) - beside)
    if unannotated or unknown:
        raise ValueError(f"{url}: {len(unannotated)} of the {images!r} files of {folder!r} have no entry "
                         f"{unannotated[:3]}, and {len(unknown)} entries name no file of that folder {unknown[:3]}")
    grid = _grid(io, f"{zip_url}::{folder}{names[0]}")
    return _Planes(names, [entries[n] for n in names], grid, fill, attribute, classes)
