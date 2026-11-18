"""One image per member (PNG or JPEG, told apart by the member's first bytes) of a gzip-compressed tar archive,
the members chosen by a glob over their names and stacked along z. A colour member's samples are a `c` axis
(`zcyx`). The planes are ordered by member name, or by `key`, a regular expression that takes the part of the
name two arrays share, so a label archive and a raw archive pair plane for plane whatever order and folders
their members are stored in.

A tar.gz has no index and cannot be read by range, so the archive is inflated from its first byte once, at
open, to learn the member names: the compressed bytes come through `io.open` (range requests, cached in
blocks, counted against the job's cap) up to the last member wanted (`count`) or the archive's end. That pass
keeps no pixels. It keeps, for each wanted member, a copy of the inflater's state at the 1 MiB step of the
archive the member starts in; `read` re-enters the stream there, so a plane costs its own member and at most
1 MiB of compressed bytes before it, from the block cache, not the archive again."""
import re
import tarfile
import zlib
from fnmatch import fnmatchcase
from typing import Any, BinaryIO, Callable, NamedTuple, TypeVar

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

STEP = 1 << 20          # compressed bytes inflated at a time; the stream can be re-entered at each: io's cache block
SCAN_READ = 32 << 20    # the most compressed bytes asked of io at a time during the scan: four 8 MiB range requests
                        # side by side. The scan asks for 1 MiB first and doubles, so an early member stays cheap
GLOB_CHARS = "*?["
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
T = TypeVar("T")


class _Point(NamedTuple):
    """Where the gzip stream can be re-entered: `state` is the inflater before compressed byte `at`, with `out`
    bytes of the tar produced so far."""
    at: int
    out: int
    state: Any          # a zlib decompressobj; never fed, only copied


class _Member(NamedTuple):
    name: str
    offset: int         # of the member's data in the tar (the inflated stream)
    size: int
    point: _Point       # the last re-entry point at or before `offset`


class _Gunzip:
    """The inflated stream of a gzip file, read forward from its start or from a re-entry point. It inflates one
    STEP only when everything inflated so far has been handed out, so `point`, the state before the last step,
    never lies behind a byte the reader has not been given yet."""

    def __init__(self, raw: BinaryIO, point: _Point | None = None, most: int = STEP):
        self._raw, self._ask, self._most = raw, STEP, most      # compressed bytes asked of io next, and at most
        self._d = point.state.copy() if point else zlib.decompressobj(31)
        self._at = point.at if point else 0             # compressed bytes inflated
        self._out = point.out if point else 0           # tar bytes produced
        self._block, self._used = b"", 0                # compressed bytes read from io, and how many are inflated
        self._buf = bytearray()                         # produced, not handed out yet
        self.point = point or _Point(0, 0, self._d.copy())
        raw.seek(self._at)

    def _step(self) -> bool:
        """Inflate the next STEP of compressed bytes into the buffer; False at the end of the file."""
        if self._used >= len(self._block):
            self._block, self._used = self._raw.read(self._ask), 0
            self._ask = min(2 * self._ask, self._most)
            if not self._block:
                return False
        self.point = _Point(self._at, self._out, self._d.copy())
        data = memoryview(self._block)[self._used:self._used + STEP]
        self._used, self._at = self._used + len(data), self._at + len(data)
        got = self._d.decompress(data)
        while self._d.eof and self._d.unused_data:      # a gzip file may be several streams, one after another
            rest = self._d.unused_data
            self._d = zlib.decompressobj(31)
            got += self._d.decompress(rest)
        self._out += len(got)
        self._buf += got
        return True

    def read(self, n: int) -> bytes:
        """Up to n bytes; fewer when the buffer holds fewer (the caller asks again), b"" at the end."""
        while not self._buf:
            if not self._step():
                return b""
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def take(self, skip: int, n: int) -> bytes:
        """The n bytes that follow the next `skip` bytes."""
        out = bytearray()
        while len(out) < n:
            data = self.read(min(skip, STEP) if skip else n - len(out))
            if not data:
                raise ValueError(f"the archive ends {skip + n - len(out)} bytes before the member does")
            if skip:
                skip -= len(data)
            else:
                out += data
        return bytes(out)


def _decode(data: bytes, name: str) -> np.ndarray:
    """The member's one image as (Y, X) or (Y, X, S), by its magic bytes (a member's name is not trusted)."""
    if data[:8] == PNG:
        # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type (3 = palette)
        if len(data) < 26 or data[25] == 3 or data[24] < 8:
            raise ValueError(f"{name}: a palette or 1/2/4-bit PNG; the decoder would replace the stored indices "
                             "by palette colours or rescale them to 0..255, so this snippet does not read it")
        a = imagecodecs.png_decode(data)
    elif data[:3] == JPEG:
        a = imagecodecs.jpeg_decode(data)
    else:
        raise ValueError(f"{name}: not a PNG or JPEG file: header={data[:8]!r}")
    a = np.asarray(a)
    if a.ndim not in (2, 3):
        raise ValueError(f"{name}: decoded to shape {a.shape}, not (Y, X) or (Y, X, S)")
    return a


