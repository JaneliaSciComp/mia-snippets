"""Single-frame Nikon ND2 images (NIS-Elements, "Ver3.0" chunk files) from zip members or plain URLs, chosen by
glob or listed one by one, stacked along z; one file is yx.

The `nd2` package is not on the harness allowlist, so the container is parsed here. An ND2 file is a run of
chunks, each `<magic u32> <name length u32> <data length u64> <name, padded> <data>`. Its last 40 bytes point
at a chunk map (name -> offset). Three chunks are read:

- `ImageAttributesLV!`: width, height, row stride, components, bit depth, frame count, compression;
- `ImageMetadataSeqLV|0!`: `dCalibration` (um per pixel, x and y) and `bCalibrated`;
- `ImageDataSeq|0!`: an 8-byte time stamp, then the rows of the frame, top row first.

The two `LV` chunks are Nikon's "lite variant" records: `<type u8> <name length u8> <UTF-16 name> <value>`.
Files are opened lazily: `open_array` reads the first file's metadata only; `read` reads the rows it needs.
"""
import struct
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MAGIC = 0x0ABECEDA                                      # bytes da ce be 0a
FILE_SIGNATURE = b"ND2 FILE SIGNATURE CHUNK NAME01!"    # name of the first chunk; its data is the version
MAP_SIGNATURE = b"ND2 CHUNK MAP SIGNATURE 0000001!"     # the file's last 40 bytes: this, then the map's offset
MAP_NAME = b"ND2 FILEMAP SIGNATURE NAME 0001!"          # name of the chunk map's chunk
ATTRIBUTES, METADATA, FRAME = b"ImageAttributesLV!", b"ImageMetadataSeqLV|0!", b"ImageDataSeq|0!"
HEADER = struct.Struct("<IIQ")                          # magic, name length, data length
STAMP = 8                                               # the double (ms) before a frame's pixels
UNCOMPRESSED = 2                                        # eCompression: 0 lossless, 1 lossy, 2 none
MAX_META = 16 << 20                                     # no metadata chunk of a real file comes near this
SCALARS = {1: "<?", 2: "<i", 3: "<I", 4: "<q", 5: "<Q", 6: "<d", 7: "<Q"}   # lite variant type -> struct
STRING, BYTES, LEVEL = 8, 9, 11


def _chunk(fh: BinaryIO, at: int, name: bytes, url: str) -> tuple[int, int]:
    """(offset, length) of the data of the chunk at `at`, after checking its magic and name."""
    fh.seek(at)
    head = fh.read(HEADER.size + len(name))
    if len(head) != HEADER.size + len(name):
        raise ValueError(f"{url}: file ends inside the chunk header at {at}")
    magic, name_len, data_len = HEADER.unpack_from(head)
    if magic != MAGIC or head[HEADER.size:] != name:
        raise ValueError(f"{url}: no ND2 chunk {name.decode('latin-1')!r} at offset {at}")
    return at + HEADER.size + name_len, data_len


def _data(fh: BinaryIO, at: int, name: bytes, url: str) -> bytes:
    """The data of a (small) metadata chunk."""
    offset, length = _chunk(fh, at, name, url)
    if length > MAX_META:
        raise ValueError(f"{url}: chunk {name.decode('latin-1')!r} is {length} bytes long")
    fh.seek(offset)
    data = fh.read(length)
    if len(data) != length:
        raise ValueError(f"{url}: file ends inside chunk {name.decode('latin-1')!r}")
    return data


def _chunk_map(fh: BinaryIO, url: str) -> dict[bytes, int]:
    """Chunk name -> offset of the chunk, from the map the file's last 40 bytes point at."""
    size = fh.seek(0, 2)
    offset, length = _chunk(fh, 0, FILE_SIGNATURE, url)      # raises on a file that is not ND2
    fh.seek(offset)
    version = fh.read(min(length, 8))
    if not version.startswith(b"Ver3."):
        raise ValueError(f"{url}: ND2 version {version.rstrip(bytes(1))!r}; this snippet reads Ver3.x chunk files")
    fh.seek(size - 40)
    signature, at = struct.unpack("<32sQ", fh.read(40))
    if signature != MAP_SIGNATURE or at >= size:
        raise ValueError(f"{url}: the file does not end in an ND2 chunk map pointer (truncated?)")
    data = _data(fh, at, MAP_NAME, url)
    chunks: dict[bytes, int] = {}
    pos = 0
    while True:                         # entries: name ending in "!", offset u64, size u64
        end = data.find(b"!", pos) + 1
        if end == 0 or data[pos:end] == MAP_SIGNATURE:
            return chunks
        if end + 16 > len(data):
            raise ValueError(f"{url}: the ND2 chunk map ends inside an entry")
        chunks[data[pos:end]] = struct.unpack_from("<Q", data, end)[0]
        pos = end + 16


