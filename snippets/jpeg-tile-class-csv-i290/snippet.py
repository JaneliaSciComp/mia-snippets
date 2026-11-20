"""One JPEG image (a zip member or a plain URL), or the per-tile class table of that image painted on its pixel
grid.

The image is cut into square tiles of `params.tile` pixels from its top left corner, and a CSV beside it has one
row per annotated tile: `row`, `col` and one 0/1 column per class. Without `params.tile` the array is the image
(a colour JPEG's samples are the c axis). With it the urls are the image and its CSV, and the array is a label on
the image's grid: the pixels of tile (row, col), rows [row * tile, (row + 1) * tile) and columns
[col * tile, (col + 1) * tile), hold the 1-based position in `params.classes` of the class column that is 1 in
that row. Tiles without a row, and the strip right of and below the last whole tile, are 0.

`open_array` reads the image's frame header (nothing is decoded) and, for labels, the table. `read` decodes the
image; a label region is made from the table alone.
"""
import csv
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MAX_CLASSES = 255           # ids are uint8, 0 is "no row for this tile"
HEAD = 1 << 16              # bytes read at a time while looking for the frame header
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _frame(data: bytes, name: str, whole: bool) -> tuple[int, int, int] | None:
    """(Y, X, samples) from the start-of-frame segment of a JPEG's first bytes; None when `data` ends before it
    and is not the `whole` file. Nothing is decoded."""
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
        if marker in SOF:
            if pos + 10 > len(data):
                break
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8 or ns not in (1, 3) or ny == 0 or nx == 0:
                raise ValueError(f"{name}: a {bits}-bit JPEG of {ny}x{nx} with {ns} samples; this snippet reads "
                                 "8-bit grey and three-sample JPEGs only")
            return ny, nx, ns
        if marker == 0xDA:                              # a scan before any frame header
            raise ValueError(f"{name}: no JPEG start-of-frame segment before the first scan")
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    if whole:
        raise ValueError(f"{name}: no JPEG start-of-frame segment found")
    return None


def _header(url: str, io: IO) -> tuple[int, int, int]:
    """(Y, X, samples) of the JPEG at `url`, from as few of its first bytes as hold the frame header: by range
    when the file can be read so (a plain URL, a stored zip member, a file staged before); a deflated zip member
    is inflated from its start through the zip; a plain file on a server without Range is staged whole."""
    container, sep, member = url.rpartition("::")
    try:
        fh = io.open(url)
    except NeedsFetch:
        if not sep:
            fh = open(io.fetch(url), "rb")
        else:
            try:
                z = ZipFile(io.open(container))
            except NeedsFetch:                          # a zip that is itself a deflated member of another zip
                z = ZipFile(io.fetch(container))
            fh = z.open(member)
    with fh:
        data = b""
        while True:
            more = fh.read(HEAD)
            data += more
            frame = _frame(data, url, not more)
            if frame is not None:
                return frame


class _Image:
    """The image at one url: [c]yx, uint8."""
    access = "plane"            # one file: any region costs fetching and decoding the whole image
    voxel_size_nm = None        # a JPEG's JFIF density is a print size, not a pixel size
    dtype = np.dtype("uint8")
    # The last image decoded by any array of this snippet. One slot for all of them, so a binding with one array
    # per image holds one decoded image (3 bytes a pixel), not one per array it has read.
    _last: tuple[str, np.ndarray] | None = None

    def __init__(self, url: str, io: IO, ny: int, nx: int, ns: int):
        self._url, self._io = url, io
        self._file_shape = (ny, nx) if ns == 1 else (ny, nx, ns)
        self.axes = "yx" if ns == 1 else "cyx"
        self.shape = (ny, nx) if ns == 1 else (ns, ny, nx)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        img = self._decoded()
        if img.ndim == 3:
            img = np.moveaxis(img, 2, 0)                # a view: (Y, X, 3) as (c, Y, X)
        return img[tuple(region.get(a, slice(None)) for a in self.axes)]

    def _decoded(self) -> np.ndarray:
        last = _Image._last
        if last is not None and last[0] == self._url:
            return last[1]
        _Image._last = None                             # let the previous image go before decoding the next
        data = self._io.fetch(self._url).read_bytes()   # a zip member comes inflated and CRC-checked
        if data[:2] != b"\xff\xd8":
            raise ValueError(f"{self._url} is not a JPEG (starts {data[:4]!r})")
        img = np.asarray(imagecodecs.jpeg_decode(data))
        if img.shape != self._file_shape or img.dtype != self.dtype:
            raise ValueError(f"{self._url}: decoded as {img.dtype} {img.shape}, not the uint8 {self._file_shape} "
                             "of its frame header")
        _Image._last = (self._url, img)
        return img


class _Tiles:
    """The class id of every pixel's tile: yx, uint8, made from the tile grid on each read."""
    access = "chunk"            # a region costs that region: nothing is fetched or decoded after open
    voxel_size_nm = None
    axes = "yx"
    dtype = np.dtype("uint8")

    def __init__(self, ny: int, nx: int, tile: int, grid: np.ndarray):
        self.shape = (ny, nx)
        self._tile = tile
        self._grid = grid       # (ceil(ny / tile), ceil(nx / tile)): class id per tile, 0 without a row

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        rows = np.arange(self.shape[0])[region.get("y", slice(None))] // self._tile     # IndexError as numpy
        cols = np.arange(self.shape[1])[region.get("x", slice(None))] // self._tile
        return self._grid[rows][..., cols]


