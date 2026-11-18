"""JPEG images chosen by glob from the members of a zip, one image per z plane, or the point annotations of
those images from a CSV table in the same zip, rasterised as discs on the image grid.

Each url names one zip; `params.glob` picks the images inside it, sorted by member name. Without `params.table`
the array is the images (a colour JPEG's samples are the c axis). With `params.table`, a pattern matching the one
CSV member that holds a row per point (columns imageName, objectType, xCoord, yCoord), the array is a label stack
on the same planes: every point is a disc of `params.radius` pixels around (yCoord, xCoord) in the plane of the
image its row names, filled with the point's number in that image (`label: instance`) or with the 1-based index
of its objectType in `classes` (`label: semantic`). An image without rows is an empty plane.

`open_array` reads each zip's directory, the first image's bytes (its frame header only; nothing is decoded) and,
for labels, the table. `read` decodes or paints the planes it needs.
"""
import csv
from fnmatch import fnmatchcase
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("instance", "semantic")
COLUMNS = ("imageName", "objectType", "xCoord", "yCoord")
MAX_RADIUS = 255            # keeps a squared distance inside uint16
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

File = tuple[ZipFile, str, str]     # open zip, its url, member name: one per z plane
Point = tuple[int, int, int]        # label value, y, x


def _jpeg_header(data: bytes, name: str) -> tuple[int, int, int]:
    """(Y, X, samples) from the start-of-frame segment; nothing is decoded."""
    if data[:2] != b"\xff\xd8":
        raise ValueError(f"{name} is not a JPEG (starts {data[:4]!r})")
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:    # markers without a length
            pos += 2
            continue
        if marker in SOF and pos + 10 <= len(data):
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8 or ns not in (1, 3) or ny == 0 or nx == 0:
                raise ValueError(f"{name}: a {bits}-bit JPEG of {ny}x{nx} with {ns} samples; this snippet reads "
                                 "8-bit grey and three-sample JPEGs only")
            return ny, nx, ns
        if marker in SOF or marker == 0xDA:             # a cut frame header, or a scan before any frame header
            break
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    raise ValueError(f"{name}: no JPEG start-of-frame segment found")


class _Planes:
    """One image per plane, axes [z][c]yx. Subclasses give `_plane(k)`: plane k as (Y, X) or (C, Y, X)."""

    def __init__(self, files: list[File], ny: int, nx: int, nc: int, dtype: str):
        self._files = files
        self._last: tuple[int, np.ndarray] | None = None    # the last plane made: its regions are read in turn
        many = len(files) > 1
        self.axes = ("z" if many else "") + ("c" if nc > 1 else "") + "yx"
        self.shape = ((len(files),) if many else ()) + ((nc,) if nc > 1 else ()) + (ny, nx)
        self.dtype = np.dtype(dtype)
        self.voxel_size_nm = None       # a JPEG's JFIF density is a print size, not a pixel size
        self.access = "plane"           # one file per plane: any y/x region costs that whole image

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        if "z" not in self.axes:
            return self._cached(0)[inner]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._cached(range(len(self._files))[z])[inner]      # IndexError when out of range, like numpy
        planes = [self._cached(k)[inner] for k in range(len(self._files))[z]]
        if planes:
            return np.stack(planes)
        blank = np.zeros(self.shape[1:], self.dtype)[inner]             # an empty z range, as numpy
        return np.empty((0, *blank.shape), self.dtype)

    def _cached(self, k: int) -> np.ndarray:
        if self._last is None or self._last[0] != k:
            self._last = (k, self._plane(k))
        return self._last[1]

    def _plane(self, k: int) -> np.ndarray:
        raise NotImplementedError

    def _name(self, k: int) -> str:
        return f"{self._files[k][1]}::{self._files[k][2]}"


