"""One image per file (JPEG or PNG, told apart by the file's first bytes) from the members of a 7z archive chosen
by glob, stacked along z. The archive is a plain url; `params.members` names its members.

A 7z archive packs its files into folders ("solid blocks"): one compressed stream holding many files end to end,
so a member is reached only by decoding its folder from the start. The archive is therefore staged whole
(`io.fetch`), its header is parsed here (the 7z container format, with the standard library's `lzma` for the
LZMA and LZMA2 coders), and each folder is decoded forward, keeping the bytes of this array's members as the
decoder passes them. A colour file's samples are a `c` axis (`zcyx`); a palette PNG is read as its stored
palette indices (the class ids of a LabelMe / VOC mask), never as palette colours.

`open_array` decodes up to the first matching member only; `read` decodes on to the members it needs.
"""
import lzma
import struct
import zlib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import BinaryIO, NamedTuple

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

SIGNATURE = b"7z\xbc\xaf\x27\x1c"
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
GLOB_CHARS = "*?["
COPY, LZMA1, LZMA2 = b"\x00", b"\x03\x01\x01", b"\x21"     # 7z coder ids this snippet decodes
STEP = 1 << 20          # bytes per decoder step, packed in and (at most) decoded out
KEEP = 1 << 30          # member bytes one array keeps in memory; past it a step back decodes the folder again
HEADER = 1 << 28        # a 7z header longer than this is refused, not allocated
# property ids of the 7z header (7zFormat.txt)
END, HEADER_ID, ARCHIVE_PROPERTIES, ADDITIONAL_STREAMS, MAIN_STREAMS, FILES, PACK_INFO, UNPACK_INFO = range(8)
SUBSTREAMS, SIZE, CRC, FOLDER, UNPACK_SIZE, NUM_UNPACK_STREAM, EMPTY_STREAM, EMPTY_FILE = range(8, 16)
NAMES, ENCODED_HEADER = 0x11, 0x17


class _Folder(NamedTuple):
    """One solid block: a packed stream at [offset, offset + packed) of the archive and the files it decodes to."""
    coder: bytes | None                 # the one coder's id, None for a chain of coders (a filter before LZMA)
    props: bytes
    offset: int
    packed: int
    size: int                           # decoded bytes
    files: list[tuple[int, int | None]]     # (size, CRC-32 or None) of each file, in stream order


class _Member(NamedTuple):
    folder: int
    start: int                          # offset in the folder's decoded stream
    size: int
    crc: int | None


