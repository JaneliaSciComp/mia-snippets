"""One tiled TIFF / OME-TIFF (BigTIFF or classic, compressed or not) at a plain URL, read tile by tile.

A whole-slide image is one TIFF page of tens of thousands of pixels a side; decoding the page to cut a patch
from it costs the whole file and gigabytes of memory. This snippet reads level 0 of `series[0]` by fetching and
decoding only the tiles that overlap the requested region, so a patch costs the 1 MiB blocks its tiles lie in.

The array is the series with its size-1 axes dropped; a sample axis (RGB stored as YXS) is presented as the
leading `c` of `cyx`. Tiles are decoded by tifffile's own per-page decoder, so every compression and predictor
it knows works, JPEG with the page's JPEGTables and YCbCr colour included.

Every step that makes requests is tried again when the transport fails or the server answers 404, 429 or 5xx:
some S3 buckets answer a share of the range requests for an existing object with 404.
"""
import time
from typing import BinaryIO, Callable, TypeVar

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXIS = {"Z": "z", "Q": "z", "I": "z", "C": "c", "S": "c", "Y": "y", "X": "x"}   # tifffile letter -> czyx
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}
WAITS = (0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0)     # seconds before each further try of a step that makes requests
T = TypeVar("T")


def _transient(e: Exception) -> bool:
    """A failure worth asking again for: the HTTP client's transport errors (timeouts, a connection dropped or
    cut inside a body) and 404/429/5xx answers. Told by class name, since a snippet may not import the client.
    404 is in the list because the bucket this was written for answers it for objects that exist."""
    names = {c.__name__ for c in type(e).__mro__}
    status = getattr(getattr(e, "response", None), "status_code", None)
    return "TransportError" in names or ("HTTPStatusError" in names and isinstance(status, int)
                                         and (status in (404, 429) or status >= 500))


def _retry(step: Callable[[], T]) -> T:
    """`step()`, tried again after each of WAITS while it fails in transit; any other error is raised at once."""
    for wait in (*WAITS, None):
        try:
            return step()
        except Exception as e:      # BudgetExceeded, HostNotAllowed, NeedsFetch, ValueError: not transient
            if wait is None or not _transient(e):
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


def _bounds(sel: int | slice, n: int) -> tuple[int, int, int | slice]:
    """A numpy selection on an axis of length n as (lo, hi, the same selection inside [lo, hi))."""
    if not isinstance(sel, slice):
        k = range(n)[sel]           # IndexError when out of range, like numpy
        return k, k + 1, 0
    r = range(n)[sel]
    if len(r) == 0:
        return 0, 0, slice(0, 0)
    lo, hi = min(r[0], r[-1]), max(r[0], r[-1]) + 1
    stop = r.stop - lo
    return lo, hi, slice(r.start - lo, stop if stop >= 0 else None, r.step)