class _Images(_Planes):
    def __init__(self, files: list[File], ny: int, nx: int, ns: int):
        super().__init__(files, ny, nx, ns, "uint8")
        self._file_shape = (ny, nx) if ns == 1 else (ny, nx, ns)

    def _plane(self, k: int) -> np.ndarray:
        """The decoded image of file k, checked against the first file's header."""
        data = self._files[k][0].read(self._files[k][2])    # zipfile inflates the member and checks its CRC
        if data[:2] != b"\xff\xd8":
            raise ValueError(f"{self._name(k)} is not a JPEG (starts {data[:4]!r})")
        a = np.asarray(imagecodecs.jpeg_decode(data))
        if a.shape != self._file_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._name(k)}: decoded as {a.dtype} {a.shape}, not the uint8 {self._file_shape} "
                             "of the first file's header; all files of one array must agree")
        return np.moveaxis(a, 2, 0) if a.ndim == 3 else a


class _Points(_Planes):
    def __init__(self, files: list[File], ny: int, nx: int, points: dict[int, list[Point]], radius: int):
        super().__init__(files, ny, nx, 1, "uint16")
        self._points = points           # plane index -> its points, in table order
        d = np.arange(-radius, radius + 1, dtype=np.int32) ** 2
        self._radius, self._d2 = radius, (d[:, None] + d[None, :]).astype(np.uint16)

    def _plane(self, k: int) -> np.ndarray:
        """Plane k with every point's disc (squared distance <= radius**2) filled with the point's value. A pixel
        inside several discs takes the nearest point's value; the earlier row wins a tie. 0 is background."""
        ny, nx = self.shape[-2:]
        r = self._radius
        out = np.zeros((ny, nx), np.uint16)
        points = self._points.get(k)
        if not points:
            return out
        best = np.full((ny, nx), r * r + 1, np.uint16)      # squared distance to the nearest point so far
        for value, y, x in points:
            y0, y1, x0, x1 = max(y - r, 0), min(y + r + 1, ny), max(x - r, 0), min(x + r + 1, nx)
            d2 = self._d2[y0 - y + r:y1 - y + r, x0 - x + r:x1 - x + r]
            near = d2 < best[y0:y1, x0:x1]
            out[y0:y1, x0:x1][near] = value
            best[y0:y1, x0:x1][near] = d2[near]
        return out


def _open_zip(url: str, io: IO) -> ZipFile:
    """The zip at `url`, read by range when it can be (a plain URL, a stored member of an outer zip), else staged
    whole (a deflated member of an outer zip, a server without Range)."""
    try:
        return ZipFile(io.open(url))
    except NeedsFetch:
        return ZipFile(io.fetch(url))


def _base(member: str) -> str:
    return member.rpartition("/")[2]


def _whole(text: str, name: str, line: int) -> int:
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not value.is_integer():      # nan and inf are not
        raise ValueError(f"{name} line {line}: coordinate {text!r} is not a whole number of pixels")
    return int(value)


