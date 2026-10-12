"""Zip members over range reads: central directory from the tail, stored members by range, deflated by fetch."""
import struct
import zlib
from pathlib import Path
from typing import BinaryIO, Iterable
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo


def read_directory(f: BinaryIO) -> dict[str, ZipInfo]:
    """Members by name. `f` is a range file over the whole zip.

    stdlib zipfile does the parsing: it reads the end-of-central-directory record from the last 64 KiB
    (plus the ZIP64 locator and record when present), then the central directory, through `f`'s range reads.
    """
    with ZipFile(f) as z:                       # closing a ZipFile leaves a passed-in file open
        return {i.filename: i for i in z.infolist()}


def data_offset(header: bytes, info: ZipInfo) -> int:
    """Where the member's bytes start, from the 30 bytes of its local header (whose extra field may differ
    from the CD's)."""
    if info.flag_bits & 1:
        raise BadZipFile(f"{info.filename!r} is encrypted")
    if info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
        raise BadZipFile(f"{info.filename!r}: unsupported compression method {info.compress_type}")
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise BadZipFile(f"bad local header for {info.filename!r}")
    name_len, extra_len = struct.unpack("<2H", header[26:30])
    return info.header_offset + 30 + name_len + extra_len


def extract(chunks: Iterable[bytes], info: ZipInfo, out: Path) -> None:
    """Write the member's (inflated) bytes from its compressed `chunks` to `out`, checking size and CRC32."""
    d = zlib.decompressobj(-15) if info.compress_type == ZIP_DEFLATED else None

    def pieces():
        for c in chunks:
            yield d.decompress(c) if d else c
        if d:
            yield d.flush()

    crc = size = 0
    with open(out, "wb") as fh:
        for piece in pieces():
            crc, size = zlib.crc32(piece, crc), size + len(piece)
            if size > info.file_size:           # don't let a lying header fill the disk
                break
            fh.write(piece)
    if (crc, size) != (info.CRC, info.file_size):
        raise BadZipFile(f"{info.filename!r}: CRC32/size mismatch")


def crc_ok(path: Path, info: ZipInfo) -> bool:
    if path.stat().st_size != info.file_size:
        return False
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return crc == info.CRC
