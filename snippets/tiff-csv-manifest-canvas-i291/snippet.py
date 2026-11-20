"""Single-page TIFF files named by the rows of a CSV manifest, one file per z plane, grey (`zyx`) or colour
(`czyx`, the stored samples as c).

The one url is the manifest. `params.column` is the CSV column that holds each file's name and `params.files` the
file's path beside the manifest with `{name}` in it (`images/{name}.tif`), so no directory listing is needed.
`where` keeps the rows whose cells have the given values (`{domain: malnio}`). The rows are ordered by name.

An array has one shape, so every plane is `params.canvas` (height, width): a file's pixels sit unchanged at the
top left of its plane, pixel (y, x) of the file at (y, x) of the plane, and the rest of the plane is 0. Nothing is
cropped or resampled. With `shape_column` (a column holding each file's shape, `(h, w)` or `(h, w, samples)`) the
canvas is checked against every row at open, each file against its row at read, and the rows can be divided by
size into arrays with tight canvases: `oversize: skip` leaves out the rows that do not fit the canvas and
`larger_than` those that fit a smaller one.

Every file is staged whole with `io.fetch`: `open_array` stages the manifest and the first file (for the dtype
and the samples, from its header), `read` the files it needs.
"""
import csv
import re
from pathlib import Path

import numpy as np
import tifffile

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

NAME = re.compile(r"[A-Za-z0-9._~-]+")      # characters that stand for themselves in a url path
SHAPE = re.compile(r"\s*[(\[]?\s*(\d+)\s*[,x]\s*(\d+)\s*(?:[,x]\s*(\d+)\s*)?,?\s*[)\]]?\s*")
PLACEHOLDER = "{name}"


def _rows(text: str, url: str) -> list[dict[str, str]]:
    """The manifest's rows by column name; a row with more or fewer cells than the header raises."""
    reader = csv.reader(text.splitlines(), strict=True)
    header = next(reader, None)
    if not header or len(set(header)) != len(header):
        raise ValueError(f"{url}: the first line must name the columns, each once; got {header!r}")
    rows = []
    for cells in reader:
        if not cells:
            continue
        if len(cells) != len(header):
            raise ValueError(f"{url}: line {reader.line_num} has {len(cells)} cells, the header has {len(header)}")
        rows.append(dict(zip(header, cells)))
    return rows


def _hw(row: dict[str, str], column: str, url: str) -> tuple[int, int]:
    """(height, width) from the row's shape cell: `(h, w)`, `(h, w, samples)`, `[h, w]` or `hxw`."""
    m = SHAPE.fullmatch(row[column])
    if not m:
        raise ValueError(f"{url}: column {column!r} holds {row[column]!r}, not a shape like '(1200, 1277)' or "
                         "'(258, 259, 3)' (height, width and, for colour, samples)")
    return int(m[1]), int(m[2])


def _fits(hw: tuple[int, int], box: tuple[int, int]) -> bool:
    return hw[0] <= box[0] and hw[1] <= box[1]