class _Header:
    """A cursor over 7z header bytes."""

    def __init__(self, data: bytes):
        self._data, self._pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > len(self._data):
            raise ValueError("7z header ends inside a field")
        self._pos += n
        return self._data[self._pos - n:self._pos]

    def rest(self) -> bytes:
        return self.take(len(self._data) - self._pos)

    def byte(self) -> int:
        return self.take(1)[0]

    def number(self) -> int:
        """7z's variable-length integer: the first byte's leading 1 bits count the bytes that follow."""
        first, value = self.byte(), 0
        for i in range(8):
            mask = 0x80 >> i
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * i))
            value |= self.byte() << (8 * i)
        return value

    def count(self) -> int:
        """A number that sizes a list: more entries than the header has bytes is a corrupt header."""
        n = self.number()
        if n > len(self._data):
            raise ValueError(f"7z header counts {n} entries in {len(self._data)} bytes")
        return n

    def bits(self, n: int) -> list[bool]:
        data = self.take((n + 7) // 8)
        return [bool(data[i // 8] & (0x80 >> (i % 8))) for i in range(n)]

    def crcs(self, n: int) -> list[int | None]:
        defined = [True] * n if self.byte() else self.bits(n)
        return [struct.unpack("<I", self.take(4))[0] if d else None for d in defined]

    def expect(self, *ids: int) -> int:
        t = self.byte()
        if t not in ids:
            raise ValueError(f"7z header: property 0x{t:02x} where one of {[f'0x{i:02x}' for i in ids]} was expected")
        return t


def _coders(h: _Header) -> tuple[bytes | None, bytes, int, int, int]:
    """One folder's coder record: (coder id or None for a chain, its properties, the number of its output
    streams, the index of the output that is the folder's data, the number of its packed streams)."""
    n = h.count()
    coder, props, n_in, n_out = None, b"", 0, 0
    for _ in range(n):
        flag = h.byte()
        ident = h.take(flag & 0x0F)
        ins, outs = (h.count(), h.count()) if flag & 0x10 else (1, 1)
        attrs = h.take(h.count()) if flag & 0x20 else b""
        n_in, n_out = n_in + ins, n_out + outs
        if n == 1 and ins == 1 and outs == 1:
            coder, props = ident, attrs
    bound = set()
    for _ in range(max(n_out - 1, 0)):
        h.number()                      # the bound input
        bound.add(h.number())
    n_packed = n_in - max(n_out - 1, 0)
    if n_packed > 1:
        for _ in range(n_packed):
            h.number()
    main = next((i for i in range(n_out) if i not in bound), 0)
    return coder, props, n_out, main, n_packed


def _streams(h: _Header) -> list[_Folder]:
    """A StreamsInfo structure as its folders, each with its packed range and its files' sizes and CRCs."""
    pack_pos, pack_sizes, coders, sizes, crcs = 0, [], [], [], []
    t = h.byte()
    if t == PACK_INFO:
        pack_pos, n = h.number(), h.count()
        t = h.expect(SIZE, CRC, END)
        if t == SIZE:
            pack_sizes = [h.number() for _ in range(n)]
            t = h.expect(CRC, END)
        if t == CRC:
            h.crcs(n)
            h.expect(END)
        t = h.byte()
    if t == UNPACK_INFO:
        h.expect(FOLDER)
        n = h.count()
        if h.byte():
            raise ValueError("7z header: folders stored in an external stream are not supported")
        coders = [_coders(h) for _ in range(n)]
        h.expect(UNPACK_SIZE)
        sizes = [[h.number() for _ in range(c[2])][c[3]] for c in coders]
        crcs = [None] * n
        if h.expect(CRC, END) == CRC:
            crcs = h.crcs(n)
            h.expect(END)
        t = h.byte()
    counts = [1] * len(coders)
    files: list[list[tuple[int, int | None]]] | None = None
    if t == SUBSTREAMS:
        t = h.byte()
        if t == NUM_UNPACK_STREAM:
            counts = [h.count() for _ in coders]
            t = h.byte()
        split = []
        for size, n in zip(sizes, counts):
            if t != SIZE and n > 1:
                raise ValueError("7z header: a folder of several files without their sizes")
            first = [h.number() for _ in range(n - 1)] if t == SIZE else []
            if sum(first) > size:
                raise ValueError("7z header: a folder's files are longer than the folder")
            split.append(first + [size - sum(first)] if n else [])
        if t == SIZE:
            t = h.byte()
        known = [[crc] if n == 1 and crc is not None else None for crc, n in zip(crcs, counts)]
        if t == CRC:
            listed = iter(h.crcs(sum(n for k, n in zip(known, counts) if k is None)))
            known = [k if k is not None else [next(listed) for _ in range(n)] for k, n in zip(known, counts)]
            t = h.byte()
        if t != END:
            raise ValueError(f"7z header: property 0x{t:02x} in SubStreamsInfo")
        files = [list(zip(s, k if k is not None else [None] * n)) for s, k, n in zip(split, known, counts)]
        t = h.byte()
    if t != END:
        raise ValueError(f"7z header: property 0x{t:02x} in StreamsInfo")
    if files is None:
        files = [[(size, crc)] for size, crc in zip(sizes, crcs)]
    folders, k = [], 0
    for (coder, props, _, _, n_packed), size, f in zip(coders, sizes, files):
        if k + n_packed > len(pack_sizes):
            raise ValueError("7z header: fewer packed streams than the folders use")
        offset = 32 + pack_pos + sum(pack_sizes[:k])
        folders.append(_Folder(coder if n_packed == 1 else None, props, offset, pack_sizes[k], size, f))
        k += n_packed
    return folders


def _decoder(folder: _Folder, name: str) -> lzma.LZMADecompressor | None:
    """The folder's decompressor; None for the Copy coder (stored bytes)."""
    if folder.coder == COPY:
        return None
    if folder.coder == LZMA2 and len(folder.props) == 1 and folder.props[0] <= 40:
        b = folder.props[0]
        size = 0xFFFFFFFF if b == 40 else (2 | (b & 1)) << (b // 2 + 11)
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA2, "dict_size": size}])
    if folder.coder == LZMA1 and len(folder.props) == 5 and folder.props[0] < 225:
        b, size = folder.props[0], struct.unpack("<I", folder.props[1:])[0]
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA1, "lc": b % 9, "lp": b // 9 % 5, "pb": b // 45, "dict_size": size}])
    what = "a chain of coders (e.g. a BCJ or Delta filter)" if folder.coder is None else f"coder {folder.coder.hex()}"
    raise ValueError(f"{name}: packed with {what}; this snippet decodes Copy, LZMA and LZMA2 folders only")


class _Solid:
    """One folder's decoded bytes, read forward in steps; a read behind the cursor starts the decoder again."""

    def __init__(self, fh: BinaryIO, folder: _Folder, name: str):
        self._fh, self._folder, self._name = fh, folder, name
        self.pos = 0                    # decoded offset of the step held in _buf
        self._restart()

    def _restart(self) -> None:
        self._dec = _decoder(self._folder, self._name)
        self._at, self._left, self.pos, self._buf = self._folder.offset, self._folder.packed, 0, b""

    def _packed(self) -> bytes:
        self._fh.seek(self._at)         # raw and label arrays may share one file object
        data = self._fh.read(min(STEP, self._left))
        self._at, self._left = self._at + len(data), self._left - len(data)
        return data

    def _step(self) -> bytes:
        """The next decoded bytes, at most STEP; b"" at the end of the packed stream."""
        if self._dec is None:
            return self._packed()
        while not self._dec.eof:
            data = self._packed() if self._dec.needs_input else b""
            if self._dec.needs_input and not data:
                break
            out = self._dec.decompress(data, STEP)
            if out:
                return out
        return b""

    def read(self, start: int, size: int) -> bytes:
        if start < self.pos:
            self._restart()
        parts, end = [], start + size
        while True:
            lo, hi = max(start, self.pos), min(end, self.pos + len(self._buf))
            if hi > lo:
                parts.append(self._buf[lo - self.pos:hi - self.pos])
            if self.pos + len(self._buf) >= end:
                return b"".join(parts)
            self.pos += len(self._buf)
            try:
                self._buf = self._step()
            except lzma.LZMAError as e:
                raise ValueError(f"{self._name}: the packed stream does not decode at byte {self.pos}: {e}") from e
            if not self._buf:
                raise ValueError(f"{self._name}: the packed stream ends at byte {self.pos} of "
                                 f"{self._folder.size} decoded bytes")


class _SevenZip:
    """A 7z archive on disk: its members by name, and their bytes through one forward decoder per folder."""

    def __init__(self, path: Path, name: str):
        self._name = name
        self._fh = open(path, "rb")
        start = self._fh.read(32)
        if len(start) < 32 or start[:6] != SIGNATURE:
            raise ValueError(f"{name}: not a 7z archive: header={start[:6]!r}")
        offset, size, crc = struct.unpack("<QQI", start[12:32])
        if size > HEADER:
            raise ValueError(f"{name}: a 7z header of {size} bytes")
        self._fh.seek(32 + offset)
        data = self._fh.read(size)
        if len(data) != size or zlib.crc32(data) != crc:
            raise ValueError(f"{name}: the 7z header at byte {32 + offset} is cut short or fails its CRC "
                             "(a truncated download, or a multi-volume archive)")
        self.folders: list[_Folder] = []
        self.members: dict[str, _Member] = {}
        self._solids: dict[int, _Solid] = {}
        if size:
            self._parse(data)

    def _parse(self, data: bytes) -> None:
        h = _Header(data)
        t = h.byte()
        while t == ENCODED_HEADER:      # the header itself is packed: one folder holding the real header
            packed = _streams(h)
            if len(packed) != 1 or packed[0].size > HEADER:
                raise ValueError(f"{self._name}: a packed 7z header in {len(packed)} folders")
            data = _Solid(self._fh, packed[0], self._name).read(0, packed[0].size)
            crc = packed[0].files[0][1]
            if crc is not None and zlib.crc32(data) != crc:
                raise ValueError(f"{self._name}: the packed 7z header fails its CRC")
            h = _Header(data)
            t = h.byte()
        if t != HEADER_ID:
            raise ValueError(f"{self._name}: 7z header starts with property 0x{t:02x}")
        t = h.byte()
        if t == ARCHIVE_PROPERTIES:
            while h.byte() != END:
                h.take(h.number())
            t = h.byte()
        if t == ADDITIONAL_STREAMS:
            raise ValueError(f"{self._name}: 7z additional streams are not supported")
        if t == MAIN_STREAMS:
            self.folders = _streams(h)
            t = h.byte()
        if t != FILES:
            return                      # an archive without files
        n = h.count()
        names: list[str] = []
        empty = [False] * n
        while (t := h.byte()) != END:
            body = _Header(h.take(h.number()))
            if t == EMPTY_STREAM:
                empty = body.bits(n)
            elif t == NAMES:
                if body.byte():
                    raise ValueError(f"{self._name}: 7z file names stored in an external stream are not supported")
                names = body.rest().decode("utf-16-le").split("\0")[:-1]
        if len(names) != n:
            raise ValueError(f"{self._name}: 7z header names {len(names)} of its {n} files")
        streams = ((i, start, size, crc) for i, f in enumerate(self.folders)
                   for (size, crc), start in zip(f.files, _starts(f.files)))
        for name, no_stream in zip(names, empty):   # directories and empty files have no stream
            if not no_stream:
                where = next(streams, None)
                if where is None:
                    raise ValueError(f"{self._name}: 7z header lists more files than its folders hold")
                self.members[name.replace("\\", "/")] = _Member(*where)

    def read(self, member: str) -> bytes:
        """The member's bytes, CRC-checked; decodes its folder from the decoder's position, or from the start."""
        m = self.members[member]
        if m.folder not in self._solids:
            self._solids[m.folder] = _Solid(self._fh, self.folders[m.folder], self._name)
        data = self._solids[m.folder].read(m.start, m.size)
        if m.crc is not None and zlib.crc32(data) != m.crc:
            raise ValueError(f"{self._name}::{member}: decoded bytes fail the archive's CRC-32")
        return data

    def position(self, folder: int) -> int:
        """The decoded offset a read of this folder continues from without starting again."""
        return self._solids[folder].pos if folder in self._solids else 0


def _starts(files: list[tuple[int, int | None]]) -> list[int]:
    out, at = [], 0
    for size, _ in files:
        out.append(at)
        at += size
    return out


def _png_indices(data: bytes, name: str) -> np.ndarray:
    """A palette PNG's stored indices as (Y, X) uint8. The file is decoded as the grey image with the same
    IHDR and IDAT chunks (colour type 3 -> 0, palette and transparency dropped), so the decoder cannot map
    indices to colours; a 1/2/4-bit grey decode is scaled to 0..255 by the decoder and is scaled back."""
    bits, chunks, pos = data[24], [], 8
    while pos + 12 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            body = body[:9] + b"\x00" + body[10:]
        if kind in (b"IHDR", b"IDAT", b"IEND"):
            chunks.append(struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body)))
        pos += 12 + length
    a = np.asarray(imagecodecs.png_decode(PNG + b"".join(chunks)))
    if a.ndim != 2 or a.dtype != np.uint8:
        raise ValueError(f"{name}: palette PNG decoded to {a.dtype} {a.shape}, not (Y, X) uint8")
    if bits < 8:
        step = 255 // ((1 << bits) - 1)
        if (a % step).any():
            raise ValueError(f"{name}: a {bits}-bit palette PNG whose decoded values are not multiples of {step}")
        a = a // step
    return a


