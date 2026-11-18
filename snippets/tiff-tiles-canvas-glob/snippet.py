"""Single-page TIFF tiles of differing height and width, chosen by glob (or listed one by one), one file per z
plane. An array has one shape, so every plane is `params.canvas` (height, width): a file's pixels sit unchanged
in the centre of its plane and the rest of the plane is 0. Nothing is cropped or resampled: a file larger than
the canvas raises.

`pair` keeps only the files that also exist, under the same name, in a partner folder (crops/ and masks/), so a
raw array and its label array hold the same files in the same order even when one listing lacks a file. `skip`
leaves named files out (e.g. a 0-byte file, or its partner), `count` pins the number of planes, `samples: grey`
returns one plane from a grey image stored as RGB, and `dtype` returns whole numbers stored as floats (label ids
in a float32 mask) as unsigned integers, checked to be the same numbers.

Files are opened lazily: `open_array` lists the files and reads the first one; `read` fetches the files it needs.
"""
from io import BytesIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
SAMPLES = ("grey",)
DTYPES = ("uint8", "uint16", "uint32")


def _name(url: str) -> str:
    """The file name of a plain or member url: what follows its last `/` (or the `::` of a top-level member)."""
    return url.rsplit("/", 1)[-1].rsplit("::", 1)[-1]


def _swap(url: str, pair: tuple[str, str]) -> str:
    """`url` with the one occurrence of pair[0] replaced by pair[1]: the same file, or glob, in the partner folder."""
    if url.count(pair[0]) != 1:
        raise ValueError(f"{url}: params.pair[0] {pair[0]!r} must occur exactly once in the url, found "
                         f"{url.count(pair[0])} times")
    return url.replace(pair[0], pair[1])


def _files(urls: tuple[str, ...], io: IO, pair: tuple[str, str] | None, skip: list[str],
           count: int | None) -> list[str]:
    """The array's files in plane order: each url expanded (a glob's files sorted by name) in binding order, minus
    `skip`, minus the files whose partner url is not among the partner folder's files."""
    own = [u for url in urls for u in io.expand(url)]
    if len(set(own)) != len(own):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    partner = [u for url in urls for u in io.expand(_swap(url, pair))] if pair else []
    for s in skip:
        hits = [sum(_name(u) == s for u in side) for side in (own, partner)]
        if max(hits) > 1:
            raise ValueError(f"params.skip {s!r} is the name of several files of this array; bind one folder per "
                             "array, so that a name is one file")
        if not any(hits):
            raise ValueError(f"params.skip {s!r} is not the name of a file of this array"
                             + (" or of its partner folder" if pair else ""))
    kept = [u for u in own if _name(u) not in skip]
    if pair:
        there = set(partner)
        kept = [u for u in kept if _swap(u, pair) in there]
    if not kept:
        raise ValueError("no file is left after params.skip and params.pair")
    if count is not None and len(kept) != count:
        raise ValueError(f"the urls stand for {len(kept)} files after params.skip and params.pair, not "
                         f"params.count {count}: the listing changed, or the binding is wrong")
    return kept