def _rows(data: bytes, name: str) -> list[tuple[int, str, str, int, int]]:
    """(line number, imageName, objectType, x, y) per data row of the table, in file order."""
    lines = list(csv.reader(data.decode("utf-8-sig").splitlines()))
    header = [h.strip() for h in lines[0]] if lines else []
    if any(header.count(c) != 1 for c in COLUMNS):
        raise ValueError(f"{name}: header {header} does not have each of the columns {list(COLUMNS)} once")
    im, ty, xc, yc = (header.index(c) for c in COLUMNS)
    out = []
    for line, row in enumerate(lines[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue                                    # a blank line
        if len(row) != len(header):
            raise ValueError(f"{name} line {line}: {len(row)} fields, the header has {len(header)}")
        out.append((line, row[im].strip(), row[ty].strip(), _whole(row[xc], name, line), _whole(row[yc], name, line)))
    return out


def _points(z: ZipFile, url: str, table: str, planes: dict[str, int], ny: int, nx: int, mode: str,
            classes: list[str], skip: list[str]) -> dict[int, list[Point]]:
    """The points of one zip's table by plane index. `planes` maps the base name of each image of the array that
    is in this zip to its plane."""
    members = [n for n in z.namelist() if not n.endswith("/")]
    tables = sorted(n for n in members if fnmatchcase(n, table))
    if len(tables) != 1:
        raise ValueError(f"params.table {table!r} must match exactly one member of {url}, it matches "
                         f"{len(tables)}: {tables[:5]}")
    name = f"{url}::{tables[0]}"
    in_zip = {_base(n) for n in members}
    out: dict[int, list[Point]] = {}
    for line, image, kind, x, y in _rows(z.read(tables[0]), name):
        if kind not in classes and kind not in skip:
            raise ValueError(f"{name} line {line}: objectType {kind!r} is in neither params.classes {classes} nor "
                             f"params.skip {skip}")
        if image not in planes:
            if image not in in_zip:
                raise ValueError(f"{name} line {line}: imageName {image!r} is not the name of a member of the zip")
            continue                                    # an image of the zip that params.glob leaves out
        if not (0 <= x < nx and 0 <= y < ny):
            raise ValueError(f"{name} line {line}: point x={x} y={y} is outside the {nx}x{ny} image {image!r}")
        if kind in skip:
            continue
        mine = out.setdefault(planes[image], [])
        if len(mine) >= 65535:
            raise ValueError(f"{name}: more than 65535 points in {image!r} exceed uint16 ids")
        mine.append((len(mine) + 1 if mode == "instance" else classes.index(kind) + 1, y, x))
    return out


def _names(value: object, what: str, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not (isinstance(value, list) and (value or not required) and len(set(value)) == len(value)
            and all(isinstance(v, str) and v for v in value)):
        raise ValueError(f"params.{what} must be a list of distinct objectType values, got {value!r}")
    return value


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    glob, table = p.get("glob"), p.get("table")
    if not isinstance(glob, str) or not glob:
        raise ValueError(f"params.glob must be a member name pattern such as 'set/images/*.jpg', got {glob!r}")
    label_params = [k for k in ("label", "classes", "skip", "radius") if p.get(k) is not None]
    if table is None and label_params:
        raise ValueError(f"params {label_params} apply to the point table only: set params.table, or drop them "
                         "to read the images")
    if table is not None:
        if not isinstance(table, str) or not table:
            raise ValueError(f"params.table must be a member name pattern such as 'set/docs/*annotations*.csv', "
                             f"got {table!r}")
        mode, radius = p.get("label"), p.get("radius")
        if mode not in MODES:
            raise ValueError(f"params.label must be one of {MODES} when params.table is set, got {mode!r}")
        if isinstance(radius, bool) or not isinstance(radius, int) or not 0 <= radius <= MAX_RADIUS:
            raise ValueError(f"params.radius must be a whole number of pixels from 0 to {MAX_RADIUS} when "
                             f"params.table is set, got {radius!r}")
        classes, skip = _names(p.get("classes"), "classes", True), _names(p.get("skip"), "skip", False)
        if set(classes) & set(skip):
            raise ValueError(f"params.classes and params.skip both name {sorted(set(classes) & set(skip))}")
    if not src.urls or len(set(src.urls)) != len(src.urls):
        raise ValueError("the urls must name at least one zip, each once")
    files: list[File] = []
    zips: list[tuple[ZipFile, str, dict[str, int]]] = []    # open zip, its url, {image base name: plane}
    for url in src.urls:
        z = _open_zip(url, io)
        names = sorted(n for n in z.namelist() if not n.endswith("/") and fnmatchcase(n, glob))
        if not names:
            raise ValueError(f"no member of {url} matches params.glob {glob!r}")
        planes = {_base(n): len(files) + i for i, n in enumerate(names)}
        if table is not None and len(planes) != len(names):
            raise ValueError(f"{url}: params.glob {glob!r} matches images of the same file name in several "
                             "folders; the table names images by file name only")
        files += [(z, url, n) for n in names]
        zips.append((z, url, planes))
    ny, nx, ns = _jpeg_header(files[0][0].read(files[0][2]), f"{files[0][1]}::{files[0][2]}")
    if table is None:
        return _Images(files, ny, nx, ns)
    points: dict[int, list[Point]] = {}
    for z, url, planes in zips:
        points.update(_points(z, url, table, planes, ny, nx, mode, classes, skip))
    return _Points(files, ny, nx, points, radius)
