"""PNG images named by the rows of a CSV table, or the pixel boxes one column of that table holds for them,
stacked along z in row order, one row per plane.

The one url is the table. Each row names an image file in `name_column`; `image_url` (a template holding
`{name}`) says where that file is, so a file store without a directory index needs no glob. Without
`boxes_column` the array is the images, samples kept as `c`. With it the array is that column's cell, a Python
literal list of `{top, left, height, width[, type]}` dicts in pixels, painted on the grid of the row's PNG (read
from its header): by a per-row box number (`boxes: instance`) or by the 1-based index of the box's `type` in
`classes` (`boxes: semantic`). A lone `0,0,0,0` entry is the table's "none here" and paints nothing.
`open_array` reads the table and the first PNG's header; `read` gets the rest.
"""
import ast
import csv
import math
import re
from io import StringIO

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
PNG = b"\x89PNG\r\n\x1a\n"
SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}      # PNG colour type -> samples per pixel (3, palette, is refused)
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")     # a bare file name: no path, query, glob or zip separator
SIDES = ("top", "left", "height", "width")

Box = tuple[str | None, int, int, int, int]     # type, y0, x0, y1, x1


def _bytes(io: IO, url: str, n: int = -1) -> bytes:
    try:
        with io.open(url) as f:
            return f.read(n)
    except NeedsFetch:          # a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def _png_header(data: bytes, url: str) -> tuple[tuple[int, ...], np.dtype]:
    """((Y, X) or (Y, X, S), dtype) from the signature and IHDR, the first 33 bytes; nothing is decoded."""
    if data[:8] != PNG or data[12:16] != b"IHDR" or len(data) < 26:
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    nx, ny, bits, colour = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), data[24],
                            data[25])
    if bits not in (8, 16) or colour not in SAMPLES:
        raise ValueError(f"{url}: a PNG of bit depth {bits} and colour type {colour}; this snippet reads 8 and "
                         "16 bit grey and colour only (a palette or 1/2/4-bit file would be rescaled by the decoder)")
    return ((ny, nx) if colour == 0 else (ny, nx, SAMPLES[colour])), np.dtype("uint8" if bits == 8 else "uint16")


def _boxes(cell: str, where: str) -> list[Box]:
    """The boxes of one cell, in cell order, without the "none here" placeholder."""
    try:
        value = ast.literal_eval(cell.strip())
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        raise ValueError(f"{where}: the cell is not a Python literal: {cell[:80]!r}") from None
    if not isinstance(value, list) or not all(isinstance(b, dict) for b in value):
        raise ValueError(f"{where}: the cell is not a list of dicts: {cell[:80]!r}")
    boxes: list[Box] = []
    for n, b in enumerate(value, 1):
        if not set(SIDES) <= set(b) or not set(b) <= set(SIDES) | {"type"}:
            raise ValueError(f"{where}: box {n} has the keys {sorted(map(str, b))}, not {list(SIDES)} and, at "
                             "most, 'type'")
        kind = b.get("type")
        if "type" in b and not isinstance(kind, str):
            raise ValueError(f"{where}: box {n} has the type {kind!r}, not a string")
        for k in SIDES:
            if isinstance(b[k], bool) or not isinstance(b[k], (int, float)) or not math.isfinite(b[k]):
                raise ValueError(f"{where}: box {n} {k} is not a number: {b[k]!r}")
        if all(b[k] == 0 for k in SIDES):
            # the table's way to say "no object in this image"; it must be the only entry of its cell
            if len(value) != 1:
                raise ValueError(f"{where}: a 0,0,0,0 placeholder among {len(value)} boxes")
            return []
        # a fractional side is moved outwards: every pixel the box touches
        y0, x0 = math.floor(b["top"]), math.floor(b["left"])
        y1, x1 = math.ceil(b["top"] + b["height"]), math.ceil(b["left"] + b["width"])
        boxes.append((kind.strip() if isinstance(kind, str) else None, y0, x0, y1, x1))
    if len({kind is None for kind, *_ in boxes}) > 1:
        raise ValueError(f"{where}: some boxes have a 'type' and some have none")
    return boxes


def _paint(shape: tuple[int, int], boxes: list[Box], mode: str, classes: list[str] | None,
           where: str) -> np.ndarray:
    """Boxes filled over pixels [top, top+height) x [left, left+width), largest first, so the smaller of two
    overlapping boxes is on top (cell order breaks ties). instance: 1-based number of the box in its cell;
    semantic: 1-based index of the box's type in `classes`, or 1 for boxes without a type. 0 is background."""
    if len(boxes) > 65535:
        raise ValueError(f"{where}: {len(boxes)} boxes exceed uint16 ids")
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    order = sorted(range(len(boxes)), key=lambda i: -(boxes[i][3] - boxes[i][1]) * (boxes[i][4] - boxes[i][2]))
    for i in order:
        kind, y0, x0, y1, x1 = boxes[i]
        if (kind is None) != (classes is None):
            raise ValueError(f"{where}: " + ("the boxes have a 'type', so params.classes must list the type "
                                             "strings" if classes is None else "the boxes have no 'type', so "
                                             "params.classes does not apply"))
        if classes is not None and kind not in classes:
            raise ValueError(f"{where}: box type {kind!r} is not in params.classes {classes}")
        if not (0 <= x0 < x1 <= nx and 0 <= y0 < y1 <= ny):
            raise ValueError(f"{where}: box {i + 1} y {y0}..{y1} x {x0}..{x1} is empty or outside the "
                             f"{nx}x{ny} image")
        out[y0:y1, x0:x1] = i + 1 if mode == "instance" else (classes.index(kind) + 1 if classes else 1)
    return out


