"""One numeric 2-D variable (chosen by `key`) of each MATLAB v5 MAT-file, from zip members or directory files
chosen by glob (or listed one by one), stacked along z: one file per z plane, in sorted name order.

The MAT-file is parsed here (level 5 format: a 128-byte header, then one element per variable, each usually
zlib-compressed), with numpy and the standard library only. The variable's rows are y and its columns x; its
values and its MATLAB class's dtype are returned unchanged. Files are opened lazily: `open_array` reads the
first file's variable headers only; `read` fetches the files it needs and inflates only the variable asked for.
"""
import struct
import zlib
from typing import Iterator, NamedTuple

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MAGIC = b"MATLAB 5.0 MAT-file"
HEADER = 128                        # descriptive text, subsystem offset, version, then the endian indicator
MI_INT8, MI_INT32, MI_UINT32, MI_MATRIX, MI_COMPRESSED = 1, 5, 6, 14, 15
STORED = {1: "i1", 2: "u1", 3: "<i2", 4: "<u2", 5: "<i4", 6: "<u4", 7: "<f4", 9: "<f8", 12: "<i8", 13: "<u8"}
CLASSES = {6: "f8", 7: "f4", 8: "i1", 9: "u1", 10: "i2", 11: "u2", 12: "i4", 13: "u4", 14: "i8", 15: "u8"}
NAMES = {1: "cell", 2: "struct", 3: "object", 4: "char", 5: "sparse", 6: "double", 7: "single", 8: "int8",
         9: "uint8", 10: "int16", 11: "uint16", 12: "int32", 13: "uint32", 14: "int64", 15: "uint64",
         16: "function handle", 17: "opaque"}
OPAQUE = 17                         # the one class whose element has no dimensions before its name
COMPLEX = 0x08                      # bit of the array flags' second byte
HEAD = 4096                         # bytes of a variable inflated to learn its name, class and dimensions


class _Var(NamedTuple):
    name: str
    mclass: int
    flags: int
    dims: tuple[int, ...]
    data: int           # offset of the real part's tag, from the start of the variable's miMATRIX element
    at: int             # offset in the file of the top-level element's body (after its 8-byte tag)
    nbytes: int         # that body's length
    compressed: bool    # the body is a zlib stream holding the miMATRIX element

    def describe(self) -> str:
        return f"{self.name!r} ({NAMES.get(self.mclass, f'class {self.mclass}')} {list(self.dims)})"


def _tag(buf: bytes | memoryview, pos: int) -> tuple[int, int, int, int]:
    """(type, byte count, data offset, next element's offset) of the sub-element whose tag is at `pos`. A small
    element (1 to 4 data bytes) packs type and count into the first word and the data into the second."""
    word, = struct.unpack_from("<I", buf, pos)
    if word >> 16:
        if word >> 16 > 4:
            raise ValueError(f"a small data element of {word >> 16} bytes")
        return word & 0xFFFF, word >> 16, pos + 4, pos + 8
    n, = struct.unpack_from("<I", buf, pos + 4)
    return word, n, pos + 8, pos + 8 + n + (-n % 8)


def _header(buf: bytes | memoryview, at: int, nbytes: int, compressed: bool) -> _Var | None:
    """What a miMATRIX element says before its data: `buf` starts at its tag and need not hold all of it. None
    for an element without a name (an empty one, or an opaque object)."""
    mtype, n = struct.unpack_from("<II", buf, 0)
    if mtype != MI_MATRIX:
        raise ValueError(f"an element of type {mtype} where a miMATRIX ({MI_MATRIX}) belongs")
    if n == 0:
        return None
    t, n, start, pos = _tag(buf, 8)
    if t != MI_UINT32 or n != 8:
        raise ValueError(f"array flags of type {t} and {n} bytes, not miUINT32 and 8")
    word, = struct.unpack_from("<I", buf, start)
    mclass, flags = word & 0xFF, (word >> 8) & 0xFF
    if mclass == OPAQUE:
        return None
    t, n, start, pos = _tag(buf, pos)
    if t != MI_INT32 or n % 4:
        raise ValueError(f"dimensions of type {t} and {n} bytes, not miINT32")
    dims = struct.unpack_from(f"<{n // 4}i", buf, start)
    t, n, start, pos = _tag(buf, pos)
    if t != MI_INT8 or start + n > len(buf):
        raise ValueError(f"a name of type {t} and {n} bytes, not miINT8 within the first {len(buf)} bytes")
    return _Var(bytes(buf[start:start + n]).decode("latin-1"), mclass, flags, dims, pos, at, nbytes, compressed)


