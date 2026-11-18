"""One multi-page TIFF (a PerkinElmer/Akoya QPTIFF, or any TIFF whose first series is a stack of single-sample
pages) from a zip member or a plain URL, read tile by tile; or, with a `.csv` url beside it, a per-object table
of centroids in micrometres marked on that image's pixel grid.

Raw form: `urls: [<tiff>]`. The array is tifffile's `series[0]` at full resolution (the pyramid's reduced levels,
thumbnail, macro and label images are other series or levels and are not read). A read decodes only the tiles
its region touches.

Label form: `urls: [<table.csv>, <tiff>]`. The TIFF gives the grid (y and x size) and the pixel size; the table
gives one point per row. Each point marks the pixel that holds it, with a class id (`class_column`, `classes`)
or the row's number."""
import csv
from pathlib import Path

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXIS = {"C": "c", "Z": "z", "Q": "z", "I": "z", "Y": "y", "X": "x"}   # tifffile letter -> czyx
UNIT_NM = {2: 25.4e6, 3: 1e7}                                         # TIFF ResolutionUnit: inch, centimetre
PARAMS = ("x_column", "y_column", "class_column", "classes", "radius")


class _Image:
    """series[0] of a TIFF as pages of one sample each; `access` is file (staged), chunk (tiles by range) or
    plane (strips by range)."""

    def __init__(self, tif: tifffile.TiffFile, staged: bool):
        self.tif = tif
        s = tif.series[0]
        if len(s.shape) < 2 or s.axes[-2:] != "YX" or tuple(s.keyframe.shape) != tuple(s.shape[-2:]):
            raise ValueError(f"series 0 is {s.axes!r} {tuple(s.shape)} with pages {tuple(s.keyframe.shape)}: "
                             "this snippet reads stacks of single-sample YX pages")
        outer = [(a, n) for a, n in zip(s.axes[:-2], s.shape[:-2])]
        if int(np.prod([n for _, n in outer], dtype=np.int64)) != len(s.pages):
            raise ValueError(f"series 0 {s.axes!r} {tuple(s.shape)} does not have one page per plane "
                             f"({len(s.pages)} pages)")
        self._outer = tuple(n for _, n in outer)                  # every dim that indexes pages
        self._kept = [i for i, (_, n) in enumerate(outer) if n > 1]   # size-1 ones are not axes of the array
        axes = "".join(AXIS.get(outer[i][0], "?") for i in self._kept) + "yx"
        if "?" in axes or len(set(axes)) != len(axes):
            raise ValueError(f"tifffile axes {s.axes!r} {tuple(s.shape)} do not map to czyx")
        self.axes = axes
        self.shape = tuple(self._outer[i] for i in self._kept) + tuple(int(n) for n in s.shape[-2:])
        self.dtype = np.dtype(s.dtype)
        self._pages = s.pages
        self._tiled = bool(s.keyframe.is_tiled)
        self.access = "file" if staged else "chunk" if self._tiled else "plane"
        self.voxel_size_nm = _voxel_size(tif)
        self._last: tuple[int, np.ndarray] | None = None          # the last untiled page decoded

    def __del__(self):          # the harness never closes arrays; a staged file's handle goes with the array
        self.tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        sel = [region.get(a, slice(None)) for a in self.axes]
        index: list[int | slice] = [0] * len(self._outer)
        for i, s in zip(self._kept, sel[:-2]):
            index[i] = s
        keys = np.arange(len(self._pages)).reshape(self._outer)[tuple(index)]
        picks = [_pick(s, n) for s, n in zip(sel[-2:], self.shape[-2:])]
        (ys, y0, y1), (xs, x0, x1) = picks
        planes = [self._box(int(k), y0, y1, x0, x1)[np.ix_(ys - y0, xs - x0)] for k in np.ravel(keys)]
        out = np.stack(planes).reshape(np.shape(keys) + planes[0].shape)
        return out[(..., *(0 if isinstance(s, (int, np.integer)) else slice(None) for s in sel[-2:]))]

    def _box(self, k: int, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        """Rows y0..y1 and columns x0..x1 of page k, from the tiles that hold them."""
        page = self._pages[k]
        if page is None:
            raise ValueError(f"page {k} of series 0 is not in this file")
        if not self._tiled:
            if self._last is None or self._last[0] != k:
                self._last = (k, np.asarray(page.asarray()).reshape(self.shape[-2:]))
            return self._last[1][y0:y1, x0:x1]
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        tl, tw = int(page.tilelength), int(page.tilewidth)
        across = -(-int(page.imagewidth) // tw)
        fh = self.tif.filehandle
        for ty in range(y0 // tl, -(-y1 // tl)):
            for tx in range(x0 // tw, -(-x1 // tw)):
                i = ty * across + tx
                if not page.databytecounts[i]:
                    continue                                    # a tile the file does not store reads 0
                fh.seek(page.dataoffsets[i])
                data, _, shape = page.decode(fh.read(page.databytecounts[i]), i)
                if data is None:
                    continue
                tile = np.asarray(data).reshape(shape[1], shape[2])      # cut to the image at its edges
                top, left = ty * tl, tx * tw
                ya, yb = max(y0, top), min(y1, top + tile.shape[0])
                xa, xb = max(x0, left), min(x1, left + tile.shape[1])
                out[ya - y0:yb - y0, xa - x0:xb - x0] = tile[ya - top:yb - top, xa - left:xb - left]
        return out


def _pick(sel: int | slice, n: int) -> tuple[np.ndarray, int, int]:
    """A selection on an axis of length n as its indices and the run [lo, hi) that holds them."""
    if isinstance(sel, slice):
        idx = np.arange(n)[sel]
    else:
        k = int(sel) + n if int(sel) < 0 else int(sel)
        if not 0 <= k < n:
            raise IndexError(f"index {sel} is out of range for {n}")
        idx = np.array([k])
    return (idx, int(idx.min()), int(idx.max()) + 1) if len(idx) else (idx, 0, 0)


def _voxel_size(tif: tifffile.TiffFile) -> dict[str, float] | None:
    """A QPTIFF states its pixel size in X/YResolution with ResolutionUnit (in any other TIFF those tags are a
    print size, so they are not used); None when the file doesn't say."""
    if not tif.is_qpi:
        return None
    tags = tif.series[0].keyframe.tags
    unit = UNIT_NM.get(int(tags["ResolutionUnit"].value)) if "ResolutionUnit" in tags else None
    out = {}
    for a, name in (("y", "YResolution"), ("x", "XResolution")):
        num, den = tags[name].value if name in tags else (0, 0)
        if unit and num and den:
            out[a] = unit * den / num
    return out if len(out) == 2 else None


class _Points:
    """A table of centroids (micrometres) as a yx plane on an image's grid; the table is read on the first read."""
    axes, access = "yx", "plane"

    def __init__(self, io: IO, url: str, image: _Image, params: dict):
        sizes = dict(zip(image.axes, image.shape))
        self.shape = (sizes["y"], sizes["x"])
        self.voxel_size_nm = image.voxel_size_nm
        if self.voxel_size_nm is None:
            raise ValueError("the image does not state its pixel size, so centroids in micrometres cannot be "
                             "placed on its grid")
        self._io, self._url = io, url
        self._x, self._y = str(params.get("x_column", "x")), str(params.get("y_column", "y"))
        self._class, classes = params.get("class_column"), params.get("classes")
        if (self._class is None) != (classes is None):
            raise ValueError("params.class_column and params.classes go together")
        if classes is not None and (not isinstance(classes, list) or not classes
                                    or len(set(map(str, classes))) != len(classes)):
            raise ValueError("params.classes must be a list of distinct class names")
        self._ids = {str(name): i + 1 for i, name in enumerate(classes or [])}
        radius = params.get("radius", 0)
        if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
            raise ValueError(f"params.radius must be a whole number of pixels, got {radius!r}")
        self._radius = radius
        self.dtype = np.dtype(np.uint32 if classes is None else np.uint8 if len(classes) < 256 else np.uint16)
        self._plane: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:
            self._plane = self._draw(*_table(self._io.fetch(self._url), self._x, self._y, self._class, self._ids))
        return self._plane[region.get("y", slice(None)), region.get("x", slice(None))]

    def _draw(self, x_um: np.ndarray, y_um: np.ndarray, values: np.ndarray) -> np.ndarray:
        ny, nx = self.shape
        rows = np.floor(y_um * 1e3 / self.voxel_size_nm["y"]).astype(np.int64)   # a pixel spans [i, i + 1) pixels
        cols = np.floor(x_um * 1e3 / self.voxel_size_nm["x"]).astype(np.int64)
        off = (rows < 0) | (rows >= ny) | (cols < 0) | (cols >= nx)
        if off.any():
            k = int(np.flatnonzero(off)[0])
            raise ValueError(f"{int(off.sum())} of {len(off)} points lie outside the {ny}x{nx} grid; the first is "
                             f"data row {k + 1} at x={x_um[k]} y={y_um[k]} um: the table is not in this image's frame")
        plane = np.zeros(self.shape, self.dtype)
        r = self._radius
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if (dy or dx) and dy * dy + dx * dx <= r * r:
                    ok = (rows + dy >= 0) & (rows + dy < ny) & (cols + dx >= 0) & (cols + dx < nx)
                    plane[rows[ok] + dy, cols[ok] + dx] = values[ok]
        plane[rows, cols] = values        # last, so a centroid's own pixel is never another point's disk
        return plane


def _table(path: Path, x: str, y: str, cls: str | None, ids: dict[str, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(x, y, value) per data row: the class's id when `cls` names a column, else the row's 1-based number."""
    xs: list[float] = []
    ys: list[float] = []
    values: list[int] = []
    unknown: dict[str, int] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader([fh.readline()]))
        missing = [c for c in (x, y, cls) if c is not None and header.count(c) != 1]
        if missing:
            raise ValueError(f"columns {missing} are not (once) among the table's {len(header)} columns: {header[:12]}")
        ix, iy, ic = header.index(x), header.index(y), header.index(cls) if cls is not None else -1
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            row = next(csv.reader([line])) if '"' in line else line.rstrip("\r\n").split(",")
            if len(row) != len(header):
                raise ValueError(f"data row {n} has {len(row)} fields, the header {len(header)}")
            xs.append(float(row[ix]))
            ys.append(float(row[iy]))
            if cls is None:
                values.append(n)
            elif row[ic] in ids:
                values.append(ids[row[ic]])
            else:
                unknown[row[ic]] = unknown.get(row[ic], 0) + 1
    if unknown:
        raise ValueError(f"column {cls!r} holds values that params.classes does not list: {dict(list(unknown.items())[:10])}")
    xy = np.array([xs, ys], np.float64).reshape(2, -1)
    if not np.isfinite(xy).all():
        raise ValueError(f"{int((~np.isfinite(xy)).any(axis=0).sum())} rows have no finite {x!r}/{y!r}")
    return xy[0], xy[1], np.array(values, np.int64)


def open_array(src: Source, io: IO) -> Array:
    tables = [u for u in src.urls if u.lower().endswith(".csv")]
    images = [u for u in src.urls if u not in tables]
    if len(images) != 1 or len(tables) > 1:
        raise ValueError(f"tiff-tiles-csv-points reads one TIFF, or one .csv and one TIFF; got {list(src.urls)}")
    try:
        tif, staged = tifffile.TiffFile(io.open(images[0])), False
    except NeedsFetch:      # a deflated member, or a server without Range: stage the whole file
        tif, staged = tifffile.TiffFile(io.fetch(images[0])), True
    image = _Image(tif, staged)
    extra = sorted(set(src.params) - set(PARAMS if tables else ()))
    if extra:
        raise ValueError(f"params {extra} do not apply to the {'label' if tables else 'raw'} form")
    return _Points(io, tables[0], image, src.params) if tables else image
