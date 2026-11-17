"""One 2D numeric or logical variable of a MATLAB 5 MAT-file (save -v6 / -v7, not -v7.3) as a yx array, or
several same-sized variables combined into one uint8 mask (`union`) or id map (`ids`).

The file is a 128-byte header and one data element per variable; -v7 files wrap each in a zlib stream. Opening
reads the element tags and the first bytes of each variable's header (class, dims, name). The first `read`
inflates the named variables whole: a zlib stream has no random access.

Requests are kept few, because a host may allow only so many a minute (Zenodo: 133, and it answers the next
one 429): the stretch of the file the named variables are expected to fill is asked for at once, and a long
variable is read in long pieces, which `io` splits into a few ranges of 8 MiB.
"""
import struct
import zlib
from dataclasses import dataclass, replace
from typing import BinaryIO, Iterator

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MI_MATRIX, MI_COMPRESSED = 14, 15
# data element types (how the numbers are stored) and array classes (what the variable is in MATLAB)
MI_TYPES = {1: "i1", 2: "u1", 3: "i2", 4: "u2", 5: "i4", 6: "u4", 7: "f4", 9: "f8", 12: "i8", 13: "u8"}
MX_CLASSES = {6: "f8", 7: "f4", 8: "i1", 9: "u1", 10: "i2", 11: "u2", 12: "i4", 13: "u4", 14: "i8", 15: "u8"}
MX_NAMES = {1: "cell", 2: "struct", 3: "object", 4: "char", 5: "sparse", 16: "function", 17: "opaque"}
FLAG_COMPLEX, FLAG_LOGICAL = 0x08, 0x02
HEAD = 512              # inflated bytes that hold a variable's flags, dims, name and data tag
PIECE = 1 << 20         # inflated bytes per step (x4), and columns' worth of work per step when combining
AHEAD = 8 << 20         # most bytes asked for ahead of the walk through the variables: one request of io's
GULP = 32 << 20         # compressed bytes per read of a variable: io asks for them as 4 ranges of 8 MiB at once
COMBINE = ("union", "ids")


@dataclass(frozen=True)
class _Var:
    name: str
    offset: int             # file offset of the top-level element's tag
    nbytes: int             # length of the top-level element's payload in the file
    compressed: bool
    mx_class: int
    logical: bool
    is_complex: bool
    dims: tuple[int, ...]
    stored: str | None = None    # numpy type code of the stored numbers, None for a class this snippet can't read
    data_start: int = 0     # offset of the numbers in the matrix element's (inflated) stream, tag included
    data_nbytes: int = 0


def _tag(buf: bytes, pos: int, e: str) -> tuple[int, int, int, int]:
    """A sub-element's (type, nbytes, data offset, offset of the next sub-element); handles the small format,
    where type and length share the first word and up to 4 data bytes follow it."""
    word, = struct.unpack_from(e + "I", buf, pos)
    if word >> 16:
        return word & 0xFFFF, word >> 16, pos + 4, pos + 8
    n, = struct.unpack_from(e + "I", buf, pos + 4)
    return word, n, pos + 8, pos + 8 + n + (-n % 8)


def _header(head: bytes, e: str, offset: int, nbytes: int, compressed: bool) -> _Var | None:
    """Parse the start of a miMATRIX element (its own tag first). None for an empty or unnamed element."""
    kind, size = struct.unpack_from(e + "II", head, 0)
    if kind != MI_MATRIX:
        raise ValueError(f"element at byte {offset} holds data type {kind}, not a miMATRIX")
    if size == 0:
        return None
    _, _, at, pos = _tag(head, 8, e)
    flags, = struct.unpack_from(e + "I", head, at)
    _, n, at, pos = _tag(head, pos, e)
    dims = struct.unpack_from(f"{e}{n // 4}i", head, at)
    _, n, at, pos = _tag(head, pos, e)
    name = head[at:at + n].decode("ascii", "replace")
    if len(head) < at + n:
        raise struct.error("name cut off")
    var = _Var(name, offset, nbytes, compressed, flags & 0xFF, bool(flags >> 8 & FLAG_LOGICAL),
               bool(flags >> 8 & FLAG_COMPLEX), tuple(int(d) for d in dims))
    if var.mx_class not in MX_CLASSES:
        return var
    stored, n, at, _ = _tag(head, pos, e)
    return replace(var, stored=MI_TYPES.get(stored), data_start=at, data_nbytes=n)