class _Images:
    def __init__(self, io: IO, url: str, pattern: str, key: re.Pattern | None, count: int | None):
        self._io, self._url = io, url
        self._members = _ordered(self._scan(pattern, count), key)
        self._last: tuple[int, np.ndarray] | None = None    # the last member decoded: c planes are read one by one
        first = self._load(0)                               # (Y, X) or (C, Y, X); every member must match
        n = len(self._members)
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self.axes = ("z" if n > 1 else "") + ("cyx" if first.ndim == 3 else "yx")
        self.shape = ((n,) if n > 1 else ()) + first.shape
        self.access = "plane"           # one member per z plane: any region of it costs the whole member
        self.voxel_size_nm = None       # PNG/JPEG resolution fields describe print size
        self._last = (0, first)

    def _archive(self, work: Callable[[BinaryIO], T]) -> T:
        """`work` on the archive opened by range; on the whole file, staged once, when the server has no Range
        or Content-Length (NeedsFetch, which a server that ignores Range raises at the first read)."""
        try:
            with self._io.open(self._url) as raw:
                return work(raw)
        except NeedsFetch:
            with open(self._io.fetch(self._url), "rb") as raw:
                return work(raw)

    def _scan(self, pattern: str, count: int | None) -> list[_Member]:
        """The regular members whose names match `pattern`, in archive order: the first `count`, or all of them
        when it is None, which reads the archive to its end."""
        def scan(raw: BinaryIO) -> list[_Member]:
            found: list[_Member] = []
            gz = _Gunzip(raw, most=SCAN_READ)
            with tarfile.open(fileobj=gz, mode="r|", bufsize=STEP) as tar:
                for info in tar:
                    if not (info.isreg() and fnmatchcase(info.name, pattern)):
                        continue
                    if gz.point.out > info.offset_data:
                        raise RuntimeError(f"{info.name}: the stream was inflated past the member's start")
                    found.append(_Member(info.name, info.offset_data, info.size, gz.point))
                    if len(found) == count:
                        break
            return found

        found = self._archive(scan)
        if not found:
            raise ValueError(f"no member of {self._url} matches {pattern!r}")
        if count is not None and len(found) < count:
            raise ValueError(f"{self._url}: {len(found)} members match {pattern!r}, params.count says {count}")
        return found

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes if k != "z")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._members))[sel])[idx]     # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._members))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """Member k as (Y, X) or (C, Y, X), checked against member 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._members[k].name}: {a.dtype} {a.shape} differs from the first member's "
                             f"{self.dtype} {self._plane_shape} (c, y, x); all members of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        m = self._members[k]
        data = self._archive(lambda raw: _Gunzip(raw, m.point).take(m.offset - m.point.out, m.size))
        a = _decode(data, m.name)
        return a if a.ndim == 2 else np.ascontiguousarray(np.moveaxis(a, 2, 0))


def _ordered(members: list[_Member], key: re.Pattern | None) -> list[_Member]:
    """The members sorted by name, or by what `key` finds in the name: its groups, or the whole match when it
    has none. A name `key` does not match, or two members with one key, raise."""
    def key_of(m: _Member) -> tuple[str, ...]:
        if key is None:
            return (m.name,)
        found = key.search(m.name)
        if found is None:
            raise ValueError(f"params.key {key.pattern!r} does not match the member {m.name!r}")
        return tuple(g or "" for g in found.groups()) or (found.group(0),)

    by_key: dict[tuple[str, ...], _Member] = {}
    for m in members:
        k = key_of(m)
        if k in by_key:
            raise ValueError(f"the members {by_key[k].name!r} and {m.name!r} have the same "
                             f"{'name' if key is None else 'key ' + repr(k)}; the plane order would be arbitrary")
        by_key[k] = m
    return [by_key[k] for k in sorted(by_key)]


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"image-targz-glob reads the members of one archive, got {len(src.urls)} urls")
    pattern, key, count = src.params.get("members"), src.params.get("key"), src.params.get("count")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"params.members must be a member name or a glob over member names, got {pattern!r}")
    if key is not None and not isinstance(key, str):
        raise ValueError(f"params.key must be a regular expression, got {key!r}")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise ValueError(f"params.count must be a positive integer, got {count!r}")
    if count is None and not any(ch in pattern for ch in GLOB_CHARS):
        count = 1               # a plain name is one member: the scan stops at it
    return _Images(io, src.urls[0], pattern, re.compile(key) if key is not None else None, count)
