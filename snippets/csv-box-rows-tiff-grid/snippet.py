"""The boxes one image has in a CSV table of one row per box, rasterised on the pixel grid of that image's
TIFF (an Aperio SVS whole-slide image: the first page, the full-resolution level).

Two urls: the table (a plain url or a zip member), then the slide. The table covers many images; `name_column`
holds each row's image name and `image_name` picks the rows of this slide. `boxes_column` holds a Python-literal
dict `{'x1': .., 'x2': .., 'y1': .., 'y2': ..}` of corners in pixels of the full-resolution level. Each box is
filled with its 1-based number among the slide's rows (`boxes: instance`) or with the 1-based position of its
`class_column` value in `classes` (`boxes: semantic`). Of the slide only the first page's header is read, for
the grid; the table is read whole at open, and a read paints only the boxes its region touches.
"""
import ast
import csv
import math
import re
from io import StringIO
from typing import BinaryIO, Callable, TypeVar

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
CORNERS = ("x1", "x2", "y1", "y2")
TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
T = TypeVar("T")
MPP = re.compile(r"\|\s*MPP\s*=\s*([0-9.eE+-]+)")      # Aperio ImageDescription: "...|AppMag = 20|MPP = 0.5030|..."


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


class _Boxes:
    """Filled boxes on a (ny, nx) grid, yx. A pixel holds the value of the smallest box that covers it (boxes are
    painted largest first, table order breaking ties); 0 where none does."""

    def __init__(self, shape: tuple[int, int], voxel_size_nm: dict[str, float] | None, boxes: np.ndarray,
                 values: np.ndarray, dtype: np.dtype):
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        order = np.argsort(-area, kind="stable")
        self._boxes, self._values = boxes[order], values[order]     # rows of y0, x0, y1, x1: [y0, y1) x [x0, x1)
        self.axes, self.shape, self.dtype = "yx", shape, dtype
        self.voxel_size_nm = voxel_size_nm
        self.access = "chunk"           # a read costs what its region costs; the table was read whole at open

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        b = self._boxes
        for k in np.flatnonzero((b[:, 0] < y1) & (b[:, 2] > y0) & (b[:, 1] < x1) & (b[:, 3] > x0)):
            by0, bx0, by1, bx1 = (int(v) for v in b[k])
            out[max(by0, y0) - y0:min(by1, y1) - y0, max(bx0, x0) - x0:min(bx1, x1) - x0] = self._values[k]
        return np.array(out[yi][..., xi])


def _read(io: IO, url: str, use: Callable[[BinaryIO], T]) -> T:
    """`use(file)` on the url read by range, or, where ranges will not do (a deflated zip member, known at open;
    a server that ignores Range, known at the first read), on the whole file staged."""
    try:
        with io.open(url) as fh:
            return use(fh)
    except NeedsFetch:
        with open(io.fetch(url), "rb") as fh:
            return use(fh)


def _grid(url: str, fh: BinaryIO) -> tuple[tuple[int, int], dict[str, float] | None]:
    """(y, x) of the TIFF's first page, and its pixel size when an Aperio description states MPP."""
    magic = fh.read(4)
    fh.seek(0)
    if magic not in TIFF_MAGIC:
        raise ValueError(f"{url} starts with {magic!r}: the second url must be the TIFF (SVS) the boxes are on; "
                         "the table is the first url")
    with tifffile.TiffFile(fh) as tif:
        page = tif.pages[0]             # only this IFD is parsed; the pyramid, label and macro pages are not
        if not isinstance(page, tifffile.TiffPage):
            raise ValueError(f"{url}: the first IFD is not an image page")
        shape = (int(page.imagelength), int(page.imagewidth))
        mpp = MPP.search(page.description or "")
    try:
        nm = float(mpp.group(1)) * 1000 if mpp else 0.0
    except ValueError:
        nm = 0.0
    return shape, ({"y": nm, "x": nm} if math.isfinite(nm) and nm > 0 else None)


def _table(url: str, data: bytes, delimiter: str) -> tuple[list[str], list[list[str]]]:
    """Header and rows of the table; every row must have the header's number of cells."""
    if data[:4] in TIFF_MAGIC:
        raise ValueError(f"{url} is a TIFF: the first url must be the table, the second the slide")
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


