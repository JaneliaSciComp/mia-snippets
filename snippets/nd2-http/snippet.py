"""One Nikon ND2 file (NIS-Elements, container "Ver3.0") at a plain URL, as cyx: one uncompressed frame.

The container is parsed here (the nd2 package is not on the allowlist). A file is a run of chunks, each a 16-byte
header (magic 0x0ABECEDA, name length, data length, little-endian), a NUL-padded name ending in "!" and the data.
Its last 40 bytes point at a chunk map naming every chunk's offset. `open_array` reads that map, the
`ImageAttributesLV!` chunk (size, components, bit depth, compression) and `ImageCalibrationLV|0!` (pixel size);
both are in Nikon's "lite variant" binary encoding, decoded by `_lite_variant`. Pixels are in
`ImageDataSeq|<frame>!`: an 8-byte timestamp, then rows of `uiWidthBytes` bytes with the components interleaved
(y, x, c). `read` asks for the rows of the region as one byte range and returns them as c, y, x.
"""
import math
import struct
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MAGIC = 0x0ABECEDA
FILE_SIGNATURE = b"ND2 FILE SIGNATURE CHUNK NAME01!"
MAP_SIGNATURE = b"ND2 CHUNK MAP SIGNATURE 0000001!"
MAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"
MAX_META = 16 << 20         # the largest attributes or calibration chunk decoded (they are a few hundred bytes)
MAX_DEPTH = 16              # nesting of a lite variant
SCALARS = {2: "<i", 3: "<I", 4: "<q", 5: "<Q", 6: "<d", 7: "<Q"}    # lite variant type -> struct format
COMPRESSION = {0: "lossless (zlib)", 1: "lossy"}                    # eCompression; 2 is none


def _lite_variant(data: bytes, pos: int, end: int, depth: int = 0) -> dict[str, object]:
    """Decode the entries in data[pos:end]: each a type byte, a name length in UTF-16 units, the name, the value.
    A level (type 11) nests; byte arrays are skipped. Raises ValueError on anything that does not fit."""
    out: dict[str, object] = {}
    try:
        while pos < end:
            start = pos
            kind, n = data[pos], data[pos + 1]
            pos += 2
            name = data[pos:pos + 2 * n].decode("utf-16-le").rstrip("\0")
            pos += 2 * n
            if kind == 1:
                value: object = data[pos] != 0
                pos += 1
            elif kind in SCALARS:
                value = struct.unpack_from(SCALARS[kind], data, pos)[0]
                pos += struct.calcsize(SCALARS[kind])
            elif kind == 8:                 # NUL-terminated UTF-16 string
                stop = pos
                while data[stop:stop + 2] not in (b"\0\0", b""):
                    stop += 2
                value = data[pos:stop].decode("utf-16-le")
                pos = stop + 2
            elif kind == 9:                 # byte array: skipped
                value = None
                pos += 8 + struct.unpack_from("<Q", data, pos)[0]
            elif kind == 11:                # level: item count, length from the entry's start, entries, offsets
                count, length = struct.unpack_from("<IQ", data, pos)
                if depth >= MAX_DEPTH or not pos + 12 <= start + length <= end:
                    raise ValueError(f"bad level {name!r}")
                value = _lite_variant(data, pos + 12, start + length, depth + 1)
                pos = start + length + 8 * count
            else:
                raise ValueError(f"unknown entry type {kind} for {name!r}")
            if pos > end:
                raise ValueError(f"entry {name!r} runs past its level")
            out[name] = value
    except (IndexError, struct.error, UnicodeDecodeError) as e:
        raise ValueError(f"truncated or malformed lite variant: {e}") from None
    return out


