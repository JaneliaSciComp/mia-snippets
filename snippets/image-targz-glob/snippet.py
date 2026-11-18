"""One image per member (PNG or JPEG, told apart by the member's first bytes) from the members of gzip-compressed
tar archives chosen by glob, stacked along z. A colour member's samples are a `c` axis (`zcyx`); a grey member
is one plane (`zyx`), its values as the decoder gives them. With `palette: index` a palette (indexed-colour)
PNG is one plane of its stored palette indices, e.g. the class ids of an indexed mask, never the palette's
colours; without the param a palette PNG raises.

Each url is staged whole with `io.fetch` and is either a tar.gz or a zip that wraps tar.gz files (what
Dataverse's bundle download, `/api/access/datafiles/<id>`, sends); the staged file's first bytes say which. A
tar.gz has no index, so `open_array` inflates each archive once to list its members and decodes the first
matching one; `read` inflates up to the member it needs, one member per z plane.
"""
import gzip
import struct
import tarfile
import zipfile
import zlib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
GZIP = b"\x1f\x8b"
ZIP = b"PK\x03\x04"
TAR_GZ = (".tar.gz", ".tgz")
PALETTE, GREY = 3, 0            # IHDR colour types
PALETTE_MODES = ("index",)


class _Archive:
    """One tar.gz: a staged file, or a member of a staged zip. Its gzip stream stays open, so members read in
    archive order inflate it once; reading a member that lies before the last one read inflates from the start."""

    def __init__(self, path: Path, inner: str | None, address: str):
        self._path, self._inner, self.address = path, inner, address
        self._zip: zipfile.ZipFile | None = None
        self._gz: gzip.GzipFile | None = None

    def _stream(self) -> gzip.GzipFile:
        if self._gz is None:
            if self._inner is None:
                self._gz = gzip.GzipFile(fileobj=open(self._path, "rb"))
            else:
                self._zip = zipfile.ZipFile(self._path)
                self._gz = gzip.GzipFile(fileobj=self._zip.open(self._inner))
        return self._gz

    def members(self) -> dict[str, tuple[int, int]]:
        """The tar's regular files by name: (offset of the data in the inflated tar, size)."""
        out: dict[str, tuple[int, int]] = {}
        with tarfile.open(fileobj=self._stream(), mode="r:") as tar:
            for info in tar:
                if info.isreg() and not info.issparse():
                    if info.name in out:
                        raise ValueError(f"{self.address}: the tar holds {info.name!r} twice")
                    out[info.name] = (info.offset_data, info.size)
        return out

    def read(self, offset: int, size: int) -> bytes:
        gz = self._stream()
        gz.seek(offset)
        data = gz.read(size)
        if len(data) != size:
            raise ValueError(f"{self.address}: {size} bytes wanted at {offset} of the tar, {len(data)} read")
        return data


class _File(NamedTuple):
    archive: _Archive
    name: str
    offset: int
    size: int


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _indices(data: bytes, where: str) -> np.ndarray:
    """The stored palette indices of the palette PNG `data` as (Y, X) uint8.

    A PNG decoder replaces each index by its palette colour. The index of a palette PNG and the sample of a
    greyscale PNG of the same bit depth are stored the same way (one sample per pixel, the same filters, the
    same interlacing), so the file is re-wrapped as a greyscale PNG: the IHDR colour type is set from 3 to 0, the
    IDAT chunks are kept byte for byte and every other chunk (PLTE, tRNS, ...) is dropped. Decoding that gives
    the indices, stretched to 0..255 when the depth is below 8, which is undone exactly."""
    out, pos, header, pixels = [PNG], 8, None, False
    while True:
        if pos + 12 > len(data):
            raise ValueError(f"{where}: PNG ends at byte {len(data)} without an IEND chunk (truncated?)")
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + n
        if end > len(data):
            raise ValueError(f"{where}: PNG chunk {kind!r} at byte {pos} runs past the end of the file (truncated?)")
        if header is None:
            if kind != b"IHDR" or n != 13:
                raise ValueError(f"{where}: PNG does not start with a 13-byte IHDR chunk")
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data[pos + 8:pos + 21])
            if colour != PALETTE or depth not in (1, 2, 4, 8):
                raise ValueError(f"{where}: PNG colour type {colour}, bit depth {depth}: not a palette PNG")
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
        raise ValueError(f"{where}: PNG has no IDAT chunk")
    out.append(_chunk(b"IEND", b""))
    width, height, depth = header
    a = np.asarray(imagecodecs.png_decode(b"".join(out)))
    if a.shape != (height, width) or a.dtype != np.uint8:
        raise ValueError(f"{where}: decoded to {a.dtype} {a.shape}, not uint8 {(height, width)} as its header says")
    if depth < 8:
        # the decoder stretches a 1/2/4-bit grey sample to 0..255 (4-bit 0,1,2 -> 0,17,34): undo it exactly
        step = 255 // ((1 << depth) - 1)
        if np.any(a % step):
            raise ValueError(f"{where}: decoded {depth}-bit samples are not multiples of {step}, so the stored "
                             "indices cannot be recovered")
        a = a // step
    return a


