"""One page (pyramid level) of a tiled, compressed TIFF at a plain URL, read tile by tile: a whole-slide image
that is far too large to decode whole (a 44544x47616 RGB page is 6.4 GB) is read by range requests for the
tiles a region overlaps, and nothing else.

`open_array` parses the file header and the IFD chain up to page `params.level` (0, the first page, by
default) and reads no pixel data. `read` fetches the encoded segments (tiles, or strips) that overlap the
requested y/x box, decodes each with tifffile's own segment decoder (JPEG with the page's shared JPEGTables,
YCbCr to RGB, any other codec tifffile knows) and returns what `page.asarray()[region]` would. An RGB page
(tifffile axes YXS) is `cyx` with c the sample axis; a grey page (YX) is `yx`. A segment the file does not
store (byte count 0) reads as the page's nodata value, 0 unless the file says otherwise, as in tifffile.

The urls stand for one file. A glob over an HTTP directory index (`<dir url>/::<glob>`) that matches several
files needs `params.index`, the 0-based position of the file among the sorted matches: the slides of a
collection differ in shape, so each is its own array.

Every step that makes requests is tried again when the transport fails (a timeout, a dropped or cut
connection, a 5xx answer): the harness's client gives up after 60 s, and one slow answer among the many a
crop job needs would otherwise end the job.
"""
import time
from typing import Callable, TypeVar

import numpy as np
import tifffile

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

ATTEMPTS = 4                             # tries per step that makes requests
T = TypeVar("T")


def _transient(e: Exception) -> bool:
    """A failure worth asking again for: the HTTP client's transport errors (timeouts, a connection dropped or
    cut inside a body) and 5xx/429 answers. Told by class name, since a snippet may not import the client."""
    names = {c.__name__ for c in type(e).__mro__}
    status = getattr(getattr(e, "response", None), "status_code", None)
    return "TransportError" in names or ("HTTPStatusError" in names and isinstance(status, int)
                                         and (status >= 500 or status == 429))


def _retry(step: Callable[[], T]) -> T:
    """`step()`, tried up to ATTEMPTS times while it fails in transit; any other error is raised at once."""
    for attempt in range(ATTEMPTS):
        try:
            return step()
        except Exception as e:      # BudgetExceeded, HostNotAllowed, NeedsFetch, ValueError: not transient
            if attempt == ATTEMPTS - 1 or not _transient(e):
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _picked(sel: int | slice, n: int) -> tuple[range, bool]:
    """The indices `sel` picks on an axis of length n, and whether the axis stays in the result (a slice) or
    is dropped (an integer; IndexError when out of range, like numpy)."""
    if isinstance(sel, slice):
        return range(n)[sel], True
    i = range(n)[sel]
    return range(i, i + 1), False


