"""Palette PNGs (IHDR colour type 3: each pixel is an index into the file's PLTE colour table), one image per
file, from zip members or directory files chosen by glob (or listed one by one), stacked along z.

A PNG decoder replaces each index by its palette colour, which turns a one-sample mask into three samples and
loses the stored ids. This snippet returns one `uint8` plane per file instead, by a rule the binding states:

- without `values`: the stored index of each pixel, unchanged;
- `values: grey`: the palette colour of each pixel as one grey level, for a file whose colours are greys
  (R = G = B, e.g. a black and white mask); a pixel whose colour is not a grey raises.

The indices are decoded by libpng (`imagecodecs`) from a copy of the file whose header says greyscale: a palette
PNG and a greyscale PNG of the same bit depth store their pixels identically, and only the colour type in IHDR
and the PLTE chunk tell the decoder to look colours up. Files are opened lazily: `open_array` reads the first
file's 33-byte header; `read` fetches the files it needs, one file per z plane.
"""
import struct
import zlib

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("grey",)
SIGNATURE = b"\x89PNG\r\n\x1a\n"
HEADER = 33                 # signature (8) + IHDR chunk: length (4), type (4), 13 bytes of data, CRC (4)
PALETTE = 3                 # IHDR colour type: each pixel is an index into PLTE
DEPTHS = (1, 2, 4, 8)       # the bit depths a palette PNG may have


def _header(data: bytes, url: str) -> tuple[tuple[int, int], int]:
    """(height, width) and bit depth from the signature and IHDR chunk; ValueError for anything but a palette PNG."""
    if len(data) < HEADER or data[:8] != SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    if colour != PALETTE or depth not in DEPTHS:
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; this snippet reads palette PNGs "
                         "(colour type 3) only, see image-tiles-glob or png-channels for grey and colour PNGs")
    return (height, width), depth


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _split(data: bytes, url: str) -> tuple[np.ndarray, bytes]:
    """The palette as (N, 3) uint8 and the same image as a greyscale PNG: IHDR with colour type 0, the IDAT chunks
    as they are, IEND. PLTE, tRNS and every ancillary chunk are left out (in a greyscale file tRNS and bKGD would
    mean something else)."""
    palette, idat, ihdr, pos, ended = None, [], data[16:29], 8, False
    while pos + 8 <= len(data) and not ended:
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) < length:
            break
        if kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat.append(_chunk(kind, body))
        ended = kind == b"IEND"
        pos += 12 + length
    if not ended:
        raise ValueError(f"{url}: the PNG ends before its IEND chunk ({len(data)} bytes)")
    if palette is None or not palette or len(palette) % 3 or len(palette) > 768:
        raise ValueError(f"{url}: a palette PNG needs one PLTE chunk of 1 to 256 RGB entries, this file has "
                         f"{'none' if palette is None else f'{len(palette)} bytes'}")
    grey = SIGNATURE + _chunk(b"IHDR", ihdr[:9] + b"\x00" + ihdr[10:]) + b"".join(idat) + _chunk(b"IEND", b"")
    return np.frombuffer(palette, np.uint8).reshape(-1, 3), grey


class _Indexed:
    def __init__(self, io: IO, urls: list[str], values: str | None):
        self._io, self._urls, self._values = io, urls, values
        self._plane_shape, _ = _header(_data(io, urls[0], HEADER), urls[0])
        self.dtype = np.dtype("uint8")                      # an index or a grey level: one byte either way
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.shape: tuple[int, ...] = ((len(urls),) if len(urls) > 1 else ()) + self._plane_shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file, read by
        self.voxel_size_nm = None       # range (stored member, plain url) or staged (deflated member)
        self._last: tuple[int, np.ndarray] | None = None    # the file decoded last; patches of one plane reuse it

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0)[y, x]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._urls))[sel])[y, x]   # IndexError when out of range, like numpy
        planes = [self._plane(k)[y, x] for k in range(len(self._urls))[sel]]
        return np.stack(planes) if planes else np.empty((0, *self._plane_shape), self.dtype)[:, y, x]

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X) uint8: its stored indices, or their grey palette colours with `values: grey`."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        data = _data(self._io, url)
        shape, depth = _header(data, url)
        if shape != self._plane_shape:
            raise ValueError(f"{url}: {shape} differs from the first file's {self._plane_shape} (y, x); all "
                             "files of one array must agree")
        palette, grey = _split(data, url)
        plane = np.asarray(imagecodecs.png_decode(grey))
        if plane.shape != shape or plane.dtype != self.dtype:
            raise ValueError(f"{url}: decoded as {plane.dtype} {plane.shape}, its header says uint8 {shape}")
        if depth < 8:       # libpng spreads 1/2/4-bit grey over 0..255 (x255, x85, x17); undo it exactly
            plane = plane // (255 // ((1 << depth) - 1))
        used = np.flatnonzero(np.bincount(plane.ravel(), minlength=256))
        if used[-1] >= len(palette):
            raise ValueError(f"{url}: stored index {int(used[-1])} is outside the file's palette of "
                             f"{len(palette)} colours")
        if self._values == "grey":
            colours = palette[used]
            odd = np.flatnonzero((colours != colours[:, :1]).any(axis=1))
            if odd.size:
                raise ValueError(f"{url}: index {int(used[odd[0]])} has the palette colour "
                                 f"{colours[odd[0]].tolist()}, not a grey (R = G = B); drop params.values to "
                                 "read the stored indices")
            plane = palette[:, 0][plane]
        plane = np.ascontiguousarray(plane, dtype=self.dtype)
        self._last = (k, plane)
        return plane


def _data(io: IO, url: str, n: int = -1) -> bytes:
    """The first `n` bytes of the file, or all of it: by range when the server allows, else staged whole."""
    try:
        with io.open(url) as fh:
            return fh.read(n)
    except NeedsFetch:      # a deflated zip member, or a server without Range / Content-Length
        data = io.fetch(url).read_bytes()
        return data if n < 0 else data[:n]


def open_array(src: Source, io: IO) -> Array:
    values = src.params.get("values")
    if values is not None and values not in MODES:
        raise ValueError(f"params.values must be one of {MODES}, got {values!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if not urls:
        raise ValueError("png-palette-glob needs at least one url")
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return _Indexed(io, urls, values)