class _Nd2:
    def __init__(self, io: IO, url: str, frame: int | None):
        self._url = url
        self._fh: BinaryIO
        try:
            self._fh, self.access = io.open(url), "chunk"
            self._fh.read(1)
        except NeedsFetch:      # no Content-Length, or a server without Range: stage the whole file
            self._fh, self.access = open(io.fetch(url), "rb"), "file"
        self._size = self._fh.seek(0, 2)
        if self._size < 16 + len(FILE_SIGNATURE) + 40:
            raise ValueError(f"{url}: {self._size} bytes, too short for an ND2 file")
        if struct.unpack("<I", self._at(0, 4))[0] != MAGIC or self._at(16, 32) != FILE_SIGNATURE:
            raise ValueError(f"{url}: not an ND2 file (no chunk magic and file signature at the start); "
                             "the older JPEG2000-based ND2 is not read")
        version = self._chunk(0, FILE_SIGNATURE, 64)[:6]
        if version != b"Ver3.0":
            raise ValueError(f"{url}: ND2 container version {version!r}, only 'Ver3.0' is read")
        self._chunks = self._chunk_map()

        attrs = self._variant(b"ImageAttributesLV!").get("SLxImageAttributes")
        if not isinstance(attrs, dict):
            raise ValueError(f"{url}: ImageAttributesLV has no SLxImageAttributes")
        try:
            width, height, comp = (self._count(attrs, k) for k in ("uiWidth", "uiHeight", "uiComp"))
            bits = self._count(attrs, "uiBpcInMemory")
            frames = self._count(attrs, "uiSequenceCount")
        except KeyError as e:
            raise ValueError(f"{url}: SLxImageAttributes has no {e.args[0]}") from None
        compression = attrs.get("eCompression", 2)
        if compression != 2:
            raise ValueError(f"{url}: eCompression {compression!r} "
                             f"({COMPRESSION.get(compression, 'unknown')}); only uncompressed frames are read")
        if bits not in (8, 16) or attrs.get("ePixelType", 1) != 1:
            raise ValueError(f"{url}: uiBpcInMemory {bits}, ePixelType {attrs.get('ePixelType')!r}; "
                             "only unsigned 8- and 16-bit pixels are read")
        self.dtype = np.dtype(np.uint8 if bits == 8 else np.uint16)
        self._stored = self.dtype.newbyteorder("<")
        packed = width * comp * self._stored.itemsize
        self._row = attrs.get("uiWidthBytes", packed)     # bytes from one row to the next; may pad the row
        if not isinstance(self._row, int) or self._row < packed:
            raise ValueError(f"{url}: uiWidthBytes {self._row!r} is less than a row of {packed} bytes")
        self._comp, self._ny, self._nx = comp, height, width
        self.axes = "cyx"
        self.shape: tuple[int, ...] = (comp, height, width)

        if frame is None:
            if frames != 1:
                raise ValueError(f"{url}: the file holds {frames} frames (uiSequenceCount); "
                                 "set params.frame to the one to read")
            frame = 0
        if not 0 <= frame < frames:
            raise ValueError(f"{url}: params.frame {frame} is outside the file's {frames} frames")
        name = b"ImageDataSeq|%d!" % frame
        if name not in self._chunks:
            raise ValueError(f"{url}: the chunk map has no {name.decode()}")
        start, length = self._header(self._chunks[name], name)
        if length < 8 + height * self._row:
            raise ValueError(f"{url}: {name.decode()} holds {length} bytes, less than a timestamp and "
                             f"{height} rows of {self._row} bytes")
        self._data = start + 8      # after the frame's timestamp (a float64)

        self.voxel_size_nm: dict[str, float] | None = None
        if b"ImageCalibrationLV|0!" in self._chunks:
            cal = self._variant(b"ImageCalibrationLV|0!").get("SLxCalibration")
            um = cal.get("dCalibration") if isinstance(cal, dict) else None
            # dAspect other than 1 is not interpreted (which axis it scales is not documented): no voxel size
            if (isinstance(cal, dict) and cal.get("bValid") is True and isinstance(um, float)
                    and math.isfinite(um) and um > 0 and cal.get("dAspect", 1.0) == 1.0):
                self.voxel_size_nm = {"y": um * 1000.0, "x": um * 1000.0}

    @staticmethod
    def _count(attrs: dict, key: str) -> int:
        value = attrs[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 1 << 31:
            raise ValueError(f"SLxImageAttributes.{key} is {value!r}")
        return value

    def _at(self, offset: int, n: int) -> bytes:
        self._fh.seek(offset)
        buf = self._fh.read(n)
        if len(buf) != n:
            raise ValueError(f"{self._url}: file ends inside bytes {offset}..{offset + n}")
        return buf

    def _header(self, offset: int, name: bytes) -> tuple[int, int]:
        """(data offset, data length) of the chunk whose header is at `offset`, which must be named `name`."""
        if not 0 <= offset <= self._size - 16:
            raise ValueError(f"{self._url}: chunk {name.decode()} at {offset} is outside the file")
        magic, name_len, length = struct.unpack("<IIQ", self._at(offset, 16))
        start = offset + 16 + name_len
        if magic != MAGIC or name_len < len(name) or start + length > self._size:
            raise ValueError(f"{self._url}: no valid chunk header for {name.decode()} at {offset}")
        if self._at(offset + 16, len(name)) != name:
            raise ValueError(f"{self._url}: the chunk at {offset} is not {name.decode()}")
        return start, length

    def _chunk(self, offset: int, name: bytes, limit: int) -> bytes:
        start, length = self._header(offset, name)
        if length > limit:
            raise ValueError(f"{self._url}: chunk {name.decode()} is {length} bytes, more than {limit}")
        return self._at(start, length)

    def _chunk_map(self) -> dict[bytes, int]:
        """Chunk name -> offset of its header, from the map the file's last 40 bytes point at."""
        tail = self._at(self._size - 40, 40)
        if tail[:32] != MAP_SIGNATURE:
            raise ValueError(f"{self._url}: no chunk map signature at the end of the file (truncated?)")
        data = self._chunk(struct.unpack("<Q", tail[32:])[0], MAP_NAME, self._size)
        chunks: dict[bytes, int] = {}
        pos = 0
        while True:     # entries: a name ending in "!", then offset and size as uint64; the signature ends them
            stop = data.find(b"!", pos)
            if stop < 0 or stop + 17 > len(data) or data[pos:stop + 1] == MAP_SIGNATURE:
                return chunks
            chunks[data[pos:stop + 1]] = struct.unpack_from("<Q", data, stop + 1)[0]
            pos = stop + 17

    def _variant(self, name: bytes) -> dict[str, object]:
        if name not in self._chunks:
            raise ValueError(f"{self._url}: the chunk map has no {name.decode()}")
        data = self._chunk(self._chunks[name], name, MAX_META)
        try:
            return _lite_variant(data, 0, len(data))
        except ValueError as e:
            raise ValueError(f"{self._url}: {name.decode()}: {e}") from None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        c, y, x = (region.get(a, slice(None)) for a in "cyx")
        ny, nx, comp = self._ny, self._nx, self._comp
        rows = range(ny)[y] if isinstance(y, slice) else range(range(ny)[y], range(ny)[y] + 1)
        if len(rows) == 0:
            return np.empty((comp, ny, nx), self.dtype)[c, y, x]
        lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
        buf = self._at(self._data + lo * self._row, (hi - lo) * self._row)      # rows lo..hi: one range
        packed = nx * comp * self._stored.itemsize
        block = np.frombuffer(buf, np.uint8).reshape(hi - lo, self._row)[:, :packed]    # drop any row padding
        block = np.ascontiguousarray(block).view(self._stored).reshape(hi - lo, nx, comp)
        out = block.transpose(2, 0, 1)[c]                   # (c, rows, x), or (rows, x) for one component
        if not isinstance(y, slice):
            out = out[..., 0, :]
        elif rows.step != 1:
            out = out[..., np.asarray(rows) - lo, :]
        return np.ascontiguousarray(out[..., x]).astype(self.dtype, copy=False)


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"nd2-http reads one file, got {len(src.urls)} urls")
    frame = src.params.get("frame")
    if frame is not None and (isinstance(frame, bool) or not isinstance(frame, int) or frame < 0):
        raise ValueError(f"params.frame must be a frame index from 0, got {frame!r}")
    return _Nd2(io, src.urls[0], frame)