def _endian(fh: BinaryIO) -> str:
    fh.seek(0)
    header = fh.read(128)
    if header[:19] == b"MATLAB 7.3 MAT-file" or header[:8] == b"\x89HDF\r\n\x1a\n":
        raise ValueError("this is a MATLAB 7.3 MAT-file, which is HDF5: bind it with an HDF5 snippet")
    if len(header) < 128 or not header.startswith(b"MATLAB 5.0 MAT-file") or header[126:128] not in (b"IM", b"MI"):
        raise ValueError(f"not a MATLAB 5.0 MAT-file: the file starts with {header[:20]!r}")
    return "<" if header[126:128] == b"IM" else ">"


def _variables(fh: BinaryIO, e: str) -> Iterator[_Var]:
    """Every named variable, in file order, from the element tags and the first bytes of each element."""
    pos = 128
    while True:
        fh.seek(pos)
        tag = fh.read(8)
        if len(tag) < 8:
            return
        kind, n = struct.unpack(e + "II", tag)
        if kind >> 16:
            raise ValueError(f"byte {pos} is not a top-level MAT element tag ({tag.hex()})")
        try:
            if kind == MI_COMPRESSED:
                var = _header(zlib.decompressobj().decompress(fh.read(min(n, 4 * HEAD)), HEAD), e, pos, n, True)
            elif kind == MI_MATRIX:
                var = _header(tag + fh.read(min(n, HEAD)), e, pos, n, False)
            else:
                var = None
        except (struct.error, zlib.error) as err:
            raise ValueError(f"cannot parse the variable header at byte {pos}: {err}") from err
        if var is not None:
            yield var
        pos += 8 + n


def _stream(fh: BinaryIO, var: _Var) -> Iterator[bytes]:
    """The variable's miMATRIX element, tag first, in pieces: inflated when the file wraps it in zlib."""
    left = var.nbytes if var.compressed else var.nbytes + 8
    fh.seek(var.offset + 8 if var.compressed else var.offset)
    inflate = zlib.decompressobj() if var.compressed else None
    while left > 0:
        chunk = fh.read(min(left, GULP))    # not 1 MiB at a time: that is one request per MiB, one by one
        if not chunk:
            raise ValueError(f"variable {var.name!r} is cut off: the file ends {left} bytes early")
        left -= len(chunk)
        if inflate is None:
            yield chunk
            continue
        while chunk:
            yield inflate.decompress(chunk, 4 * PIECE)      # bounded: a mask inflates several hundredfold
            chunk = inflate.unconsumed_tail
    if inflate is not None:
        yield inflate.flush()


