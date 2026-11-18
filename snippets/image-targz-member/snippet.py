"""One image, a single-page TIFF or a PNG told apart by its first bytes, that is a member of a gzip-compressed tar
archive: `yx`, or `cyx` when the file has samples. A PNG is returned as the samples it stores, so a palette PNG
gives its palette indices (the class ids of a mask) and a 1/2/4-bit PNG its small integers, not colours or values
rescaled to 0..255. A member that is a tar hard link or symlink is read from the member it points to.

A tar.gz has no index and cannot be read by range, so a member is reached by inflating the archive from its first
byte: the compressed bytes up to the member's end come through `io.open` (range requests, cached in blocks, counted
against the job's cap) and nothing behind it is asked for. The archive is never fetched whole."""
import posixpath
import struct
import tarfile
import zlib
from io import BytesIO
from typing import BinaryIO

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
SUFFIXES = (".tif", ".tiff", ".png")    # what the default member (no params.member) ends in
PNG_SAMPLES = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}    # PNG colour type -> samples stored per pixel (3 = palette index)
FIRST_READ = 1 << 20    # compressed bytes asked of io at first: one cache block
MAX_READ = 32 << 20     # ... doubling up to four 8 MiB range requests side by side for a member deep in the archive
MAX_MEMBER = 1 << 30    # a larger member is not an image this snippet should hold in memory
MAX_LINKS = 8           # hops from a link member to the regular file it stands for


class _Ahead:
    """The archive's compressed bytes for tarfile's stream reader, asked of `io` in reads that double from
    FIRST_READ to MAX_READ: an early member costs little, a late one is fetched by several requests at once."""

    def __init__(self, raw: BinaryIO):
        self._raw, self._buf, self._at, self._next = raw, b"", 0, FIRST_READ

    def read(self, n: int) -> bytes:
        if self._at >= len(self._buf):
            self._buf, self._at = self._raw.read(self._next), 0
            self._next = min(2 * self._next, MAX_READ)
        out = self._buf[self._at:self._at + n]
        self._at += len(out)
        return out


def _name(name: str) -> str:
    """A member name as it is compared: `./data/a.tif` and `data/a.tif` are one member."""
    return posixpath.normpath(name)


def _scan(io: IO, url: str, wanted: str | None) -> tuple[tarfile.TarInfo, bytes | None]:
    """One pass over the archive, from its first byte up to the member `wanted` (None: the first regular member
    that ends in an image suffix): its header and its bytes, or None for the bytes when it is a link."""
    try:
        raw = io.open(url)
    except NeedsFetch:      # a server without Range or Content-Length: the archive has to be staged whole
        raw = open(io.fetch(url), "rb")
    with raw, tarfile.open(fileobj=_Ahead(raw), mode="r|gz", bufsize=FIRST_READ) as tar:
        for info in tar:
            if wanted is None:
                if not (info.isfile() and info.name.lower().endswith(SUFFIXES)):
                    continue
            elif _name(info.name) != wanted:
                continue
            if info.islnk() or info.issym():
                return info, None
            if not info.isfile():
                raise ValueError(f"{url}: member {info.name!r} is not a file (tar type {info.type!r})")
            if info.size > MAX_MEMBER:
                raise ValueError(f"{url}: member {info.name!r} is {info.size} bytes, more than the {MAX_MEMBER} "
                                 "this snippet holds in memory")
            data = tar.extractfile(info).read()
            if len(data) != info.size:
                raise ValueError(f"{url}: member {info.name!r} is truncated: {len(data)} of {info.size} bytes")
            return info, data
    raise ValueError(f"{url} has no member {wanted!r}" if wanted is not None
                     else f"{url} has no regular member ending in {SUFFIXES}")


def _member(io: IO, url: str, member: str | None) -> tuple[str, bytes]:
    """(name, bytes) of the regular file `member` names, following tar links: a link has no bytes of its own, and
    its target lies before it in the stream, so each hop is one more pass (over blocks `io` has cached)."""
    wanted = None if member is None else _name(member)
    hops = []
    for _ in range(MAX_LINKS + 1):
        info, data = _scan(io, url, wanted)
        if data is not None:
            return info.name, data
        hops.append(info.name)
        # a hard link names its target by its full name in the archive, a symlink relative to its own folder
        wanted = _name(info.linkname if info.islnk()
                       else posixpath.join(posixpath.dirname(info.name), info.linkname))
    raise ValueError(f"{url}: member {member!r} is a chain of more than {MAX_LINKS} links: {hops}")


def _series(tif: tifffile.TiffFile, where: str) -> tifffile.TiffPageSeries:
    s = tif.series[0]
    if len(tif.series) != 1 or s.axes not in ("YX", "YXS"):
        raise ValueError(f"{where}: {len(tif.series)} TIFF series, the first with axes {s.axes!r} {tuple(s.shape)}; "
                         "this snippet reads one single-page YX or YXS (RGB) image")
    return s


