"""One colour-coded mask PNG per file (8-bit RGB, each class painted in one colour), from plain URLs, zip members
or files chosen by glob, stacked along z. Every pixel's colour is replaced by its class id: the position of that
colour in the binding's `colours` table, so the array is one integer plane per file (`yx` / `zyx`) and never the
R, G, B samples.

The table is the binding's, not collected from the files: the same colour then has the same id in every file,
whichever colours a file happens to hold. A pixel whose colour the table does not list raises at `read`, naming
the colours, so an incomplete table is never read as a class. `open_array` reads the first file's header only;
`read` fetches and decodes the files it needs, one file per z plane.
"""
import struct

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
HEADER = 33             # the signature (8 bytes) and the IHDR chunk (length 4, type 4, data 13, CRC 4)
RGB = 2                 # IHDR colour type of a truecolour PNG without alpha
MAX_COLOURS = 256       # ids are uint8
UNLISTED = 0xFFFF       # the table's value for a colour params.colours does not list
STRIP = 1 << 22         # pixels mapped at a time, so a large mask costs its decoded samples and little more
SHOWN = 16              # unlisted colours named in the error; a file with more is not a colour-coded mask


def _keys(colours: object) -> list[int]:
    """params.colours as one 24-bit number per colour (r << 16 | g << 8 | b), in id order."""
    if not isinstance(colours, list) or not 1 <= len(colours) <= MAX_COLOURS:
        raise ValueError(f"params.colours must be a list of 1 to {MAX_COLOURS} [r, g, b] colours, id 0 first, "
                         f"got {colours!r}")
    keys = []
    for k, c in enumerate(colours):
        if not (isinstance(c, list) and len(c) == 3 and all(type(v) is int and 0 <= v <= 255 for v in c)):
            raise ValueError(f"params.colours[{k}] must be [r, g, b] with integers 0..255, got {c!r}")
        keys.append(c[0] << 16 | c[1] << 8 | c[2])
    if len(set(keys)) != len(keys):
        twice = sorted({k for k in keys if keys.count(k) > 1})
        raise ValueError("params.colours lists a colour twice: " + ", ".join(_rgb(k) for k in twice))
    return keys


def _rgb(key: int) -> str:
    return f"[{key >> 16}, {key >> 8 & 255}, {key & 255}]"


def _plane_shape(head: bytes, url: str) -> tuple[int, int]:
    """(height, width) from the first HEADER bytes of an 8-bit RGB PNG; any other file raises."""
    if head[:8] != PNG:
        raise ValueError(f"{url}: not a PNG file: header={head[:8]!r}")
    if len(head) < HEADER or head[8:16] != b"\x00\x00\x00\rIHDR":
        raise ValueError(f"{url}: PNG does not start with a 13-byte IHDR chunk")
    width, height, depth, colour = struct.unpack(">IIBB", head[16:26])
    if colour != RGB or depth != 8:
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; this snippet reads 8-bit RGB "
                         "masks (colour type 2) only. A palette PNG's ids are its stored indices "
                         "(png-palette-index-glob); a greyscale PNG's are its values (image-tiles-glob)")
    return height, width


class _ClassPlanes:
    def __init__(self, io: IO, urls: list[str], keys: list[int]):
        self._io, self._urls = io, urls
        self._table = np.full(1 << 24, UNLISTED, np.uint16)     # 24-bit colour -> id
        self._table[keys] = np.arange(len(keys), dtype=np.uint16)
        self._last: tuple[int, np.ndarray] | None = None        # the last file decoded, as ids
        self._plane_shape = _plane_shape(self._head(urls[0]), urls[0])     # every file must match
        self.dtype = np.dtype(np.uint8)
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + self._plane_shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file, read by
        self.voxel_size_nm = None       # range (stored member, plain url) or staged (deflated member)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in "yx")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._urls))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._urls))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X) class ids."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        try:
            with self._io.open(url) as fh:
                data = fh.read()
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            data = self._io.fetch(url).read_bytes()
        shape = _plane_shape(data[:HEADER], url)
        if shape != self._plane_shape:
            raise ValueError(f"{url}: {shape} differs from the first file's {self._plane_shape} (y, x); all "
                             "files of one array must agree")
        a = np.asarray(imagecodecs.png_decode(data))
        if a.shape != shape + (3,) or a.dtype != np.uint8:
            raise ValueError(f"{url}: decoded to {a.dtype} {a.shape}, not uint8 {shape + (3,)} as its header "
                             "says (a tRNS chunk adds an alpha sample, which this snippet does not read)")
        ids = self._ids(a, url)
        self._last = (k, ids)
        return ids

    def _head(self, url: str) -> bytes:
        try:
            with self._io.open(url) as fh:
                return fh.read(HEADER)
        except NeedsFetch:
            with self._io.fetch(url).open("rb") as fh:
                return fh.read(HEADER)

    def _ids(self, a: np.ndarray, url: str) -> np.ndarray:
        """(Y, X, 3) colours as (Y, X) ids; raises when a colour is not in the table."""
        out = np.empty(a.shape[:2], np.uint8)
        unlisted: dict[int, int] = {}       # colour -> pixels
        rows = max(1, STRIP // max(1, a.shape[1]))
        for y in range(0, a.shape[0], rows):
            s = a[y:y + rows]
            packed = s[:, :, 0].astype(np.uint32) << 16
            packed |= s[:, :, 1].astype(np.uint32) << 8
            packed |= s[:, :, 2]
            ids = self._table[packed]
            bad = ids == UNLISTED
            if bad.any():
                for key, n in zip(*np.unique(packed[bad], return_counts=True)):
                    unlisted[int(key)] = unlisted.get(int(key), 0) + int(n)
                if len(unlisted) > SHOWN:
                    break
            out[y:y + rows] = ids.astype(np.uint8)
        if len(unlisted) > SHOWN:
            raise ValueError(f"{url}: more than {SHOWN} colours that params.colours does not list (so far "
                             f"{len(unlisted)}, e.g. {', '.join(_rgb(k) for k in sorted(unlisted)[:4])}); this "
                             "does not look like a colour-coded mask with one colour per class")
        if unlisted:
            named = ", ".join(f"{_rgb(k)} ({n} px)" for k, n in sorted(unlisted.items()))
            raise ValueError(f"{url}: {sum(unlisted.values())} of {out.size} pixels have a colour that "
                             f"params.colours does not list: {named}. Append each to the table (the ids "
                             "already given keep their meaning)")
        return out


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if not out:
        raise ValueError("the urls name no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    return _ClassPlanes(io, _expand(src.urls, io), _keys(src.params.get("colours")))