class _Rows:
    """One table row per z plane. Images: (Y, X) or (S, Y, X) per row, axes z[c]yx. Boxes: (Y, X), axes zyx."""

    def __init__(self, io: IO, rows: list[tuple[str, str, str | None]], boxes: str | None,
                 classes: list[str] | None):
        self._io, self._rows, self._boxes, self._classes = io, rows, boxes, classes
        self._last: tuple[int, np.ndarray] | None = None    # the last plane made: its c planes are read in turn
        self._label = boxes is not None
        self._file_shape, self._file_dtype = self._header(0)
        if self._label:
            self._plane(0)                                  # the first row's cell must parse and fit, at open
        c = () if self._label else self._file_shape[2:]
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape = (len(rows),) + c + self._file_shape[:2]
        self.dtype = np.dtype("uint16") if self._label else self._file_dtype
        self.access = "plane"           # a y/x region costs its file (labels: its file's first block)
        self.voxel_size_nm = None       # neither the table nor a PNG header (pHYs is a print size) says

    def _header(self, k: int) -> tuple[tuple[int, ...], np.dtype]:
        url = self._rows[k][1]
        return _png_header(_bytes(self._io, url, 33), url)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._plane(k)[inner] for k in range(len(self._rows))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self.shape[1:], bool)[inner].shape,
                                                            self.dtype)
        return self._plane(range(len(self._rows))[z])[inner]    # IndexError when out of range, like numpy

    def _plane(self, k: int) -> np.ndarray:
        """Row k as (Y, X) or (S, Y, X), its PNG checked against the first row's."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        where, url, cell = self._rows[k]
        data = b"" if self._label else _bytes(self._io, url)
        shape, dtype = self._header(k) if self._label else _png_header(data, url)
        if shape != self._file_shape or dtype != self._file_dtype:
            raise ValueError(f"{url}: {dtype} {shape} differs from the first row's image, {self._file_dtype} "
                             f"{self._file_shape}; all rows of one array must have one image shape")
        if self._label:
            a = _paint(shape[:2], _boxes(cell, where), self._boxes, self._classes, where)
        else:
            a = np.asarray(imagecodecs.png_decode(data))    # refused above when the header is another shape
            if a.shape != shape or a.dtype != dtype:        # e.g. a tRNS chunk, which adds an alpha sample
                raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, its header says {dtype} {shape}")
            a = np.ascontiguousarray(np.moveaxis(a, 2, 0)) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _text(value: object, name: str, pattern: str = r".+") -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"params.{name} {value!r}")
    return value


def _table(data: bytes, url: str, delimiter: str) -> tuple[list[str], list[list[str]]]:
    """Header and rows of the table; every row must have the header's number of cells."""
    try:
        rows = [r for r in csv.reader(StringIO(data.decode("utf-8-sig"), newline=""), delimiter=delimiter) if r]
    except (UnicodeDecodeError, csv.Error) as e:
        raise ValueError(f"{url}: not a UTF-8 CSV table with delimiter {delimiter!r}: {e}") from None
    if len(rows) < 2:
        raise ValueError(f"{url}: a table needs a header and at least one row")
    for n, r in enumerate(rows[1:], 2):
        if len(r) != len(rows[0]):
            raise ValueError(f"{url}: row {n} has {len(r)} cells, the header {len(rows[0])}; is "
                             f"params.delimiter {delimiter!r} right?")
    return [h.strip() for h in rows[0]], rows[1:]


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    template = _text(p.get("image_url"), "image_url: it must be a url holding {name} once, got",
                     r"[^{}]*\{name\}[^{}]*")
    name_column = _text(p.get("name_column"), "name_column: it must name the column of image file names, got")
    delimiter = _text(p.get("delimiter", ","), "delimiter: it must be one character, got", r"[^\r\n\"]")
    column, boxes, classes, count = p.get("boxes_column"), p.get("boxes"), p.get("classes"), p.get("count")
    if column is None and (boxes is not None or classes is not None):
        raise ValueError("params.boxes and params.classes apply to a box array only: set params.boxes_column")
    if column is not None:
        _text(column, "boxes_column: it must name the column of box lists, got")
        if boxes not in BOX_MODES:
            raise ValueError(f"params.boxes must be one of {BOX_MODES} when params.boxes_column is set, got "
                             f"{boxes!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c and c == c.strip() for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct type strings, got {classes!r}")
    if len(src.urls) != 1 or "::" in src.urls[0]:
        raise ValueError(f"the urls must be the one table file (no glob or zip member), got {list(src.urls)}")
    url = src.urls[0]
    header, table = _table(_bytes(io, url), url, delimiter)
    for name in (name_column,) + ((column,) if column is not None else ()):
        if header.count(name) != 1:
            raise ValueError(f"{url}: the header {header} does not have exactly one column {name!r}")
    if count is not None and (isinstance(count, bool) or count != len(table)):
        raise ValueError(f"params.count is {count!r}, but {url} has {len(table)} rows")
    rows: list[tuple[str, str, str | None]] = []
    for n, r in enumerate(table, 2):
        name = r[header.index(name_column)].strip()
        if not NAME.fullmatch(name):
            raise ValueError(f"{url}: row {n} names the image {name!r}, which is not a bare file name")
        rows.append((f"{url} row {n} ({name}) column {column}", template.replace("{name}", name),
                     r[header.index(column)] if column is not None else None))
    if len({image for _, image, _ in rows}) != len(rows):
        raise ValueError(f"{url}: column {name_column!r} names the same image in two rows")
    return _Rows(io, rows, boxes, classes)