def _select(rows: list[dict[str, str]], url: str, column: str, where: dict[str, list[str]],
            shape_column: str | None, canvas: tuple[int, int], larger_than: tuple[int, int] | None,
            skip_oversize: bool, count: int | None) -> list[tuple[str, tuple[int, int] | None]]:
    """(name, (height, width) from the manifest or None) of the array's files, ordered by name."""
    known = list(rows[0]) if rows else []
    for c in [column, *where, *([shape_column] if shape_column else [])]:
        if c not in known:
            raise ValueError(f"{url}: no column {c!r}; the manifest's columns are {known}")
    kept: list[tuple[str, tuple[int, int] | None]] = []
    for row in rows:
        if any(row[c] not in values for c, values in where.items()):
            continue
        name = row[column]
        if not NAME.fullmatch(name) or name in (".", ".."):
            raise ValueError(f"{url}: column {column!r} holds {name!r}; a file name may hold letters, digits and "
                             "._~- only (this snippet does not percent-encode)")
        hw = _hw(row, shape_column, url) if shape_column else None
        if hw is not None:
            if larger_than is not None and _fits(hw, larger_than):
                continue
            if not _fits(hw, canvas):
                if skip_oversize:
                    continue
                raise ValueError(f"{url}: {name} is {hw[0]}x{hw[1]} (height x width, column {shape_column!r}), "
                                 f"larger than params.canvas {canvas[0]}x{canvas[1]}; nothing is cropped, so give "
                                 "a canvas that holds the largest file, or oversize: skip to leave such rows to "
                                 "another array")
        kept.append((name, hw))
    names = [name for name, _ in kept]
    if len(set(names)) != len(names):
        raise ValueError(f"{url}: column {column!r} names a file twice among the selected rows")
    if not kept:
        raise ValueError(f"{url}: no row is selected by params.where, larger_than and oversize")
    if count is not None and len(kept) != count:
        raise ValueError(f"{url}: {len(kept)} rows are selected, not params.count {count}: the manifest changed, "
                         "or the binding is wrong")
    return sorted(kept)


def _series(tif: tifffile.TiffFile, url: str) -> tifffile.TiffPageSeries:
    """The file's one series, a single page with axes YX, YXS or SYX."""
    s = tif.series[0]
    if len(tif.series) != 1 or len(tif.pages) != 1 or s.axes not in ("YX", "YXS", "SYX"):
        raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.series)} series and "
                         f"{len(tif.pages)} pages; this snippet reads single-page YX or YXS files only")
    return s


