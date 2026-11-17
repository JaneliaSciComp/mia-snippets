"""One palette (indexed-colour) PNG per file, from zip members or directory files chosen by glob (or listed one
by one), stacked along z. The values returned are the stored palette indices, e.g. the instance or class ids
of a mask saved as an indexed PNG, never the palette's colours.

A PNG decoder replaces each index by its palette colour. The index of a palette PNG and the sample of a
greyscale PNG of the same bit depth are stored the same way (one sample per pixel, the same filters, the same
interlacing), so each file is re-wrapped in memory as a greyscale PNG: the IHDR colour type is set from 3 to 0,
the IDAT chunks are kept byte for byte and every other chunk (PLTE, tRNS, ...) is dropped. Decoding that gives
the indices. Files are opened lazily: `open_array` reads the first file only; `read` fetches the files it needs,
one file per z plane. A zip of at most WHOLE_ZIP bytes is staged whole the first time one of its members is
needed, so its members cost one request together instead of two each.
"""
import struct
import zlib

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, HostNotAllowed, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
PALETTE, GREY = 3, 0            # IHDR colour types
DEPTHS = (1, 2, 4, 8)           # the bit depths a palette PNG may have
WHOLE_ZIP = 16 << 20            # a zip up to this size is staged whole: indexed masks are a few kB each, and a
                                # deflated member read by range costs two requests (zenodo.org answers 429 to
                                # a few hundred of them in a row)


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _as_grey(data: bytes, url: str) -> tuple[bytes, int, int, int]:
    """The palette PNG `data` as a greyscale PNG with the same pixel data, and its width, height, bit depth."""
    if data[:8] != PNG:
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    out, pos, header, pixels = [PNG], 8, None, False
    while True:
        if pos + 12 > len(data):
            raise ValueError(f"{url}: PNG ends at byte {len(data)} without an IEND chunk (truncated?)")
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + n
        if end > len(data):
            raise ValueError(f"{url}: PNG chunk {kind!r} at byte {pos} runs past the end of the file (truncated?)")
        if header is None:
            if kind != b"IHDR" or n != 13:
                raise ValueError(f"{url}: PNG does not start with a 13-byte IHDR chunk")
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data[pos + 8:pos + 21])
            if colour != PALETTE or depth not in DEPTHS:
                raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; this snippet reads palette "
                                 "PNGs (colour type 3) only, see image-tiles-glob for greyscale and colour PNGs")
            header = (width, height, depth)
            out.append(_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, GREY, compression,
                                                   filtering, interlace)))
        elif kind == b"IDAT":
            out.append(data[pos:end])       # byte for byte, with its own CRC: the decoder checks it
            pixels = True
        elif kind == b"IEND":
            break
        pos = end
    if not pixels:
        raise ValueError(f"{url}: PNG has no IDAT chunk")
    out.append(_chunk(b"IEND", b""))
    return b"".join(out), *header


def _indices(data: bytes, url: str) -> np.ndarray:
    """The stored palette indices of one palette PNG as (Y, X) uint8."""
    grey, width, height, depth = _as_grey(data, url)
    a = np.asarray(imagecodecs.png_decode(grey))
    if a.shape != (height, width) or a.dtype != np.uint8:
        raise ValueError(f"{url}: decoded to {a.dtype} {a.shape}, not uint8 {(height, width)} as its header says")
    if depth < 8:
        # the decoder stretches a 1/2/4-bit grey sample to 0..255 (2-bit 0,1,2,3 -> 0,85,170,255): undo it exactly
        step = 255 // ((1 << depth) - 1)
        if np.any(a % step):
            raise ValueError(f"{url}: decoded {depth}-bit samples are not multiples of {step}, so the stored "
                             "indices cannot be recovered")
        a = a // step
    return a


class _Planes:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._zips: set[str] = set()                        # the zips already looked at by _stage_small_zip
        self._last: tuple[int, np.ndarray] | None = None    # the last file decoded
        first = self._load(0)                               # (Y, X); every file must match
        self._plane_shape, self.dtype = first.shape, np.dtype(np.uint8)
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + first.shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file, read by
        self.voxel_size_nm = None       # range (stored member, plain url) or staged (deflated member)
        self._last = (0, first)

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
        """File k as (Y, X), checked against file 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape:
            raise ValueError(f"{self._urls[k]}: {a.shape} differs from the first file's {self._plane_shape} "
                             "(y, x); all files of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        url = self._urls[k]
        self._stage_small_zip(url)
        try:
            with self._io.open(url) as fh:
                data = fh.read()
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            data = self._io.fetch(url).read_bytes()
        return _indices(data, url)

    def _stage_small_zip(self, url: str) -> None:
        """Stage the zip that holds member `url` whole, once, when it is at most WHOLE_ZIP bytes: `io` then reads
        its members from that copy. Only a saving, so a zip that cannot be staged is read member by member."""
        i = url.find("::", url.find("/", url.find("://") + 3))
        if i < 0 or url[:i] in self._zips:
            return
        self._zips.add(url[:i])
        try:
            with self._io.open(url[:i]) as fh:      # no bytes: the size is the HEAD's
                size = fh.seek(0, 2)
            if size <= WHOLE_ZIP:
                self._io.fetch(url[:i])
        except (NeedsFetch, HostNotAllowed):        # no Content-Length, or a job reading from its cache alone
            pass


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
    return _Planes(io, _expand(src.urls, io))