def _variables(data: bytes, url: str) -> Iterator[_Var]:
    """The named variables of a MAT-file, in file order; only the first HEAD bytes of each are inflated."""
    if len(data) < HEADER or data[:len(MAGIC)] != MAGIC:
        raise ValueError(f"{url}: not a MATLAB v5 MAT-file: header={data[:20]!r} (a v7.3 file is HDF5, see "
                         "hdf5-zip-member)")
    if data[126:128] != b"IM":
        raise ValueError(f"{url}: endian indicator {data[126:128]!r}, not b'IM': a big-endian (b'MI') or damaged "
                         "MAT-file, which this snippet does not read")
    view, pos = memoryview(data), HEADER
    while pos < len(data):
        try:
            mtype, n = struct.unpack_from("<II", data, pos)
            if pos + 8 + n > len(data):
                raise ValueError(f"its {n} bytes run past the end of the file ({len(data)} bytes)")
            if mtype == MI_COMPRESSED:
                var = _header(zlib.decompressobj().decompress(view[pos + 8:pos + 8 + n], HEAD), pos + 8, n, True)
            elif mtype == MI_MATRIX:
                var = _header(view[pos:pos + min(8 + n, HEAD)], pos + 8, n, False)
            else:
                raise ValueError(f"type {mtype}, neither miMATRIX ({MI_MATRIX}) nor miCOMPRESSED ({MI_COMPRESSED})")
        except (ValueError, struct.error, zlib.error) as e:
            raise ValueError(f"{url}: the element at byte {pos} is not a MAT v5 variable: {e}") from None
        if var is not None:
            yield var
        pos += 8 + n        # top-level elements follow each other without padding


def _find(data: bytes, key: str, url: str) -> _Var:
    """Variable `key` of the file, checked to be one this snippet reads: real, numeric and 2-D."""
    seen = []
    for var in _variables(data, url):
        if var.name == key:
            break
        seen.append(var.describe())
    else:
        raise ValueError(f"{url}: no variable {key!r}; the file holds {', '.join(seen) or 'no variables'}")
    if var.mclass not in CLASSES:
        raise ValueError(f"{url}: variable {var.describe()} is not a numeric array")
    if var.flags & COMPLEX:
        raise ValueError(f"{url}: variable {var.describe()} is complex; this snippet reads real arrays")
    if len(var.dims) != 2 or min(var.dims) < 0:
        raise ValueError(f"{url}: variable {var.describe()} is not 2-D; this snippet reads one (rows, columns) "
                         "plane per file")
    return var


def _values(data: bytes, var: _Var, url: str) -> np.ndarray:
    """The variable as a C-ordered (rows, columns) array of its class's dtype."""
    count = var.dims[0] * var.dims[1]
    try:
        if var.compressed:
            limit = var.data + 16 + 8 * count       # a tag and `count` values of the widest type, padded
            buf = zlib.decompressobj().decompress(memoryview(data)[var.at:var.at + var.nbytes], limit + 1)
            if len(buf) > limit:
                raise ValueError(f"it inflates to more than the {limit} bytes its dimensions allow")
        else:
            buf = memoryview(data)[var.at - 8:var.at + var.nbytes]
        t, n, start, _ = _tag(buf, var.data)
        if t not in STORED:
            raise ValueError(f"its data is of type {t}, not a numeric type")
        stored = np.dtype(STORED[t])
        if n != count * stored.itemsize or start + n > len(buf):
            raise ValueError(f"its data is {n} bytes of {stored} ({len(buf) - start} present), not {count} values")
        a = np.frombuffer(buf, stored, count, start).reshape(var.dims, order="F")    # MATLAB stores columns first
    except (struct.error, zlib.error) as e:
        raise ValueError(f"{url}: variable {var.describe()} is damaged: {e}") from None
    except ValueError as e:
        raise ValueError(f"{url}: variable {var.describe()}: {e}") from None
    dtype = np.dtype(CLASSES[var.mclass])
    out = np.ascontiguousarray(a, dtype)    # MATLAB may store a class in a narrower type when the values fit
    if not np.can_cast(stored, dtype, "safe") and not np.array_equal(out, a):
        raise ValueError(f"{url}: variable {var.describe()} is stored as {stored} with values its class cannot hold")
    return out


class _Planes:
    def __init__(self, io: IO, urls: list[str], key: str):
        self._io, self._urls, self._key = io, urls, key
        var = _find(self._bytes(0), key, urls[0])           # headers only: nothing of the variable is inflated
        self._plane_shape, self.dtype = (int(var.dims[0]), int(var.dims[1])), np.dtype(CLASSES[var.mclass])
        self.axes = ("z" if len(urls) > 1 else "") + "yx"
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + self._plane_shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file, read by
        self.voxel_size_nm = None       # range (stored member, plain url) or staged (deflated member)
        self._last: tuple[int, np.ndarray] | None = None    # the last file decoded: patches of one plane follow

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = (region.get("y", slice(None)), region.get("x", slice(None)))
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
        """File k's variable as (Y, X), checked against file 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url, data = self._urls[k], self._bytes(k)
        a = _values(data, _find(data, self._key, url), url)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{url}: variable {self._key!r} is {a.dtype} {a.shape}, the first file's is "
                             f"{self.dtype} {self._plane_shape} (y, x); all files of one array must agree")
        self._last = (k, a)
        return a

    def _bytes(self, k: int) -> bytes:
        url = self._urls[k]
        try:
            with self._io.open(url) as fh:
                return fh.read()
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            return self._io.fetch(url).read_bytes()


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    key = src.params.get("key")
    if not isinstance(key, str) or not key:
        raise ValueError("params.key is required: the name of the MAT-file variable to read, e.g. 'inst_map'")
    urls = _expand(src.urls, io)
    if not urls:
        raise ValueError("the binding names no file")
    return _Planes(io, urls, key)