class _Tiles:
    axes = "zyx"
    access = "plane"            # one file per z plane: any region of it costs the whole file
    voxel_size_nm = None        # TIFF resolution fields of such tiles describe print size

    def __init__(self, io: IO, urls: list[str], canvas: tuple[int, int], samples: str | None, dtype: str | None):
        self._io, self._urls, self._canvas, self._samples, self._dtype = io, urls, canvas, samples, dtype
        self.shape = (len(urls), *canvas)
        self._last: tuple[int, np.ndarray] | None = None    # the last plane built
        self.dtype = np.dtype(self._tile(0).dtype)          # the first file's, or params.dtype

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = (region.get("y", slice(None)), region.get("x", slice(None)))
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._urls))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._urls))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._canvas, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """File k centred on a canvas of zeros."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        tile, url = self._tile(k), self._urls[k]
        if tile.dtype != self.dtype:
            raise ValueError(f"{url}: dtype {tile.dtype} differs from the first file's {self.dtype}; all files of "
                             "one array must have one dtype")
        (h, w), (H, W) = tile.shape, self._canvas
        if h > H or w > W:
            raise ValueError(f"{url}: the file is {h}x{w} (height x width), larger than params.canvas {H}x{W}; "
                             "nothing is cropped, so give a canvas that holds the largest file of the array")
        plane = np.zeros(self._canvas, self.dtype)
        y, x = (H - h) // 2, (W - w) // 2
        plane[y:y + h, x:x + w] = tile
        self._last = (k, plane)
        return plane

    def _tile(self, k: int) -> np.ndarray:
        """File k as stored, (Y, X): after `samples`, and in `dtype` when the binding names one."""
        url = self._urls[k]
        try:
            with self._io.open(url) as fh:
                data = fh.read()
        except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
            data = self._io.fetch(url).read_bytes()
        if not data:
            raise ValueError(f"{url}: the file is empty (0 bytes); name it in params.skip of this array and of "
                             "the array it pairs with")
        if data[:4] not in TIFF:
            raise ValueError(f"{url}: not a TIFF file: header={data[:8]!r}")
        with tifffile.TiffFile(BytesIO(data)) as tif:
            s = tif.series[0]
            if len(tif.series) != 1 or s.axes not in ("YX", "YXS"):
                raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.series)} series; "
                                 "this snippet reads single-page YX or YXS tiles only")
            a = np.asarray(s.asarray())
        if a.ndim == 3:
            if self._samples != "grey":
                raise ValueError(f"{url}: the file has {a.shape[2]} samples per pixel; this snippet has no c axis. "
                                 "Set params.samples: grey when the samples are identical copies")
            if not np.array_equal(a, np.broadcast_to(a[:, :, :1], a.shape)):
                raise ValueError(f"{url}: samples differ, so this is not a grey image stored as RGB")
            a = a[:, :, 0]
        if self._dtype is not None:
            with np.errstate(invalid="ignore"):     # a NaN or a number outside the dtype fails the comparison
                b = a.astype(self._dtype)
            if not np.array_equal(b, a):
                raise ValueError(f"{url}: {a.dtype} values from {a.min()} to {a.max()} are not all whole numbers "
                                 f"that {self._dtype} holds, so params.dtype {self._dtype} would change them")
            a = b
        return a


def _strings(value: object, what: str, n: int | None = None) -> list[str]:
    ok = isinstance(value, list) and all(isinstance(v, str) and v for v in value) and len(set(value)) == len(value)
    if not ok or (n is not None and len(value) != n):
        raise ValueError(f"params.{what} must be a list of {'' if n is None else f'{n} '}different non-empty "
                         f"strings, got {value!r}")
    return list(value)


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    canvas = p.get("canvas")
    if not (isinstance(canvas, list) and len(canvas) == 2 and all(_int(v) for v in canvas)):
        raise ValueError(f"params.canvas must be [height, width], two positive whole numbers, got {canvas!r}")
    if p.get("samples") is not None and p["samples"] not in SAMPLES:
        raise ValueError(f"params.samples must be one of {SAMPLES}, got {p['samples']!r}")
    if p.get("dtype") is not None and p["dtype"] not in DTYPES:
        raise ValueError(f"params.dtype must be one of {DTYPES}, got {p['dtype']!r}")
    if p.get("count") is not None and not _int(p["count"]):
        raise ValueError(f"params.count must be a positive whole number, got {p['count']!r}")
    pair = _strings(p["pair"], "pair", 2) if p.get("pair") is not None else None
    skip = _strings(p["skip"], "skip") if p.get("skip") is not None else []
    files = _files(src.urls, io, (pair[0], pair[1]) if pair else None, skip, p.get("count"))
    return _Tiles(io, files, (canvas[0], canvas[1]), p.get("samples"), p.get("dtype"))
