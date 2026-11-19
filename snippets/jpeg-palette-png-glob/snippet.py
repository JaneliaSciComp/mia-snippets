"""One image per file, JPEG or palette PNG (told apart by the file's first bytes), from zip members or directory
files chosen by glob (or listed one by one), stacked along z.

A JPEG is read as the decoder gives it: grey as one plane, colour as a `c` axis (`zcyx`). A palette PNG (colour
type 3) is read as one `uint8` plane whose values the binding chooses: `palette: index`, the stored palette
indices, or `palette: grey`, the palette colour of each pixel as one grey value (every colour the file uses
must have R = G = B). The indices are decoded by libpng from the file's own IDAT stream under a greyscale
header, so they are the stored ones whatever the palette says.

Planes are in glob order (sorted by name) unless `pair_urls` is given: then plane k is the file whose stem,
less `pair_suffix`, is the stem of file k of `pair_urls` (the raw array's urls), and open fails unless the two
sets of stems match one to one.

`open_array` reads the first file only; `read` fetches the files it needs, one file per z plane.
"""
import struct
import zlib

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("index", "grey")
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
PALETTE = 3                     # IHDR colour type: each sample is an index into the PLTE chunk
GREY = 0                        # IHDR colour type: one sample per pixel
DEPTHS = (1, 2, 4, 8)           # the bit depths a palette PNG may have


def _chunks(data: bytes, url: str) -> list[tuple[bytes, bytes, bytes]]:
    """The PNG's chunks up to IEND as (type, body, the whole chunk with length and CRC)."""
    out, pos = [], len(PNG)
    while pos + 12 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + n
        if end > len(data):
            break
        out.append((kind, data[pos + 8:pos + 8 + n], data[pos:end]))
        if kind == b"IEND":
            return out
        pos = end
    raise ValueError(f"{url}: a truncated PNG (no IEND chunk)")


def _palette_png(data: bytes, url: str, mode: str | None) -> np.ndarray:
    """A palette PNG as (Y, X) uint8: its stored indices, or the grey value of each index's palette colour."""
    chunks = _chunks(data, url)
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise ValueError(f"{url}: a PNG without an IHDR chunk first")
    width, height, depth, colour = struct.unpack(">IIBB", chunks[0][1][:10])
    if colour != PALETTE or depth not in DEPTHS:
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; this snippet reads palette "
                         "(colour type 3) PNGs only, see image-tiles-glob for grey and colour PNGs")
    if mode is None:
        raise ValueError(f"{url}: a palette PNG; params.palette must say which value a pixel gets, one of "
                         f"{MODES} (the stored index, or the palette colour as one grey value)")
    # The same IDAT stream under a greyscale header: libpng undoes the filters and the interlacing and returns
    # the samples, which are the indices. Every other chunk (PLTE, tRNS, bKGD, ...) is left out.
    ihdr = b"IHDR" + chunks[0][1][:9] + bytes([GREY]) + chunks[0][1][10:]
    grey = b"".join([PNG, struct.pack(">I", 13), ihdr, struct.pack(">I", zlib.crc32(ihdr)),
                     *(whole for kind, _, whole in chunks if kind in (b"IDAT", b"IEND"))])
    idx = np.asarray(imagecodecs.png_decode(grey))
    if idx.shape != (height, width) or idx.dtype != np.uint8:
        raise ValueError(f"{url}: indices decoded as {idx.dtype} {idx.shape}, the header says uint8 "
                         f"{(height, width)}")
    step = 255 // ((1 << depth) - 1)        # libpng spreads 1/2/4-bit grey over 0..255: 2-bit 0..3 is 0,85,170,255
    if step > 1:
        if (idx % step).any():
            raise ValueError(f"{url}: {depth}-bit samples decoded to values that are not multiples of {step}")
        idx = idx // step
    if mode == "index":
        return idx
    plte = [body for kind, body, _ in chunks if kind == b"PLTE"]
    if len(plte) != 1 or not plte[0] or len(plte[0]) % 3:
        raise ValueError(f"{url}: a palette PNG without one valid PLTE chunk")
    colours = np.frombuffer(plte[0], np.uint8).reshape(-1, 3)
    used = np.flatnonzero(np.bincount(idx.ravel(), minlength=256))
    if used[-1] >= len(colours):
        raise ValueError(f"{url}: pixel index {int(used[-1])} is outside the {len(colours)}-colour palette")
    for i in used:
        r, g, b = (int(v) for v in colours[i])
        if not r == g == b:
            raise ValueError(f"{url}: palette index {int(i)} is the colour ({r}, {g}, {b}), not a grey, so its "
                             "pixels have no grey value; use palette: index for the stored indices")
    alpha = b"".join(body for kind, body, _ in chunks if kind == b"tRNS")
    clear = [int(i) for i in used if i < len(alpha) and alpha[i] != 255]
    if clear:
        raise ValueError(f"{url}: palette indices {clear[:8]} are transparent (tRNS chunk), so their pixels "
                         "have no single grey value; use palette: index for the stored indices")
    lut = np.zeros(256, np.uint8)
    lut[:len(colours)] = colours[:, 0]
    return lut[idx]


