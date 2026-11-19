"""A set of VIA (VGG Image Annotator) CSV box annotations, one CSV per image, chosen by glob from zip members or
an HTTP directory (or listed one by one), stacked along z, one file per plane.

Each CSV becomes a label plane on the grid of the one PNG in its own folder (the CSV's `filename` column is not
used to find it), with every `rect` region filled: by its 1-based row number in the file (`boxes: instance`)
or by the 1-based index of its class in `classes` (`boxes: semantic`). The class is the value of the region
attribute `class_key`. A CSV without regions is an all-background plane. `open_array` reads the first CSV and
the header of its PNG; `read` fetches the CSVs and PNG headers of the planes it needs. No pixel is decoded.
"""
import csv
import json
import math
import re
from io import StringIO
from pathlib import Path
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
COLUMNS = ("filename", "region_count", "region_shape_attributes", "region_attributes")
PNG = b"\x89PNG\r\n\x1a\n"
# a bare word as a JSON value, {"particle_class":immature}: some exports lost the quotes
BARE = re.compile(r"(:\s*)(?!(?:true|false|null)\b)([A-Za-z_][\w .-]*?)(\s*[,}])")

Region = tuple[str, int, int, int, int]     # class, x0, y0, x1, y1 as drawn (far corner exclusive), not clipped


def _source(io: IO, url: str) -> BinaryIO | Path:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url)


def _head(io: IO, url: str, n: int) -> bytes:
    src = _source(io, url)
    with (src.open("rb") if isinstance(src, Path) else src) as f:
        return f.read(n)


def _all_bytes(io: IO, url: str) -> bytes:
    src = _source(io, url)
    if isinstance(src, Path):
        return src.read_bytes()
    with src:
        return src.read()


def _split(url: str) -> tuple[str, str | None]:
    """(file or zip url, zip member or None): `::` counts after the host only."""
    path = url.find("/", url.find("://") + 3)
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


def _grid_url(io: IO, url: str, ext: str) -> str:
    """The one file with extension `ext` in the folder of the CSV `url` (not in its subfolders)."""
    container, member = _split(url)
    if member is None:
        folder = url.rpartition("/")[0] + "/"
        hits = [folder + n for n in io.list(folder) if "/" not in n and n.lower().endswith("." + ext)]
    else:
        folder = member.rpartition("/")[0]
        folder = folder + "/" if folder else ""
        hits = [f"{container}::{n}" for n in io.list(container)
                if n.startswith(folder) and "/" not in n[len(folder):] and n.lower().endswith("." + ext)]
    if len(hits) != 1:
        raise ValueError(f"{url}: its folder holds {len(hits)} .{ext} files {sorted(hits)[:4]}, not the one image "
                         "the boxes are drawn on")
    return hits[0]


