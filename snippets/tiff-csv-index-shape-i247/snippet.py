"""Single-page grey TIFF files named by a CSV table, those of one width and height stacked as zyx.

For a folder of independent 2D images that has no directory index and whose images differ in shape: the table
(a header row with `file_name`, `width`, `height`; one row per image, `file_name` relative to the table's own
folder) is the index, and `params.width` / `params.height` select the rows of one shape. A binding has one entry
per shape. A second url, a Hugging Face repo-info document, says which of the table's files exist; rows whose
file it does not list are left out. `open_array` reads the table, the listing and the first file's header;
`read` opens the files of the planes it is asked for, and reads an uncompressed strip-contiguous page by row
range instead of whole.
"""
import csv
import json
import re
from collections import Counter
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

NAME, WIDTH, HEIGHT = "file_name", "width", "height"      # the table's columns this snippet reads
RESOLVE = re.compile(r"/resolve/[^/]+/(.*)$")             # a Hugging Face file url: .../resolve/<revision>/<path>
UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")


class _Stack:
    def __init__(self, io: IO, urls: list[str], page_shape: tuple[int, int]):
        self._io, self._urls, self._page_shape = io, urls, page_shape
        with self._tiff(0) as tif:
            page = self._page(tif, 0)
            self._file_dtype = np.dtype(page.dtype)
            contiguous = self._data_offset(page) is not None
        self.shape: tuple[int, ...] = (len(urls), *page_shape) if len(urls) > 1 else page_shape
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.dtype = self._file_dtype
        self.voxel_size_nm = None       # plain TIFF resolution tags describe print size, not pixels
        self.access = "chunk" if contiguous else "plane"    # rows by range, or the whole decoded page

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0, y, x)
        sel = region.get("z", slice(None))
        if isinstance(sel, slice):
            planes = [self._plane(k, y, x) for k in range(self.shape[0])[sel]]
            if not planes:
                return np.empty((0, *self._page_shape), self.dtype)[:, y, x]
            return np.stack(planes)
        return self._plane(range(self.shape[0])[sel], y, x)     # IndexError when out of range, like numpy

    def _tiff(self, k: int) -> tifffile.TiffFile:
        url = self._urls[k]
        try:
            fh: BinaryIO = self._io.open(url)
            return tifffile.TiffFile(fh)
        except NeedsFetch:      # a server without Range: stage the whole file
            return tifffile.TiffFile(self._io.fetch(url))

    def _page(self, tif: tifffile.TiffFile, k: int) -> tifffile.TiffPage:
        s = tif.series[0]
        if len(tif.series) != 1 or len(tif.pages) != 1 or s.axes != "YX":
            raise ValueError(f"{self._urls[k]}: tifffile axes {s.axes!r} {tuple(s.shape)}, {len(tif.pages)} "
                             "page(s); this snippet reads single-page grey YX TIFFs")
        page = tif.pages[0]
        if tuple(page.shape) != self._page_shape:
            raise ValueError(f"{self._urls[k]}: the file is {tuple(page.shape)} (height, width), its row of the "
                             f"table says {self._page_shape}")
        if k > 0 and np.dtype(page.dtype) != self._file_dtype:
            raise ValueError(f"{self._urls[k]}: {page.dtype} differs from the first file's {self._file_dtype}; "
                             "all files of one stack must agree")
        return page

    @staticmethod
    def _data_offset(page: tifffile.TiffPage) -> int | None:
        """File offset of the pixel block when it is stored raw in one run (rows can be read by range)."""
        if page.compression != 1 or page.predictor != 1 or page.fillorder != 1 or page.is_tiled:
            return None
        offsets, counts = page.dataoffsets, page.databytecounts      # strips, in row order
        size = int(np.prod(page.shape)) * np.dtype(page.dtype).itemsize
        if not offsets or len(offsets) != len(counts) or sum(counts) != size:
            return None
        if any(o + c != nxt for o, c, nxt in zip(offsets, counts, offsets[1:])):
            return None
        return int(offsets[0])

    def _plane(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
        with self._tiff(k) as tif:
            page = self._page(tif, k)
            offset = self._data_offset(page)
            if offset is None:
                return np.asarray(page.asarray()).astype(self.dtype, copy=False)[y, x]
            ny, nx = self._page_shape
            rows = range(ny)[y] if isinstance(y, slice) else range(range(ny)[y], range(ny)[y] + 1)
            if len(rows) == 0:
                return np.empty((ny, nx), self.dtype)[y, x]
            lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
            stored = np.dtype(page.dtype).newbyteorder(tif.byteorder)
            fh = tif.filehandle
            fh.seek(offset + lo * nx * stored.itemsize)
            buf = fh.read((hi - lo) * nx * stored.itemsize)
        if len(buf) != (hi - lo) * nx * stored.itemsize:
            raise ValueError(f"{self._urls[k]}: file ends inside the pixel data")
        block = np.frombuffer(buf, stored).reshape(hi - lo, nx).astype(self.dtype, copy=False)
        if not isinstance(y, slice):
            return block[0, x]
        picked = block if rows.step == 1 else block[np.arange(rows.start, rows.stop, rows.step) - lo]
        return picked[:, x]


def _quote(name: str) -> str:
    """`name` as a url path: every byte but letters, digits and `-._~/` percent-encoded."""
    return "".join(chr(b) if b in UNRESERVED else f"%{b:02X}" for b in name.encode("utf-8"))


def _rows(io: IO, table: str) -> list[tuple[str, int, int]]:
    """(file_name, width, height) of every row of the table, in row order."""
    with open(io.fetch(table), newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in (NAME, WIDTH, HEIGHT) if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{table}: no column {missing} in the header row {reader.fieldnames}; this snippet "
                             f"reads a table with the columns {NAME}, {WIDTH}, {HEIGHT}")
        rows = []
        for r in reader:
            name = r[NAME] or ""
            try:
                rows.append((name, int(r[WIDTH]), int(r[HEIGHT])))
            except (TypeError, ValueError):
                raise ValueError(f"{table} line {reader.line_num}: {WIDTH} {r[WIDTH]!r} and {HEIGHT} {r[HEIGHT]!r} "
                                 "are not both integers") from None
            parts = name.split("/")
            if not name or "\\" in name or any(p in ("", ".", "..") for p in parts) or any(ord(ch) < 32 for ch in name):
                raise ValueError(f"{table} line {reader.line_num}: {NAME} {name!r} is not a path below the table's "
                                 "folder")
    return rows


def _listed(io: IO, listing: str, table: str) -> set[str]:
    """The url paths, relative to the table's folder, of the files a Hugging Face repo-info document lists."""
    m = RESOLVE.search(table)
    if m is None:
        raise ValueError(f"a listing url needs the table at a Hugging Face file url (.../resolve/<revision>/<path>) "
                         f"to place its files in the repository, got {table!r}")
    folder = m[1][:m[1].rfind("/") + 1]      # the table's folder in the repository, as the url spells it
    try:
        doc = json.loads(io.fetch(listing).read_text(encoding="utf-8"))
        names = [s["rfilename"] for s in doc["siblings"]]
    except (ValueError, KeyError, TypeError):
        raise ValueError(f"{listing}: not a Hugging Face repo-info document (a JSON object whose `siblings` are "
                         "`{rfilename: <path in the repository>}`)") from None
    if not all(isinstance(n, str) for n in names):
        raise ValueError(f"{listing}: a `siblings` entry has an `rfilename` that is not a string")
    return {q[len(folder):] for q in map(_quote, names) if q.startswith(folder)}


def open_array(src: Source, io: IO) -> Array:
    width, height = src.params.get("width"), src.params.get("height")
    for key, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"params.{key} must be a positive integer, got {value!r}")
    tables = [u for u in src.urls if u.lower().endswith(".csv")]
    others = [u for u in src.urls if not u.lower().endswith(".csv")]
    if len(tables) != 1 or len(others) > 1:
        raise ValueError("tiff-csv-index-shape-i247 needs one url ending in .csv (the table) and at most one "
                         f"other (the listing of the files that exist), got {list(src.urls)}")
    table = tables[0]
    if "::" in table[table.find("://") + 3:]:
        raise ValueError(f"the table must be a plain file url, not a zip member: {table!r}")
    rows = _rows(io, table)
    if others:
        listed = _listed(io, others[0], table)
        kept = [r for r in rows if _quote(r[0]) in listed]
        if not kept:
            raise ValueError(f"{others[0]} lists none of the {len(rows)} files of {table}")
        rows = kept
    names = [name for name, w, h in rows if (w, h) == (width, height)]
    if not names:
        common = ", ".join(f"{w}x{h} ({n})" for (w, h), n in Counter((w, h) for _, w, h in rows).most_common(8))
        raise ValueError(f"{table}: no row with width {width} and height {height}" +
                         (" whose file is in the listing" if others else "") +
                         f"; width x height (rows) of the most frequent shapes: {common}")
    if len(set(names)) != len(names):
        twice = [n for n, count in Counter(names).items() if count > 1]
        raise ValueError(f"{table}: {len(twice)} file(s) named by more than one row, e.g. {twice[0]!r}")
    folder = table[:table.rfind("/") + 1]
    return _Stack(io, [folder + _quote(name) for name in names], (height, width))