def _header(data: bytes, where: str) -> tuple[tuple[int, ...], np.dtype]:
    """(shape, dtype) of the image as `read` returns it, (Y, X) or (C, Y, X), from the file's header alone."""
    if data[:8] == PNG:
        if len(data) < 33 or data[12:16] != b"IHDR":
            raise ValueError(f"{where}: a PNG without an IHDR chunk")
        width, height, depth, colour, _compression, _filter, interlace = struct.unpack(">IIBBBBB", data[16:29])
        if colour not in PNG_SAMPLES or depth not in (1, 2, 4, 8, 16):
            raise ValueError(f"{where}: PNG colour type {colour} with bit depth {depth} is not read")
        if interlace:
            raise ValueError(f"{where}: an interlaced (Adam7) PNG; the decoder reads one pass of it only")
        shape: tuple[int, ...] = (height, width)
        if PNG_SAMPLES[colour] > 1:
            shape = (PNG_SAMPLES[colour],) + shape
        return shape, np.dtype("uint16" if depth == 16 else "uint8")
    if data[:4] in TIFF:
        with tifffile.TiffFile(BytesIO(data)) as tif:
            s = _series(tif, where)
            shape = tuple(int(n) for n in s.shape)
            return (shape[2:] + shape[:2] if s.axes == "YXS" else shape), np.dtype(s.dtype).newbyteorder("=")
    raise ValueError(f"{where}: not a PNG or TIFF file: header={data[:8]!r}")


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png_stored(data: bytes, where: str) -> np.ndarray:
    """The samples a PNG stores, (Y, X) or (Y, X, S). The file is decoded from its IHDR and IDAT chunks alone,
    with a palette image declared grey: a palette index and a grey sample of the same bit depth are stored the
    same way, so the decoder then returns the indices instead of the palette's colours, and no tRNS chunk adds
    an alpha sample. The decoder scales 1/2/4-bit grey to 0..255 by an exact factor (255, 85, 17), undone here."""
    depth, colour = data[24], data[25]
    ihdr = bytearray(data[16:29])
    if colour == 3:
        ihdr[9] = 0
    chunks, at = [_chunk(b"IHDR", bytes(ihdr))], 8
    while at + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, at)
        if kind == b"IDAT":
            chunks.append(data[at:at + 12 + length])
        if kind == b"IEND":
            break
        at += 12 + length
    a = np.asarray(imagecodecs.png_decode(PNG + b"".join(chunks) + _chunk(b"IEND", b"")))
    if depth < 8:
        step = 255 // ((1 << depth) - 1)
        if a.dtype != np.uint8 or (a % step).any():
            raise ValueError(f"{where}: a {depth}-bit PNG decoded to {a.dtype} values that are not multiples of "
                             f"{step}; the stored values cannot be recovered")
        a = a // step
    return a


def _decode(data: bytes, where: str) -> np.ndarray:
    """The image as (Y, X) or (C, Y, X)."""
    if data[:8] == PNG:
        a = _png_stored(data, where)
    else:
        with tifffile.TiffFile(BytesIO(data)) as tif:
            a = np.asarray(_series(tif, where).asarray())
    return np.ascontiguousarray(np.moveaxis(a, 2, 0)) if a.ndim == 3 else a


class _Image:
    def __init__(self, io: IO, url: str, member: str | None):
        name, data = _member(io, url, member)
        self._where = f"{url} member {name}"
        # the member's bytes are kept: reaching them again would inflate the archive from its start once more
        self._data: bytes | None = data
        self._pixels: np.ndarray | None = None
        self.shape, self.dtype = _header(data, self._where)
        self.axes = "cyx" if len(self.shape) == 3 else "yx"
        self.voxel_size_nm = None       # PNG and TIFF resolution fields describe print size
        self.access = "file"            # any read costs the whole member: the stream has no index to seek by

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._pixels is None:
            a = _decode(self._data, self._where)
            if a.shape != self.shape or a.dtype.kind != self.dtype.kind or a.dtype.itemsize != self.dtype.itemsize:
                raise ValueError(f"{self._where}: decoded to {a.dtype} {a.shape}, its header says "
                                 f"{self.dtype} {self.shape}")
            self._pixels, self._data = a.astype(self.dtype, copy=False), None
        return self._pixels[tuple(region.get(k, slice(None)) for k in self.axes)]


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"image-targz-member reads one member of one archive, got {len(src.urls)} urls")
    url, member = src.urls[0], src.params.get("member")
    if "::" in url[url.find("/", url.find("://") + 3):]:
        raise ValueError(f"{url}: the url is the archive's own; name the member with params.member "
                         "(io reads `::` as a zip member)")
    if member is not None and not isinstance(member, str):
        raise ValueError(f"params.member must be the member's name in the archive, got {member!r}")
    return _Image(io, url, member)