def _png_shape(io: IO, url: str) -> tuple[int, int]:
    """(Y, X) from the IHDR chunk, which follows the 8-byte signature."""
    head = _head(io, url, 24)
    if len(head) < 24 or head[:8] != PNG or head[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={head[:8]!r}")
    nx, ny = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if nx <= 0 or ny <= 0:
        raise ValueError(f"{url}: IHDR {nx}x{ny} is not an image size")
    return ny, nx


def _json(text: str, where: str) -> dict:
    """A JSON object; bare-word values are taken as strings."""
    try:
        value = json.loads(text)
    except ValueError:
        try:
            value = json.loads(BARE.sub(r'\1"\2"\3', text))
        except ValueError:
            raise ValueError(f"{where}: not a JSON object: {text[:120]!r}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{where}: not a JSON object: {text[:120]!r}")
    return value


def _regions(data: bytes, url: str, class_key: str) -> list[Region]:
    """The rect regions of one image's VIA CSV, in row order."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not a UTF-8 text file: header={data[:8]!r}") from None
    rows = [r for r in csv.reader(StringIO(text, newline="")) if any(f.strip() for f in r)]
    header = [h.strip() for h in rows[0]] if rows else []
    if header:
        header[0] = header[0].lstrip("#").strip()       # VIA 1 wrote the header line as a comment: #filename,...
    missing = [c for c in COLUMNS if header.count(c) != 1]
    if missing:
        raise ValueError(f"{url}: not a VIA region CSV: header {header[:8]} lacks (or repeats) {missing}")
    col = {c: header.index(c) for c in COLUMNS}
    regions: list[Region] = []
    images, counts = set(), set()
    for n, row in enumerate(rows[1:], 2):
        where = f"{url} row {n}"
        if len(row) != len(header):
            raise ValueError(f"{where}: {len(row)} fields under a header of {len(header)}")
        images.add(row[col["filename"]])
        counts.add(row[col["region_count"]].strip())
        shape = _json(row[col["region_shape_attributes"]], where)
        if not shape:           # VIA's row for an image without regions
            continue
        if shape.get("name") != "rect":
            raise ValueError(f"{where}: region shape {shape.get('name')!r}; this snippet paints rect regions only")
        try:
            x, y, w, h = (shape[k] for k in ("x", "y", "width", "height"))
            ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y, w, h))
        except KeyError:
            ok = False
        if not ok or w <= 0 or h <= 0:
            raise ValueError(f"{where}: rect {shape} needs numbers x, y and positive width, height")
        cls = _json(row[col["region_attributes"]], where).get(class_key)
        if not isinstance(cls, str) or not cls.strip():
            raise ValueError(f"{where}: region attribute {class_key!r} is missing or not a string: "
                             f"{row[col['region_attributes']][:120]!r}")
        # a fractional corner moves outwards: every pixel the box touches
        regions.append((cls.strip(), math.floor(x), math.floor(y), math.ceil(x + w), math.ceil(y + h)))
    if len(images) > 1:
        raise ValueError(f"{url}: rows for {len(images)} images {sorted(images)[:3]}; this snippet reads one CSV "
                         "per image")
    if counts and counts != {str(len(regions))}:
        raise ValueError(f"{url}: region_count says {sorted(counts)} but the file has {len(regions)} region rows")
    if len(regions) > 65535:
        raise ValueError(f"{url}: {len(regions)} regions exceed uint16 ids")
    return regions


def _paint(shape: tuple[int, int], regions: list[Region], mode: str, classes: list[str], overhang: int,
           url: str) -> np.ndarray:
    """Boxes filled over pixels [y0, y1) x [x0, x1), clipped to the grid, largest (as drawn) first, so the
    smaller of two overlapping boxes is on top (row order breaks ties). instance: 1-based row number among the
    file's regions; semantic: 1-based index of the class in `classes`. 0 is background."""
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    order = sorted(range(len(regions)),
                   key=lambda i: -(regions[i][3] - regions[i][1]) * (regions[i][4] - regions[i][2]))
    for i in order:
        cls, x0, y0, x1, y1 = regions[i]
        if cls not in classes:
            raise ValueError(f"{url}: region {i + 1} has class {cls!r}, which is not in params.classes {classes}")
        beyond = max(-x0, -y0, x1 - nx, y1 - ny)
        if beyond > overhang:
            raise ValueError(f"{url}: region {i + 1} x {x0}..{x1} y {y0}..{y1} reaches {beyond} px beyond the "
                             f"{nx}x{ny} image; params.overhang allows {overhang}")
        out[max(y0, 0):min(y1, ny), max(x0, 0):min(x1, nx)] = i + 1 if mode == "instance" else classes.index(cls) + 1
    return out


class _Planes:
    """One CSV per plane, painted (Y, X) on its folder's PNG grid; axes [z]yx."""

    def __init__(self, io: IO, urls: list[str], boxes: str, classes: list[str], class_key: str, overhang: int,
                 image_ext: str):
        self._io, self._urls, self._boxes, self._classes = io, urls, boxes, classes
        self._class_key, self._overhang, self._ext = class_key, overhang, image_ext
        self._last: tuple[int, np.ndarray] | None = None    # the last painted plane
        self._grid = self._shape(0)
        self._plane(0, self._grid)      # the first CSV is parsed and checked at open; nothing is kept
        many = len(urls) > 1
        self.axes = ("z" if many else "") + "yx"
        self.shape = ((len(urls),) if many else ()) + self._grid
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # a y/x region costs its CSV and its PNG's header
        self.voxel_size_nm = None       # a VIA CSV has none

    def _shape(self, k: int) -> tuple[int, int]:
        return _png_shape(self._io, _grid_url(self._io, self._urls[k], self._ext))

    def _plane(self, k: int, shape: tuple[int, int]) -> np.ndarray:
        url = self._urls[k]
        regions = _regions(_all_bytes(self._io, url), url, self._class_key)
        return _paint(shape, regions, self._boxes, self._classes, self._overhang, url)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in "yx")
        if "z" not in self.axes:
            return self._file(0)[inner]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._file(range(len(self._urls))[z])[inner]     # IndexError when out of range, like numpy
        planes = [self._file(k)[inner] for k in range(len(self._urls))[z]]
        if not planes:
            return np.empty((0,) + np.empty(self._grid, self.dtype)[inner].shape, self.dtype)
        return np.stack(planes)

    def _file(self, k: int) -> np.ndarray:
        """CSV k as (Y, X), on a grid checked against the first file's."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        shape = self._shape(k)
        if shape != self._grid:
            raise ValueError(f"{self._urls[k]}: its image is {shape}, the first file's {self._grid}; all files of "
                             "one array must agree, bind the others as another array")
        a = self._plane(k, shape)
        self._last = (k, a)
        return a


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if not out:
        raise ValueError("the urls match no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    boxes, classes, class_key = p.get("boxes"), p.get("classes"), p.get("class_key")
    overhang, image_ext = p.get("overhang", 0), p.get("image_ext", "png")
    if boxes not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {boxes!r}")
    if not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
            and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    if not (isinstance(class_key, str) and class_key):
        raise ValueError(f"params.class_key must name the region attribute that holds the class, got {class_key!r}")
    if not (isinstance(overhang, int) and not isinstance(overhang, bool) and overhang >= 0):
        raise ValueError(f"params.overhang must be a whole number of pixels >= 0, got {overhang!r}")
    if not (isinstance(image_ext, str) and image_ext and "." not in image_ext and "/" not in image_ext):
        raise ValueError(f"params.image_ext must be an extension without the dot, got {image_ext!r}")
    return _Planes(io, _expand(src.urls, io), boxes, classes, class_key, overhang, image_ext.lower())