class _Planes:
    access = "plane"            # one file per z plane: any region of it costs the whole file
    voxel_size_nm = None        # the manifest has none, and TIFF resolution fields of such images describe print size

    def __init__(self, io: IO, files: list[tuple[str, tuple[int, int] | None]], canvas: tuple[int, int]):
        self._io, self._files, self._canvas = io, files, canvas
        with tifffile.TiffFile(self._path(0)) as tif:       # header only: no pixel is decoded at open
            s = _series(tif, files[0][0])
            self.dtype = np.dtype(s.dtype)
            self._samples = None if s.axes == "YX" else int(s.shape[s.axes.index("S")])
        if self._samples is None:
            self.axes, self.shape = "zyx", (len(files), *canvas)
        else:
            self.axes, self.shape = "czyx", (self._samples, len(files), *canvas)
        self._last: tuple[int, np.ndarray] | None = None    # the last plane built

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._files))[sel])[idx]   # IndexError when out of range, like numpy
        z_axis = 1 if self._samples is not None and isinstance(idx[0], slice) else 0
        planes = [self._plane(k)[idx] for k in range(len(self._files))[sel]]
        if not planes:
            plane = tuple(n for a, n in zip(self.axes, self.shape) if a != "z")
            one = np.empty(plane, self.dtype)[idx].shape
            return np.empty(one[:z_axis] + (0,) + one[z_axis:], self.dtype)
        return np.stack(planes, axis=z_axis)

    def _path(self, k: int) -> Path:
        return self._io.fetch(self._files[k][0])

    def _plane(self, k: int) -> np.ndarray:
        """File k at the top left of a canvas of zeros: (H, W), or (samples, H, W)."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url, listed = self._files[k]
        with tifffile.TiffFile(self._path(k)) as tif:
            s = _series(tif, url)
            a = np.asarray(s.asarray())
            if s.axes == "YXS":
                a = np.moveaxis(a, 2, 0)
        got = None if a.ndim == 2 else a.shape[0]
        if got != self._samples or a.dtype != self.dtype:
            raise ValueError(f"{url}: dtype {a.dtype} with {got or 'no'} samples differs from the first file's "
                             f"{self.dtype} with {self._samples or 'no'} samples; all files of one array must "
                             "have one dtype and one number of samples")
        (h, w), (H, W) = a.shape[-2:], self._canvas
        if listed is not None and (h, w) != listed:
            raise ValueError(f"{url}: the file is {h}x{w} (height x width), the manifest's shape column says "
                             f"{listed[0]}x{listed[1]}")
        if h > H or w > W:
            raise ValueError(f"{url}: the file is {h}x{w} (height x width), larger than params.canvas {H}x{W}; "
                             "nothing is cropped, so give a canvas that holds the largest file of the array")
        plane = np.zeros(a.shape[:-2] + self._canvas, self.dtype)
        plane[..., :h, :w] = a
        self._last = (k, plane)
        return plane


def _box(value: object, what: str) -> tuple[int, int]:
    ok = isinstance(value, list) and len(value) == 2 and all(
        isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value)
    if not ok:
        raise ValueError(f"params.{what} must be [height, width], two positive whole numbers, got {value!r}")
    return value[0], value[1]


def _column(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"params.{what} must be the name of a CSV column, got {value!r}")
    return value


def _where(value: object) -> dict[str, list[str]]:
    """params.where as {column: the values a row's cell may have}."""
    out: dict[str, list[str]] = {}
    if value is None:
        return out
    if isinstance(value, dict):
        for column, wanted in value.items():
            values = wanted if isinstance(wanted, list) else [wanted]
            if not (isinstance(column, str) and column and values and all(isinstance(v, str) for v in values)):
                break
            out[column] = values
        else:
            return out
    raise ValueError("params.where must map column names to a string or a list of strings (quote numbers: cells "
                     f"are compared as text), got {value!r}")


def _template(value: object) -> str:
    """params.files, checked to be a path below the manifest's folder with one {name} in it."""
    ok = (isinstance(value, str) and value.count(PLACEHOLDER) == 1 and not value.startswith("/")
          and not any(part in ("", ".", "..") for part in value.split("/"))
          and re.fullmatch(r"[A-Za-z0-9._~/-]+", value.replace(PLACEHOLDER, "x")))
    if not ok:
        raise ValueError(f"params.files must be a file's path beside the manifest with {PLACEHOLDER} once in it, "
                         f"e.g. 'images/{PLACEHOLDER}.tif' (letters, digits and ._~-/ only, no '..'), got {value!r}")
    return str(value)


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    if len(src.urls) != 1 or "::" in src.urls[0] or "/" not in src.urls[0]:
        raise ValueError(f"this snippet takes one plain url, the CSV manifest; got {list(src.urls)}")
    url = src.urls[0]
    column, template = _column(p.get("column"), "column"), _template(p.get("files"))
    canvas = _box(p.get("canvas"), "canvas")
    shape_column = _column(p["shape_column"], "shape_column") if p.get("shape_column") is not None else None
    larger_than = _box(p["larger_than"], "larger_than") if p.get("larger_than") is not None else None
    if p.get("oversize") not in (None, "skip"):
        raise ValueError(f"params.oversize must be 'skip' when given, got {p['oversize']!r}")
    if shape_column is None and (larger_than is not None or p.get("oversize") is not None):
        raise ValueError("params.larger_than and params.oversize select rows by their shape, so they need "
                         "params.shape_column")
    if larger_than is not None and _fits(canvas, larger_than):
        raise ValueError(f"params.canvas {list(canvas)} fits in params.larger_than {list(larger_than)}, so no file "
                         "can be both larger than the one and within the other")
    count = p.get("count")
    if count is not None and not (isinstance(count, int) and not isinstance(count, bool) and count > 0):
        raise ValueError(f"params.count must be a positive whole number, got {count!r}")
    rows = _rows(io.fetch(url).read_text(encoding="utf-8-sig"), url)
    chosen = _select(rows, url, column, _where(p.get("where")), shape_column, canvas, larger_than,
                     p.get("oversize") == "skip", count)
    folder = url[:url.rindex("/") + 1]
    return _Planes(io, [(folder + template.replace(PLACEHOLDER, name), hw) for name, hw in chosen], canvas)