class _Tiled:
    def __init__(self, url: str, tif: tifffile.TiffFile, access: str):
        self._url, self._tif, self.access = url, tif, access
        s = tif.series[0].levels[0]
        key = s.keyframe
        if not key.is_tiled:
            raise ValueError(f"{url}: series 0 is stored in strips, not tiles; tiff-tiled-http reads tiled pages "
                             "(tiff-zip-member-2 reads a stripped page whole)")
        if key.imagedepth != 1 or key.tiledepth != 1:
            raise ValueError(f"{url}: tiles with depth (image depth {key.imagedepth}, tile depth {key.tiledepth}) "
                             "are not supported")
        page = tuple(key.shape)
        n_outer = len(s.shape) - len(page)
        if tuple(s.shape[n_outer:]) != page or len(s.axes) != len(s.shape):
            raise ValueError(f"{url}: series shape {tuple(s.shape)} does not end in its page shape {page}")
        if key.axes not in ("YX", "YXS", "SYX"):
            raise ValueError(f"{url}: page axes {key.axes!r} are not YX, YXS or SYX")
        self._series, self._key = s, key
        self._outer = tuple(s.shape[:n_outer])              # the dims that index pages
        self._samples = key.shaped[0] * key.shaped[4]       # 1 for a grey page
        self._contig = key.shaped[0] == 1                   # samples inside a tile, else one tile plane each
        self._ny, self._nx = key.imagelength, key.imagewidth
        # the series as (outer..., S, Y, X): the order the array presents, with size-1 axes dropped
        letters = s.axes[:n_outer] + ("S" if "S" in key.axes else "") + "YX"
        sizes = self._outer + ((self._samples,) if "S" in key.axes else ()) + (self._ny, self._nx)
        self._has_s = "S" in key.axes
        self._kept = [i for i, (a, n) in enumerate(zip(letters, sizes)) if n > 1 or a in "YX"]
        self._full = "".join(AXIS.get(a, "?") for a in letters)
        self.axes = "".join(self._full[i] for i in self._kept)
        if "?" in self.axes or len(set(self.axes)) != len(self.axes):
            raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} do not map to distinct czyx axes")
        self.shape = tuple(sizes[i] for i in self._kept)
        self.dtype = np.dtype(s.dtype).newbyteorder("=")
        self.voxel_size_nm = _voxel_size(tif, self.axes)
        self._decode = key.decode                           # fails here, not at the first crop, for a codec
        self._jpegtables = key.jpegtables                   # tifffile cannot decode
        self._tiles: dict[int, tuple[tuple[int, ...], tuple[int, ...]]] = {}
        _retry(lambda: self._page_tiles(0))

    def _page_tiles(self, k: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """(offsets, byte counts) of the tiles of page k of the series; read once per page."""
        if k not in self._tiles:
            page = self._series[k]
            if page is None:
                raise ValueError(f"{self._url}: page {k} of the series is missing from the file")
            if page.keyframe.hash != self._key.hash:
                raise ValueError(f"{self._url}: page {k} of the series is not laid out like its first page")
            offsets, counts = tuple(page.dataoffsets), tuple(page.databytecounts)
            across, down = -(-self._nx // self._key.tilewidth), -(-self._ny // self._key.tilelength)
            want = across * down * (1 if self._contig else self._samples)
            if len(offsets) != want or len(counts) != want:
                raise ValueError(f"{self._url}: page {k} lists {len(offsets)} tiles, its size needs {want}")
            self._tiles[k] = offsets, counts
        return self._tiles[k]

    def _bytes(self, offset: int, count: int) -> bytes:
        fh: BinaryIO = self._tif.filehandle
        fh.seek(offset)
        data = fh.read(count)
        if len(data) != count:
            raise ValueError(f"{self._url}: file ends inside a tile ({count} bytes at {offset})")
        return data

    def _box(self, k: int, y0: int, y1: int, x0: int, x1: int, samples: list[int]) -> np.ndarray:
        """Rows y0..y1 and columns x0..x1 of page k as (len(samples), y1 - y0, x1 - x0), from the tiles that
        overlap them; a tile the file leaves out (byte count 0) stays 0."""
        out = np.zeros((len(samples), y1 - y0, x1 - x0), self.dtype)
        if out.size == 0:
            return out
        offsets, counts = _retry(lambda: self._page_tiles(k))
        th, tw = self._key.tilelength, self._key.tilewidth
        across, down = -(-self._nx // tw), -(-self._ny // th)
        planes = [0] if self._contig else samples       # separate samples: one run of tiles per sample
        for ty in range(y0 // th, (y1 - 1) // th + 1):
            for tx in range(x0 // tw, (x1 - 1) // tw + 1):
                for p in planes:
                    i = (p * down + ty) * across + tx
                    if counts[i] == 0 or offsets[i] == 0:
                        continue
                    data = _retry(lambda: self._bytes(offsets[i], counts[i]))
                    tile, (_, _, ty0, tx0, _), (_, h, w, _) = self._decode(data, i, jpegtables=self._jpegtables)
                    if tile is None:
                        continue
                    tile = np.asarray(tile).reshape(h, w, -1)       # (rows, columns, samples in this tile)
                    ys, xs = slice(max(y0, ty0), min(y1, ty0 + h)), slice(max(x0, tx0), min(x1, tx0 + w))
                    part = tile[ys.start - ty0:ys.stop - ty0, xs.start - tx0:xs.stop - tx0]
                    dst = (slice(ys.start - y0, ys.stop - y0), slice(xs.start - x0, xs.stop - x0))
                    if self._contig:
                        out[(slice(None), *dst)] = np.moveaxis(part[:, :, samples], 2, 0)
                    else:
                        out[(samples.index(p), *dst)] = part[:, :, 0]
        return out

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx: list[int | slice] = [0] * len(self._full)              # dropped size-1 axes are index 0
        for i in self._kept:
            idx[i] = region.get(self._full[i], slice(None))
        n = len(self._outer)
        y0, y1, ysel = _bounds(idx[-2], self._ny)
        x0, x1, xsel = _bounds(idx[-1], self._nx)
        ssel: int | slice = idx[n] if self._has_s else 0
        samples = list(np.atleast_1d(np.arange(self._samples)[ssel]))
        keys = np.arange(int(np.prod(self._outer, dtype=np.int64))).reshape(self._outer)[tuple(idx[:n])]
        tail: tuple[int | slice, ...] = (slice(None) if isinstance(ssel, slice) and self._has_s else 0, ysel, xsel)
        boxes = [self._box(int(k), y0, y1, x0, x1, samples)[tail] for k in np.ravel(keys)]
        if not boxes:       # an empty selection of pages
            return np.empty(np.shape(keys) + np.empty((len(samples), y1 - y0, x1 - x0))[tail].shape, self.dtype)
        return np.stack(boxes).reshape(np.shape(keys) + boxes[0].shape)


def _voxel_size(tif: tifffile.TiffFile, axes: str) -> dict[str, float] | None:
    """From the first OME image's PhysicalSizeX/Y/Z; None when the file is not OME or doesn't say."""
    out: dict[str, float] = {}
    if tif.is_ome and tif.ome_metadata:
        img = tifffile.xml2dict(tif.ome_metadata)["OME"]["Image"]
        px = (img[0] if isinstance(img, list) else img)["Pixels"]
        for a in "zyx":
            v, unit = px.get(f"PhysicalSize{a.upper()}"), px.get(f"PhysicalSize{a.upper()}Unit", "µm")
            if a in axes and v and unit in NM:
                out[a] = float(v) * NM[unit]
    return out or None


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"tiff-tiled-http reads one file, got {len(src.urls)} urls")
    url = src.urls[0]
    try:        # the blocks a failed try did get are cached, so a further try asks only for the rest
        return _retry(lambda: _Tiled(url, tifffile.TiffFile(io.open(url)), "chunk"))
    except NeedsFetch:      # a server without Range, or a deflated zip member: stage the whole file
        return _Tiled(url, tifffile.TiffFile(_retry(lambda: io.fetch(url))), "file")