def _decode(data: bytes, name: str) -> np.ndarray:
    """The file's one image as (Y, X) or (Y, X, S), by its magic bytes (a member's name is not trusted)."""
    if data[:8] == PNG:
        # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type (3 = palette)
        if len(data) < 26:
            raise ValueError(f"{name}: a PNG of {len(data)} bytes")
        if data[25] == 3:
            return _png_indices(data, name)
        if data[24] < 8:
            raise ValueError(f"{name}: a {data[24]}-bit grey PNG; the decoder would rescale its values to 0..255, "
                             "so this snippet does not read it")
        a = imagecodecs.png_decode(data)
    elif data[:3] == JPEG:
        a = imagecodecs.jpeg_decode(data)
    else:
        raise ValueError(f"{name}: not a PNG or JPEG file: header={data[:8]!r}")
    a = np.asarray(a)
    if a.ndim not in (2, 3):
        raise ValueError(f"{name}: decoded to shape {a.shape}, not (Y, X) or (Y, X, S)")
    return a


class _Tiles:
    def __init__(self, archive: _SevenZip, url: str, names: list[str]):
        self._archive, self._url, self._names = archive, url, names
        self._kept: dict[int, bytes] = {}       # this array's members the decoder has passed, by z
        self._kept_bytes = 0
        self._by_folder: dict[int, list[tuple[int, int]]] = {}      # folder -> (start, z) in stream order
        for k, name in enumerate(names):
            m = archive.members[name]
            self._by_folder.setdefault(m.folder, []).append((m.start, k))
        for order in self._by_folder.values():
            order.sort()
        self._last: tuple[int, np.ndarray] | None = None    # the last file decoded: c planes are read one by one
        first = self._load(0)                               # (Y, X) or (C, Y, X); every file must match
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self.axes = ("z" if len(names) > 1 else "") + ("cyx" if first.ndim == 3 else "yx")
        self.shape = ((len(names),) if len(names) > 1 else ()) + first.shape
        self.access = "plane"           # one file per z plane; any region of it costs the whole file, and the
        self.voxel_size_nm = None       # folder's stream up to it
        self._last = (0, first)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes if k != "z")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._names))[sel])[idx]   # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._names))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (C, Y, X), checked against file 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._url}::{self._names[k]}: {a.dtype} {a.shape} differs from the first file's "
                             f"{self.dtype} {self._plane_shape} (c, y, x); all files of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        a = _decode(self._bytes(k), f"{self._url}::{self._names[k]}")
        return a if a.ndim == 2 else np.ascontiguousarray(np.moveaxis(a, 2, 0))

    def _bytes(self, k: int) -> bytes:
        """Member k's bytes. The folder is decoded forward to it, and this array's other members on the way are
        kept (up to KEEP bytes), so reading the planes in any order decodes the folder once."""
        if k in self._kept:
            return self._kept[k]
        m = self._archive.members[self._names[k]]
        at = self._archive.position(m.folder)
        if at > m.start:
            at = 0                      # behind the decoder: it starts again
        data = b""
        for start, j in self._by_folder[m.folder]:
            if at <= start <= m.start and j not in self._kept:
                data = self._archive.read(self._names[j])
                if j != k and self._kept_bytes + len(data) <= KEEP:
                    self._kept[j], self._kept_bytes = data, self._kept_bytes + len(data)
        if self._kept_bytes + len(data) <= KEEP:
            self._kept[k], self._kept_bytes = data, self._kept_bytes + len(data)
        return data


def open_array(src: Source, io: IO) -> Array:
    pattern = src.params.get("members")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("params.members is required: a glob over the archive's member names, "
                         "e.g. 'dataset/JPEGImages/*.jpg'")
    if len(src.urls) != 1 or "::" in src.urls[0].split("://", 1)[-1]:
        raise ValueError("urls must be one plain 7z archive url; its members are named by params.members, "
                         "not by '<url>::<member>' (which the harness reads as a zip member)")
    url = src.urls[0]
    archive = _SevenZip(io.fetch(url), url)     # a solid archive has no smaller unit than the whole file
    names = sorted(n for n in archive.members if fnmatchcase(n, pattern))
    if not names:
        raise ValueError(f"no member of {url} matches {pattern!r} ({len(archive.members)} members with data)")
    return _Tiles(archive, url, names)