def _box(cell: str, shape: tuple[int, int], where: str) -> tuple[int, int, int, int]:
    """(y0, x0, y1, x1) of one cell: the pixels [y1, y2) x [x1, x2) of the dict, a fractional corner moved
    outwards (every pixel the box touches)."""
    try:
        value = ast.literal_eval(cell.strip())
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        raise ValueError(f"{where}: the cell is not a Python literal: {cell[:80]!r}") from None
    if not isinstance(value, dict) or set(value) != set(CORNERS):
        raise ValueError(f"{where}: the cell is not a dict with exactly the keys {list(CORNERS)}: {cell[:80]!r}")
    for k in CORNERS:
        if isinstance(value[k], bool) or not isinstance(value[k], (int, float)) or not math.isfinite(value[k]):
            raise ValueError(f"{where}: {k} is not a number: {value[k]!r}")
    y0, x0 = math.floor(value["y1"]), math.floor(value["x1"])
    y1, x1 = math.ceil(value["y2"]), math.ceil(value["x2"])
    if not (0 <= y0 < y1 <= shape[0] and 0 <= x0 < x1 <= shape[1]):
        raise ValueError(f"{where}: the box y {y0}..{y1} x {x0}..{x1} is empty or outside the slide's "
                         f"{shape[1]}x{shape[0]} (x by y) grid; is the second url the image these rows are for?")
    return y0, x0, y1, x1


def _text(params: dict, key: str, pattern: str = r".+") -> str:
    value = params.get(key)
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"params.{key} must be a string matching {pattern!r}, got {value!r}")
    return value


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    image_name, name_column, column = _text(p, "image_name"), _text(p, "name_column"), _text(p, "boxes_column")
    delimiter = _text({"delimiter": p.get("delimiter", ",")}, "delimiter", r"[^\r\n\"]")
    mode, class_column, classes, count = p.get("boxes"), p.get("class_column"), p.get("classes"), p.get("count")
    if mode not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {mode!r}")
    if mode == "semantic":
        _text(p, "class_column")
        if not (isinstance(classes, list) and 0 < len(classes) <= 255 and len(set(map(str, classes))) == len(classes)
                and all(isinstance(c, (str, int)) and not isinstance(c, bool) and str(c) == str(c).strip() != ""
                        for c in classes)):
            raise ValueError("params.classes must list the distinct values of params.class_column (strings or "
                             f"whole numbers, at most 255) in class id order, got {classes!r}")
        classes = [str(c) for c in classes]
    elif class_column is not None or classes is not None:
        raise ValueError("params.class_column and params.classes apply to boxes: semantic only")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise ValueError(f"params.count must be the number of rows the image has in the table, got {count!r}")
    if len(src.urls) != 2:
        raise ValueError("csv-box-rows-tiff-grid reads two urls, the table and then the slide its boxes are on, "
                         f"got {len(src.urls)}")
    table_url, slide_url = src.urls
    shape, voxel_size_nm = _read(io, slide_url, lambda fh: _grid(slide_url, fh))
    header, table = _table(table_url, _read(io, table_url, lambda fh: fh.read()), delimiter)
    for name in (name_column, column) + ((class_column,) if mode == "semantic" else ()):
        if header.count(name) != 1:
            raise ValueError(f"{table_url}: the header {header} does not have exactly one column {name!r}")
    at_name, at_box = header.index(name_column), header.index(column)
    rows = [(n, r) for n, r in enumerate(table, 2) if r[at_name] == image_name]
    if not rows:
        raise ValueError(f"{table_url}: no row has {name_column} = {image_name!r} (compared as written); a slide "
                         "without boxes cannot be told from a wrong name, so it is not read as an empty label")
    if count is not None and count != len(rows):
        raise ValueError(f"params.count is {count}, but {table_url} has {len(rows)} rows with {name_column} = "
                         f"{image_name!r}")
    boxes = np.array([_box(r[at_box], shape, f"{table_url} row {n} ({image_name}) column {column}")
                      for n, r in rows], np.int64).reshape(-1, 4)
    if mode == "instance":
        return _Boxes(shape, voxel_size_nm, boxes, np.arange(1, len(rows) + 1, dtype=np.uint32), np.dtype("uint32"))
    at_class, values = header.index(class_column), np.zeros(len(rows), np.uint8)
    for i, (n, r) in enumerate(rows):
        if r[at_class].strip() not in classes:
            raise ValueError(f"{table_url} row {n}: {class_column} {r[at_class]!r} is not in params.classes "
                             f"{classes}")
        values[i] = classes.index(r[at_class].strip()) + 1
    return _Boxes(shape, voxel_size_nm, boxes, values, np.dtype("uint8"))