def _decode(data: bytes, url: str, mode: str | None) -> np.ndarray:
    """The file's one image as (Y, X) or (C, Y, X), by its magic bytes (a zip member's name is not trusted)."""
    if data[:8] == PNG:
        return _palette_png(data, url, mode)
    if data[:3] != JPEG:
        raise ValueError(f"{url}: not a JPEG or PNG file: header={data[:8]!r}")
    if mode is not None:
        raise ValueError(f"{url}: a JPEG; params.palette is for palette PNG files, drop it")
    a = np.asarray(imagecodecs.jpeg_decode(data))
    if a.ndim == 3:
        a = np.ascontiguousarray(np.moveaxis(a, 2, 0))      # (Y, X, S) -> (C, Y, X), c in the file's sample order
    if a.ndim not in (2, 3):
        raise ValueError(f"{url}: decoded to shape {a.shape}, not (Y, X) or (Y, X, S)")
    return a


class _Tiles:
    def __init__(self, io: IO, urls: list[str], mode: str | None):
        self._io, self._urls, self._mode = io, urls, mode
        first = self._load(0)                               # (Y, X) or (C, Y, X); every file must match
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self.axes = ("z" if len(urls) > 1 else "") + ("cyx" if first.ndim == 3 else "yx")
        self.shape: tuple[int, ...] = ((len(urls),) if len(urls) > 1 else ()) + first.shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file
        self.voxel_size_nm = None       # JFIF density and pHYs describe print size, not the pixel size
        self._last: tuple[int, np.ndarray] = (0, first)     # the last file decoded: c planes are read one by one

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes if k != "z")
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
        """File k as (Y, X) or (C, Y, X), checked against file 0."""
        if self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._urls[k]}: {a.dtype} {a.shape} differs from the first file's "
                             f"{self.dtype} {self._plane_shape}; all files of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        url = self._urls[k]
        try:
            with self._io.open(url) as fh:
                data = fh.read()
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            data = self._io.fetch(url).read_bytes()
        return _decode(data, url, self._mode)


def _expand(urls: list[str], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if not out:
        raise ValueError("no urls")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def _stem(url: str) -> str:
    """The file's name without its directories and its last extension."""
    name = url.rsplit("::", 1)[-1].rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name[1:] else name


def _by_stem(urls: list[str], suffix: str, what: str) -> dict[str, str]:
    """stem (less `suffix`, which every stem must end with) -> url; two files with one stem raise."""
    out: dict[str, str] = {}
    for url in urls:
        stem = _stem(url)
        if not stem.endswith(suffix):
            raise ValueError(f"{url}: the file's stem does not end with params.pair_suffix {suffix!r}")
        key = stem[:len(stem) - len(suffix)]
        if key in out:
            raise ValueError(f"{what}: {out[key]} and {url} have the same stem {key!r}, so stems do not pair "
                             "files one to one")
        out[key] = url
    return out


def _paired(urls: list[str], others: list[str], suffix: str) -> list[str]:
    """`urls` in the order of `others`: the file whose stem less `suffix` is the stem of others[k], for each k."""
    mine, theirs = _by_stem(urls, suffix, "urls"), _by_stem(others, "", "params.pair_urls")
    missing = [k for k in theirs if k not in mine]
    extra = [k for k in mine if k not in theirs]
    if missing or extra:
        raise ValueError(f"the stems of urls (less {suffix!r}) and of params.pair_urls do not pair one to one: "
                         f"{len(missing)} of pair_urls have no file here (first {missing[:3]}), {len(extra)} "
                         f"files here have none in pair_urls (first {extra[:3]})")
    return [mine[k] for k in theirs]


def open_array(src: Source, io: IO) -> Array:
    mode, pair, suffix = (src.params.get(k) for k in ("palette", "pair_urls", "pair_suffix"))
    if mode is not None and mode not in MODES:
        raise ValueError(f"params.palette must be one of {MODES}, got {mode!r}")
    if suffix is not None and (not isinstance(suffix, str) or pair is None):
        raise ValueError("params.pair_suffix must be a string and needs params.pair_urls")
    urls = _expand(list(src.urls), io)
    if pair is not None:
        if not isinstance(pair, list) or not pair or not all(isinstance(u, str) for u in pair):
            raise ValueError(f"params.pair_urls must be a list of urls, got {pair!r}")
        urls = _paired(urls, _expand(pair, io), suffix or "")
    return _Tiles(io, urls, mode)