def _level(data: bytes, url: str) -> dict[str, object]:
    """The scalar and string items of a lite variant chunk's one top-level record; nested records, which this
    snippet does not need, are skipped by their stated length."""
    def items(pos: int, end: int, count: int) -> dict[str, object]:
        out: dict[str, object] = {}
        for _ in range(count):
            start = pos
            kind, name_len = data[pos], data[pos + 1]
            pos += 2
            name = data[pos:pos + 2 * name_len].decode("utf-16-le").rstrip("\0")
            pos += 2 * name_len
            if kind in SCALARS:
                out[name] = struct.unpack_from(SCALARS[kind], data, pos)[0]
                pos += struct.calcsize(SCALARS[kind])
            elif kind == STRING:
                stop = pos
                while data[stop:stop + 2] not in (b"\0\0", b""):
                    stop += 2
                out[name] = data[pos:stop].decode("utf-16-le")
                pos = stop + 2
            elif kind == BYTES:
                pos += 8 + struct.unpack_from("<Q", data, pos)[0]
            elif kind == LEVEL:          # <item count u32> <length from the item's start u64> items, offsets
                n, length = struct.unpack_from("<IQ", data, pos)
                pos = start + length + 8 * n
            else:
                raise ValueError(f"lite variant item {name!r} has unknown type {kind}")
            if pos > end:
                raise ValueError(f"lite variant item {name!r} runs past its record")
        return out

    try:
        if data[0] != LEVEL:
            raise ValueError(f"the chunk starts with type {data[0]}, not a record (compressed metadata?)")
        head = 2 + 2 * data[1]
        n, length = struct.unpack_from("<IQ", data, head)
        return items(head + 12, min(length, len(data)), n)
    except (IndexError, struct.error, UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"{url}: cannot parse ND2 metadata: {e}") from None


def _uint(record: dict[str, object], key: str, url: str) -> int:
    v = record.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"{url}: ND2 image attribute {key} is {v!r}, not a count")
    return v


class _Frame:
    """What one file says about its one frame."""

    def __init__(self, fh: BinaryIO, url: str):
        chunks = _chunk_map(fh, url)
        for name in (ATTRIBUTES, FRAME):
            if name not in chunks:
                raise ValueError(f"{url}: the ND2 chunk map has no {name.decode()!r}")
        a = _level(_data(fh, chunks[ATTRIBUTES], ATTRIBUTES, url), url)
        nx, ny, stride = (_uint(a, k, url) for k in ("uiWidth", "uiHeight", "uiWidthBytes"))
        comp, bits = _uint(a, "uiComp", url), _uint(a, "uiBpcInMemory", url)
        frames, compression = _uint(a, "uiSequenceCount", url), _uint(a, "eCompression", url)
        if frames != 1 or comp != 1:
            raise ValueError(f"{url}: {frames} frame(s) of {comp} component(s); this snippet reads ND2 files "
                             "that hold one frame of one component (one 2D grey image)")
        if compression != UNCOMPRESSED:
            raise ValueError(f"{url}: eCompression {compression}; this snippet reads uncompressed frames only")
        if bits not in (8, 16):
            raise ValueError(f"{url}: {bits} bits per component in memory; this snippet reads 8 and 16")
        self.shape, self.stride = (ny, nx), stride
        self.dtype = np.dtype(f"<u{bits // 8}")
        offset, length = _chunk(fh, chunks[FRAME], FRAME, url)
        if nx < 1 or ny < 1 or stride < nx * self.dtype.itemsize or length != STAMP + ny * stride:
            raise ValueError(f"{url}: frame chunk of {length} bytes for {ny} rows of {stride} bytes "
                             f"({nx} px of {bits} bits)")
        self.offset = offset + STAMP                    # file offset of the top row
        self.voxel_size_nm: dict[str, float] | None = None
        if METADATA in chunks:
            m = _level(_data(fh, chunks[METADATA], METADATA, url), url)
            um = m.get("dCalibration")
            if m.get("bCalibrated") is True and isinstance(um, float) and um > 0:
                self.voxel_size_nm = {"y": um * 1e3, "x": um * 1e3}     # square pixels: one calibration