def _decode(data: bytes, where: str, palette: str | None) -> np.ndarray:
    """The member's one image as (Y, X) or (Y, X, S), by its magic bytes (its name is not trusted)."""
    if data[:8] == PNG:
        # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type (3 = palette)
        if len(data) < 26:
            raise ValueError(f"{where}: a PNG of {len(data)} bytes, without a header")
        if data[25] == PALETTE:
            if palette != "index":
                raise ValueError(f"{where}: a palette PNG; the decoder would replace the stored indices by "
                                 "palette colours. Set params.palette: index to read the indices (an indexed mask)")
            return _indices(data, where)
        if data[24] < 8:
            raise ValueError(f"{where}: a {data[24]}-bit grey PNG; the decoder would rescale its samples to "
                             "0..255, so this snippet does not read it")
        a = imagecodecs.png_decode(data)
    elif data[:3] == JPEG:
        a = imagecodecs.jpeg_decode(data)
    else:
        raise ValueError(f"{where}: not a PNG or JPEG file: header={data[:8]!r}")
    a = np.asarray(a)
    if a.ndim not in (2, 3):
        raise ValueError(f"{where}: decoded to shape {a.shape}, not (Y, X) or (Y, X, S)")
    return a


class _Stack:
    def __init__(self, files: list[_File], palette: str | None):
        self._files, self._palette = files, palette
        self._last: tuple[int, np.ndarray] | None = None    # the last member decoded: c planes are read one by one
        first = self._load(0)                               # (Y, X) or (C, Y, X); every member must match
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self.axes = ("z" if len(files) > 1 else "") + ("cyx" if first.ndim == 3 else "yx")
        self.shape = ((len(files),) if len(files) > 1 else ()) + first.shape
        self.access = "plane"           # one member per z plane: any region of it costs the member, inflated
        self.voxel_size_nm = None       # from the staged archive
        self._last = (0, first)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes if k != "z")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._files))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._files))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """Member k as (Y, X) or (C, Y, X), checked against member 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            f = self._files[k]
            raise ValueError(f"{f.archive.address}::{f.name}: {a.dtype} {a.shape} differs from the first member's "
                             f"{self.dtype} {self._plane_shape}; all members of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        f = self._files[k]
        a = _decode(f.archive.read(f.offset, f.size), f"{f.archive.address}::{f.name}", self._palette)
        return a if a.ndim == 2 else np.ascontiguousarray(np.moveaxis(a, 2, 0))


def _archives(io: IO, url: str) -> list[_Archive]:
    """The tar.gz archives one url stands for: the staged file itself, or the .tar.gz members of a staged zip."""
    path = io.fetch(url)
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == GZIP:
        return [_Archive(path, None, url)]
    if magic != ZIP:
        raise ValueError(f"{url}: neither a gzip stream nor a zip: header={magic!r}")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    inner = sorted(n for n in names if n.lower().endswith(TAR_GZ))
    if not inner:
        raise ValueError(f"{url}: a zip without a .tar.gz or .tgz member (it holds {names[:5]})")
    return [_Archive(path, n, f"{url}::{n}") for n in inner]


def _files(io: IO, url: str, pattern: str) -> list[_File]:
    """The members of the url's archives that match `pattern`, sorted by member name."""
    found: dict[str, _File] = {}
    seen = 0
    for archive in _archives(io, url):
        members = archive.members()
        seen += len(members)
        for name, (offset, size) in members.items():
            if fnmatchcase(name, pattern):
                if name in found:
                    raise ValueError(f"{url}: {name!r} is in {found[name].archive.address} and {archive.address}")
                found[name] = _File(archive, name, offset, size)
    if not found:
        raise ValueError(f"{url}: none of the {seen} tar members matches {pattern!r}")
    return [found[name] for name in sorted(found)]


def open_array(src: Source, io: IO) -> Array:
    pattern = src.params.get("members")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"params.members must be a glob over the tar member names, got {pattern!r}")
    palette = src.params.get("palette")
    if palette is not None and palette not in PALETTE_MODES:
        raise ValueError(f"params.palette must be one of {PALETTE_MODES}, got {palette!r}")
    if len(set(src.urls)) != len(src.urls):
        raise ValueError("the urls name the same archive twice")
    return _Stack([f for url in src.urls for f in _files(io, url, pattern)], palette)