class _Level:
    def __init__(self, io: IO, url: str, level: int):
        self._io, self._url, self._level = io, url, level
        self._page_shape, self.dtype = _retry(self._header)
        ny, nx = self._page_shape[:2]
        self._samples: int = self._page_shape[2] if len(self._page_shape) == 3 else 0     # 0: a grey page
        self.shape: tuple[int, ...] = (self._samples, ny, nx) if self._samples else (ny, nx)
        self.axes = "cyx" if self._samples else "yx"
        self.voxel_size_nm = None       # plain TIFF resolution tags describe print size, not pixels
        self.access = "chunk"           # a y/x box costs the segments it overlaps

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self._page_shape[:2]
        rows, keep_y = _picked(region.get("y", slice(None)), ny)
        cols, keep_x = _picked(region.get("x", slice(None)), nx)
        chans, keep_c = _picked(region.get("c", slice(None)), self._samples) if self._samples else (range(1), False)
        if len(rows) == 0 or len(cols) == 0 or len(chans) == 0:
            out = np.empty((len(chans), len(rows), len(cols)), self.dtype)
        else:
            y0, y1 = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
            x0, x1 = min(cols[0], cols[-1]), max(cols[0], cols[-1]) + 1
            box = _retry(lambda: self._box(y0, y1, x0, x1))         # (y1 - y0, x1 - x0, samples or 1)
            if rows.step != 1:
                box = box[np.asarray(rows) - y0]
            if cols.step != 1:
                box = box[:, np.asarray(cols) - x0]
            out = np.moveaxis(box, 2, 0)
            if chans != range(out.shape[0]):
                out = out[list(chans)]
        if not self._samples:
            return out[0][tuple(slice(None) if keep else 0 for keep in (keep_y, keep_x))]
        return out[tuple(slice(None) if keep else 0 for keep in (keep_c, keep_y, keep_x))]

    def _tiff(self) -> tifffile.TiffFile:
        return tifffile.TiffFile(self._io.open(self._url))      # NeedsFetch (no ranges) is not caught: a slide
                                                                # staged whole is what this snippet is not for

    def _page(self, tif: tifffile.TiffFile) -> tifffile.TiffPage:
        """Page `level` of the file, checked to be one this snippet reads segment by segment."""
        try:
            page = tif.pages[self._level]
        except IndexError:
            raise ValueError(f"{self._url}: params.level {self._level} is past the file's last page "
                             f"(it has {len(tif.pages)})") from None
        if not isinstance(page, tifffile.TiffPage) or page.axes not in ("YX", "YXS"):
            raise ValueError(f"{self._url}: page {self._level} has tifffile axes {getattr(page, 'axes', None)!r} "
                             f"{tuple(page.shape)}; this snippet reads grey (YX) or RGB (YXS) pages only")
        if page.planarconfig != 1 and page.samplesperpixel > 1:
            raise ValueError(f"{self._url}: page {self._level} stores its samples in separate planes "
                             "(PlanarConfiguration 2); this snippet reads interleaved samples only")
        if len(page.dataoffsets) != len(page.databytecounts):
            raise ValueError(f"{self._url}: page {self._level} has {len(page.dataoffsets)} segment offsets and "
                             f"{len(page.databytecounts)} byte counts")
        return page

    def _header(self) -> tuple[tuple[int, ...], np.dtype]:
        with self._tiff() as tif:
            page = self._page(tif)
            return tuple(page.shape), np.dtype(page.dtype)

    def _box(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
        """Rows y0..y1 and columns x0..x1 of the page as (rows, columns, samples), from the segments they overlap."""
        with self._tiff() as tif:
            page = self._page(tif)
            if tuple(page.shape) != self._page_shape or np.dtype(page.dtype) != self.dtype:
                raise ValueError(f"{self._url}: page {self._level} is now {page.dtype} {tuple(page.shape)}, it "
                                 f"was {self.dtype} {self._page_shape} at open; the file changed")
            ny, nx = self._page_shape[:2]
            th, tw = page.chunks[:2]                # tile, or strip (rows per strip x the page's width)
            across = -(-nx // tw)                   # segments per row of segments
            out = np.full((y1 - y0, x1 - x0, self._samples or 1), page.nodata, self.dtype)
            fh = tif.filehandle
            for ty in range(y0 // th, (y1 - 1) // th + 1):
                for tx in range(x0 // tw, (x1 - 1) // tw + 1):
                    index = ty * across + tx
                    offset, count = page.dataoffsets[index], page.databytecounts[index]
                    if count == 0:                  # a segment the file does not store: nodata
                        continue
                    fh.seek(offset)
                    data = fh.read(count)
                    if len(data) != count:
                        raise ValueError(f"{self._url}: file ends inside segment {index} of page {self._level}")
                    segment, (_, _, sy, sx, _), (_, h, w, _) = page.decode(data, index, jpegtables=page.jpegtables)
                    if (sy, sx) != (ty * th, tx * tw):
                        raise ValueError(f"{self._url}: segment {index} of page {self._level} is at y {sy}, "
                                         f"x {sx}, not at y {ty * th}, x {tx * tw} where its index puts it")
                    h, w = min(h, ny - sy), min(w, nx - sx)         # an edge segment is stored padded
                    a, b = max(y0, sy), min(y1, sy + h)
                    c, d = max(x0, sx), min(x1, sx + w)
                    out[a - y0:b - y0, c - x0:d - x0] = np.asarray(segment).reshape(
                        segment.shape[1], segment.shape[2], -1)[a - sy:b - sy, c - sx:d - sx]
        return out


def _int(params: dict, name: str) -> int | None:
    value = params.get(name)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"params.{name} must be an integer >= 0, got {value!r}")
    return value


def _file(src: Source, io: IO) -> str:
    """The one file the urls stand for: their only match, or match `params.index` of the sorted matches."""
    index = _int(src.params, "index")
    files = [f for url in src.urls for f in _retry(lambda: io.expand(url))]
    if not files:
        raise ValueError("tiff-pyramid-tiles needs a url, and a glob has to match a file")
    if len(set(files)) != len(files):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    if index is None:
        if len(files) > 1:
            raise ValueError(f"the urls stand for {len(files)} files and one array is one file: set params.index "
                             f"(0..{len(files) - 1}, the position among the sorted matches) or bind one url")
        return files[0]
    if index >= len(files):
        raise ValueError(f"params.index {index} is out of range: the urls stand for {len(files)} file(s)")
    return files[index]


def open_array(src: Source, io: IO) -> Array:
    return _Level(io, _file(src, io), _int(src.params, "level") or 0)