def _whole(text: str) -> int | None:
    """The whole number >= 0 that `text` writes ('84' or '84.0'), else None."""
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() and value >= 0 else None    # nan and inf are not integers


def _grid(data: bytes, name: str, ny: int, nx: int, tile: int, classes: list[str], ignore: list[str]) -> np.ndarray:
    """The table as class ids per tile, shape (ceil(ny / tile), ceil(nx / tile)); 0 where no row names the tile
    or its row sets no class."""
    lines = list(csv.reader(data.decode("utf-8-sig").splitlines()))
    header = [h.strip() for h in lines[0]] if lines else []
    wanted = ["row", "col", *classes]
    if any(header.count(c) != 1 for c in wanted):
        raise ValueError(f"{name}: header {header} does not have each of the columns {wanted} once")
    unknown = [h for h in header if h not in wanted and h not in ignore]
    if unknown:
        raise ValueError(f"{name}: columns {unknown} are in neither params.classes nor params.ignore")
    r, c = header.index("row"), header.index("col")
    at = [header.index(k) for k in classes]
    grid = np.zeros((-(-ny // tile), -(-nx // tile)), np.uint8)
    seen: set[tuple[int, int]] = set()
    for line, cells in enumerate(lines[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue                                    # a blank line
        if len(cells) != len(header):
            raise ValueError(f"{name} line {line}: {len(cells)} fields, the header has {len(header)}")
        row, col = _whole(cells[r]), _whole(cells[c])
        if row is None or col is None:
            raise ValueError(f"{name} line {line}: row {cells[r]!r}, col {cells[c]!r} are not whole numbers >= 0")
        if (row + 1) * tile > ny or (col + 1) * tile > nx:
            raise ValueError(f"{name} line {line}: tile row {row}, col {col} of {tile} pixels ends at y "
                             f"{(row + 1) * tile}, x {(col + 1) * tile}, outside the {ny}x{nx} image (height x "
                             "width); params.tile is too large for this table")
        if (row, col) in seen:
            raise ValueError(f"{name} line {line}: a second row for tile row {row}, col {col}")
        seen.add((row, col))
        flags = [_whole(cells[k]) for k in at]
        if any(f not in (0, 1) for f in flags):
            raise ValueError(f"{name} line {line}: class cells {[cells[k] for k in at]} are not all 0 or 1")
        if sum(flags) > 1:
            raise ValueError(f"{name} line {line}: tile row {row}, col {col} has several classes set "
                             f"({[k for k, f in zip(classes, flags) if f]}); a tile takes one class id")
        if sum(flags) == 1:
            grid[row, col] = flags.index(1) + 1
    return grid


def _names(value: object, what: str, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not (isinstance(value, list) and (value or not required) and len(set(value)) == len(value)
            and all(isinstance(v, str) and v and v not in ("row", "col") for v in value)):
        raise ValueError(f"params.{what} must be a list of distinct column names of the table, got {value!r}")
    return value


def _ext(url: str) -> str:
    return url.rpartition(".")[2].lower()


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    tile = p.get("tile")
    images = [u for u in src.urls if _ext(u) in ("jpg", "jpeg")]
    if tile is None:
        extra = [k for k in ("classes", "ignore") if p.get(k) is not None]
        if extra:
            raise ValueError(f"params {extra} apply to the tile table only: set params.tile and give the table's "
                             "url after the image's, or drop them to read the image")
        if len(src.urls) != 1 or len(images) != 1:
            raise ValueError(f"an image array takes one url, a .jpg or .jpeg file, got {list(src.urls)}")
        return _Image(images[0], io, *_header(images[0], io))
    if isinstance(tile, bool) or not isinstance(tile, int) or tile < 1:
        raise ValueError(f"params.tile must be the tile edge in pixels, a whole number >= 1, got {tile!r}")
    classes, ignore = _names(p.get("classes"), "classes", True), _names(p.get("ignore"), "ignore", False)
    if len(classes) > MAX_CLASSES or set(classes) & set(ignore):
        raise ValueError(f"params.classes holds at most {MAX_CLASSES} names, none of them in params.ignore; got "
                         f"{len(classes)} classes, {sorted(set(classes) & set(ignore))} in both")
    tables = [u for u in src.urls if _ext(u) == "csv"]
    if len(src.urls) != 2 or len(images) != 1 or len(tables) != 1:
        raise ValueError("a tile label array takes two urls, the image (.jpg or .jpeg) and its table (.csv), got "
                         f"{list(src.urls)}")
    image, table = images[0], tables[0]
    stem = image[:image.rindex(".")]
    if not table.startswith(stem + "_"):
        raise ValueError(f"{table} is not a table of {image}: a table is named <image name without its "
                         "extension>_<anything>.csv and lies beside its image")
    ny, nx, _ = _header(image, io)
    return _Tiles(ny, nx, tile, _grid(io.fetch(table).read_bytes(), table, ny, nx, tile, classes, ignore))