class _Stack:
    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._frames: dict[int, _Frame] = {}            # per file, parsed once
        fh, ranged = self._open(0)
        with fh:
            first = self._frame(0, fh)
        ny, nx = first.shape
        self.shape: tuple[int, ...] = (len(urls), ny, nx) if len(urls) > 1 else (ny, nx)
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.dtype = first.dtype.newbyteorder("=")
        self.voxel_size_nm = first.voxel_size_nm        # of the first file; z is a file index, not a length
        # Several files: one file per plane, so the plane is the read unit; the harness then draws its crops
        # plane by plane and each file is asked for once, not once per crop (every file costs requests of its
        # own: its metadata by range, or the whole of a deflated member). One file: rows by range from a stored
        # member or a plain URL; a deflated member is staged whole, here at open.
        self.access = "plane" if len(urls) > 1 else "chunk" if ranged else "file"

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0, y, x)
        sel = region.get("z", slice(None))
        if isinstance(sel, slice):
            planes = [self._plane(k, y, x) for k in range(self.shape[0])[sel]]
            return np.stack(planes) if planes else np.empty((0, *self.shape[1:]), self.dtype)[:, y, x]
        return self._plane(range(self.shape[0])[sel], y, x)     # IndexError when out of range, like numpy

    def _open(self, k: int) -> tuple[BinaryIO, bool]:
        """(file k, whether it is read by range)."""
        try:
            return self._io.open(self._urls[k]), True
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            return open(self._io.fetch(self._urls[k]), "rb"), False

    def _frame(self, k: int, fh: BinaryIO) -> _Frame:
        """File k's frame layout, checked against file 0's."""
        if k not in self._frames:
            frame = _Frame(fh, self._urls[k])
            if k > 0:
                first = self._frames[0]
                if frame.shape != first.shape or frame.dtype != first.dtype:
                    raise ValueError(f"{self._urls[k]}: {frame.dtype} {frame.shape} differs from the first "
                                     f"file's {first.dtype} {first.shape}; all files of one array must agree")
            self._frames[k] = frame
        return self._frames[k]

    def _plane(self, k: int, y: int | slice, x: int | slice) -> np.ndarray:
        ny, nx = self.shape[-2:]
        rows = range(ny)[y] if isinstance(y, slice) else range(range(ny)[y], range(ny)[y] + 1)
        if len(rows) == 0:
            return np.empty((0, nx), self.dtype)[:, x]
        lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
        fh, _ = self._open(k)
        with fh:
            frame = self._frame(k, fh)
            fh.seek(frame.offset + lo * frame.stride)
            buf = fh.read((hi - lo) * frame.stride)
        if len(buf) != (hi - lo) * frame.stride:
            raise ValueError(f"{self._urls[k]}: file ends inside the pixel data")
        # a row is `stride` bytes, of which the first nx pixels are the image (the rest pads the row)
        block = np.ndarray((hi - lo, nx), frame.dtype, buf, strides=(frame.stride, frame.dtype.itemsize))
        if not isinstance(y, slice):
            return block[0, x].astype(self.dtype)
        picked = block if rows.step == 1 else block[np.arange(rows.start, rows.stop, rows.step) - lo]
        return picked[:, x].astype(self.dtype)          # astype copies: the result does not hold `buf`


def open_array(src: Source, io: IO) -> Array:
    urls = [u for url in src.urls for u in io.expand(url)]      # a glob url as its files, sorted by name
    if not urls:
        raise ValueError("nd2-zip-glob needs at least one url; the glob matched no file")
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return _Stack(io, urls)