def _load(fh: BinaryIO, var: _Var, e: str) -> np.ndarray:
    """The variable as a (rows, columns) array of its MATLAB class. MATLAB stores column after column."""
    stored = np.dtype(e + str(var.stored))
    out = np.empty(var.data_nbytes // stored.itemsize, dtype=stored)
    buf, filled, skip = memoryview(out).cast("B"), 0, var.data_start
    for piece in _stream(fh, var):
        if skip:
            piece, skip = piece[skip:], max(0, skip - len(piece))
        take = min(len(piece), len(buf) - filled)
        buf[filled:filled + take] = piece[:take]
        filled += take
        if filled == len(buf):
            break
    if filled < len(buf):
        raise ValueError(f"variable {var.name!r} holds {filled} data bytes, its header says {len(buf)}")
    return out.astype(np.dtype(MX_CLASSES[var.mx_class]), copy=False).reshape(var.dims, order="F")


class _Variables:
    def __init__(self, fh: BinaryIO, e: str, variables: list[_Var], combine: str | None):
        self._fh, self._e, self._vars, self._combine = fh, e, variables, combine
        self._data: np.ndarray | None = None
        self.axes = "yx"
        self.shape = variables[0].dims
        self.dtype = np.dtype(MX_CLASSES[variables[0].mx_class] if combine is None else "u1")
        self.voxel_size_nm = None       # a MAT-file has no place for it
        self.access = "file"            # the first read inflates the named variables whole

    def _whole(self) -> np.ndarray:
        if self._combine is None:
            return _load(self._fh, self._vars[0], self._e)
        out = np.zeros(self.shape, dtype=np.uint8, order="F")
        step = max(1, PIECE // max(1, self.shape[0]))
        for number, var in enumerate(self._vars, start=1):
            data = _load(self._fh, var, self._e)
            for x in range(0, self.shape[1], step):
                on, sub = data[:, x:x + step] != 0, out[:, x:x + step]
                if self._combine == "union":
                    sub[on] = 1
                    continue
                if sub[on].any():
                    other = self._vars[int(sub[on].max()) - 1].name
                    raise ValueError(f"variables {other!r} and {var.name!r} overlap, so one id per pixel cannot "
                                     "hold them: use combine 'union', or bind them one by one")
                sub[on] = number
        return out

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._data is None:
            self._data = self._whole()
        return np.ascontiguousarray(self._data[region.get("y", slice(None)), region.get("x", slice(None))])


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"mat5-variable reads one file, got {len(src.urls)} urls")
    wanted = src.params.get("variable")
    names = [n.strip() for n in wanted.split(",")] if isinstance(wanted, str) else []
    if not names or not all(names) or len(set(names)) != len(names):
        raise ValueError("mat5-variable needs params.variable: a variable name, or several different names "
                         f"separated by commas (e.g. 'BW1,BW2'), got {wanted!r}")
    combine = src.params.get("combine", "union" if len(names) > 1 else None)
    if combine is not None and combine not in COMBINE:
        raise ValueError(f"params.combine must be one of {COMBINE}, got {combine!r}")
    if combine == "ids" and len(names) > 255:
        raise ValueError(f"combine 'ids' numbers at most 255 variables in uint8, got {len(names)}")
    try:
        fh: BinaryIO = io.open(src.urls[0])
    except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
        fh = io.fetch(src.urls[0]).open("rb")
    e = _endian(fh)
    found: dict[str, _Var] = {}
    seen: list[str] = []
    for var in _variables(fh, e):       # stops at the last wanted name: later elements are not touched
        seen.append(var.name)
        if var.name in names:
            found[var.name] = var
            left = len(names) - len(found)
            if not left:
                break
            # The names still looked for are most often the next elements, about as long as this one (BW1, BW2,
            # ... of one size). Without this, each of their headers is a request of its own; with it the stretch
            # they would fill, which the first read needs anyway, is one request (the bytes land in io's cache).
            fh.seek(var.offset)
            fh.read(min((8 + var.nbytes) * (left + 1) + 4 * HEAD, AHEAD))
    missing = [n for n in names if n not in found]
    if missing:
        raise KeyError(f"no variable {missing} in the file; variables: {seen}")
    variables = [found[n] for n in names]
    for var in variables:
        what = f"variable {var.name!r}"
        if var.mx_class not in MX_CLASSES:
            raise ValueError(f"{what} is a {MX_NAMES.get(var.mx_class, f'class {var.mx_class}')} array, "
                             "not a numeric or logical one")
        if var.is_complex:
            raise ValueError(f"{what} is complex")
        if len(var.dims) != 2 or min(var.dims) < 1:
            raise ValueError(f"{what} has dims {var.dims}; this snippet reads 2D variables only")
        count = var.dims[0] * var.dims[1]
        if var.stored is None or var.data_nbytes != count * np.dtype(var.stored).itemsize:
            raise ValueError(f"{what}: {var.data_nbytes} data bytes of element type {var.stored} do not hold "
                             f"{var.dims[0]}x{var.dims[1]} numbers")
        if var.dims != variables[0].dims:
            raise ValueError(f"{what} is {var.dims}, {variables[0].name!r} is {variables[0].dims}: variables "
                             "combined into one array must have the same size")
    return _Variables(fh, e, variables, combine)
