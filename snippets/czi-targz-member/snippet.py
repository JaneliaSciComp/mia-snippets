"""One Zeiss CZI z-stack that is a member of a gzip-compressed tar archive, as `czyx` (or `zyx` with
`channel: <k>`), parsed with the standard library: uncompressed, single-tile, single-scene, single-timepoint
subblocks of one 2D plane each.

A tar.gz has no index and cannot be read by range, so a member is reached by inflating the archive from its first
byte: the compressed bytes up to the member's end come through `io.open` (range requests, cached in blocks, counted
against the job's cap) and nothing behind it is asked for. The archive is never fetched whole."""
import struct
import tarfile
from typing import BinaryIO, NamedTuple
from xml.etree import ElementTree

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

HEAD_READ = 1 << 20     # compressed bytes asked of io at a time while only headers are wanted: one cache block
LOAD_READ = 32 << 20    # ... and while the member is loaded: four 8 MiB range requests side by side
PIXEL = {0: "u1", 1: "<u2", 2: "<f4", 12: "<u4"}    # CZI pixel type -> dtype: Gray8, Gray16, Gray32Float, Gray32
UNCOMPRESSED = 0


class _Entry(NamedTuple):
    """A subblock's directory entry (DirectoryEntryDV)."""
    pixel: int
    compression: int
    pyramid: int
    dims: dict[str, tuple[int, int, int]]      # dimension letter -> (start, size, stored size)


class _Layout(NamedTuple):
    dtype: np.dtype
    cs: list[int]           # the C starts of the channels, in order
    zs: list[int]           # the Z starts of the planes, in order
    y: int
    x: int


class _Sequential:
    """A forward-only reader over a tar member's stream that knows its position in the member."""

    def __init__(self, f: BinaryIO):
        self._f, self.pos = f, 0

    def read(self, n: int) -> bytes:
        data = self._f.read(n)
        if len(data) != n:
            raise ValueError(f"CZI is truncated: {n} bytes wanted at {self.pos}, {len(data)} read")
        self.pos += n
        return data

    def skip_to(self, pos: int) -> None:
        while self.pos < pos:
            self.read(min(pos - self.pos, 1 << 20))


def _entry(buf: bytes, at: int) -> tuple[_Entry, int]:
    """The DirectoryEntryDV at buf[at:] and its length in bytes."""
    if buf[at:at + 2] != b"DV":
        raise ValueError(f"CZI directory entry has schema {buf[at:at + 2]!r}, not b'DV'")
    pixel, _position, _part, compression, pyramid = struct.unpack_from("<iqiiB", buf, at + 2)
    count, = struct.unpack_from("<i", buf, at + 28)
    dims = {}
    for k in range(count):
        name, start, size, _coordinate, stored = struct.unpack_from("<4siifi", buf, at + 32 + 20 * k)
        dims[name.rstrip(b"\0").decode("ascii")] = (start, size, stored)
    return _Entry(pixel, compression, pyramid, dims), 32 + 20 * count


def _plane_key(e: _Entry) -> tuple[int, int]:
    return e.dims.get("C", (0, 1, 1))[0], e.dims.get("Z", (0, 1, 1))[0]


def _layout(entries: list[_Entry]) -> _Layout:
    """The czyx grid the pyramid-level-0 subblocks fill, or a ValueError naming what this snippet does not read."""
    planes = [e for e in entries if e.pyramid == 0]
    if not planes:
        raise ValueError("CZI has no full-resolution subblocks")
    for what, values in (("pixel types", {e.pixel for e in planes}), ("compressions", {e.compression for e in planes})):
        if len(values) != 1:
            raise ValueError(f"CZI subblocks have several {what}: {sorted(values)}")
    first = planes[0]
    if first.compression != UNCOMPRESSED:
        raise ValueError(f"CZI subblocks have compression {first.compression}; this snippet reads uncompressed (0) only")
    if first.pixel not in PIXEL:
        raise ValueError(f"CZI pixel type {first.pixel} is not one of the grey types {sorted(PIXEL)} this snippet reads")
    if not {"X", "Y"} <= set(first.dims):
        raise ValueError(f"CZI subblock has dimensions {sorted(first.dims)}, without X and Y")
    for e in planes:
        for d, (start, size, stored) in e.dims.items():
            if d in "XY" and (start, size) != first.dims[d][:2]:
                raise ValueError(f"CZI subblocks differ in {d} (start {start} size {size} and start {first.dims[d][0]} "
                                 f"size {first.dims[d][1]}): mosaic tiles are not read")
            if d not in "XY" and size != 1:
                raise ValueError(f"CZI subblock spans {size} along {d}; one 2D plane per subblock is read")
            if d not in "XYCZ" and start != first.dims.get(d, (None,))[0]:
                raise ValueError(f"CZI has several indices along {d} (scenes, timepoints, tiles, ...); "
                                 "one stack per file is read")
            if size != stored:
                raise ValueError(f"CZI subblock stores {stored} for {size} along {d} at pyramid level 0")
    keys = [_plane_key(e) for e in planes]
    cs, zs = sorted({c for c, _ in keys}), sorted({z for _, z in keys})
    if len(set(keys)) != len(keys) or len(keys) != len(cs) * len(zs):
        raise ValueError(f"CZI has {len(keys)} planes for {len(cs)} channels x {len(zs)} z: not one plane per (c, z)")
    return _Layout(np.dtype(PIXEL[first.pixel]), cs, zs, first.dims["Y"][1], first.dims["X"][1])


def _voxel_size(xml: bytes | None) -> dict[str, float] | None:
    """Metadata/Scaling/Items/Distance (metres per pixel) as nm; None when the file doesn't say."""
    if not xml:
        return None
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    out = {}
    for d in root.iterfind("./Metadata/Scaling/Items/Distance"):
        axis, value = (d.get("Id") or "").lower(), d.findtext("Value")
        try:
            if axis in ("z", "y", "x") and value:
                out[axis] = float(value) * 1e9
        except ValueError:
            pass
    return out or None


class _Czi:
    def __init__(self, io: IO, url: str, member: str | None, channel: int | None):
        self._io, self._url, self._member = io, url, member
        self._planes: dict[tuple[int, int], np.ndarray] | None = None
        entries, xml, _ = self._scan(load=False, want=None)
        if entries is None:     # the directory or the metadata lies behind the pixel data: one pass keeps it all
            entries, xml, self._planes = self._scan(load=True, want=None)
        self._layout = lay = _layout(entries)
        if channel is not None and not 0 <= channel < len(lay.cs):
            raise ValueError(f"params.channel {channel} out of range for {len(lay.cs)} channels")
        self._cs = lay.cs if channel is None else [lay.cs[channel]]
        self._collapse = channel is not None
        if self._planes is not None:
            self._planes = self._checked(self._planes)
        self.axes = "zyx" if self._collapse else "czyx"
        self.shape = (len(lay.zs), lay.y, lay.x) if self._collapse else (len(lay.cs), len(lay.zs), lay.y, lay.x)
        self.dtype = lay.dtype.newbyteorder("=")
        self.voxel_size_nm = _voxel_size(xml)
        self.access = "file"    # any read costs the whole member: the stream has no index to seek by

    def _scan(self, load: bool, want: set[int] | None) -> tuple[list[_Entry] | None, bytes | None, dict | None]:
        """Walk the member's segments in file order: (directory entries, metadata XML, planes by (C, Z) start).
        Without `load` it stops at the first subblock and returns entries None unless the directory, and the
        metadata when the file has one, came before it. With `load` it reads to the member's end and keeps the
        level-0 planes whose C start is in `want` (all when None); entries are then the subblocks' own."""
        try:
            raw = self._io.open(self._url)
        except NeedsFetch:      # a server without Range or Content-Length: the archive has to be staged whole
            raw = open(self._io.fetch(self._url), "rb")
        with raw, tarfile.open(fileobj=raw, mode="r|gz", bufsize=LOAD_READ if load else HEAD_READ) as tar:
            for info in tar:
                if info.isfile() and (info.name == self._member if self._member is not None
                                      else info.name.lower().endswith(".czi")):
                    break
            else:
                raise ValueError(f"{self._url} has no member {self._member!r}" if self._member is not None
                                 else f"{self._url} has no .czi member")
            f, size = _Sequential(tar.extractfile(info)), info.size
            directory, own, xml, planes, has_xml, at = None, [], None, {}, True, 0
            while at + 32 <= size:
                f.skip_to(at)
                sid, allocated, _used = struct.unpack("<16sqq", f.read(32))
                sid = sid.rstrip(b"\0")
                if (at == 0) != (sid == b"ZISRAWFILE") or allocated < 0:
                    raise ValueError(f"{info.name} is not a CZI file: segment {sid!r} ({allocated} bytes) at {at}")
                if sid == b"ZISRAWFILE":
                    has_xml = struct.unpack_from("<q", f.read(68), 60)[0] > 0     # MetadataPosition
                elif sid == b"ZISRAWDIRECTORY":
                    body = f.read(allocated)
                    directory, offset = [], 128
                    for _ in range(struct.unpack_from("<i", body)[0]):
                        e, length = _entry(body, offset)
                        directory.append(e)
                        offset += length
                elif sid == b"ZISRAWMETADATA":
                    xml = f.read(256 + struct.unpack("<i", f.read(4))[0] - 4)[252:]
                elif sid == b"ZISRAWSUBBLOCK":
                    if not load:
                        return (directory if xml is not None or not has_xml else None), xml, None
                    head = f.read(256)
                    metadata_size, _attachment_size, data_size = struct.unpack_from("<iiq", head)
                    count, = struct.unpack_from("<i", head, 16 + 28)
                    head += f.read(max(0, 16 + 32 + 20 * count - 256))
                    e, length = _entry(head, 16)
                    own.append(e)
                    if e.pyramid == 0 and e.compression == UNCOMPRESSED and e.pixel in PIXEL and \
                            {"X", "Y"} <= set(e.dims) and (want is None or _plane_key(e)[0] in want):
                        shape = (e.dims["Y"][2], e.dims["X"][2])
                        dtype = np.dtype(PIXEL[e.pixel])
                        if data_size != shape[0] * shape[1] * dtype.itemsize:
                            raise ValueError(f"CZI subblock at {at} holds {data_size} bytes for a {shape} {dtype} plane")
                        f.skip_to(at + 32 + max(256, 16 + length) + metadata_size)
                        planes[_plane_key(e)] = np.frombuffer(f.read(data_size), dtype).reshape(shape)
                at += 32 + allocated
            return (own if load else directory), xml, (planes if load else None)

    def _checked(self, planes: dict[tuple[int, int], np.ndarray]) -> dict[tuple[int, int], np.ndarray]:
        """The planes of the channels this array exposes; a ValueError when the stream did not hold them all."""
        lay = self._layout
        keep = {(c, z): planes.get((c, z)) for c in self._cs for z in lay.zs}
        missing = [k for k, p in keep.items() if p is None or p.shape != (lay.y, lay.x) or p.dtype != lay.dtype]
        if missing:
            raise ValueError(f"CZI subblocks for {len(missing)} of {len(keep)} planes are missing or differ from "
                             f"the directory, e.g. (C, Z) = {missing[0]}")
        return keep

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._planes is None:
            self._planes = self._checked(self._scan(load=True, want=set(self._cs))[2])
        full: dict[str, int | slice] = {a: region.get(a, slice(None)) for a in "czyx"}
        if self._collapse:
            full["c"] = 0
        cs = np.asarray(self._cs)[full["c"]]
        zs = np.asarray(self._layout.zs)[full["z"]]
        out = np.stack([self._planes[int(c), int(z)][full["y"], full["x"]] for c in np.ravel(cs) for z in np.ravel(zs)])
        return out.reshape(np.shape(cs) + np.shape(zs) + out.shape[1:]).astype(self.dtype, copy=False)


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"czi-targz-member reads one member of one archive, got {len(src.urls)} urls")
    member, channel = src.params.get("member"), src.params.get("channel")
    if member is not None and not isinstance(member, str):
        raise ValueError(f"params.member must be the member's name in the archive, got {member!r}")
    if channel is not None and (isinstance(channel, bool) or not isinstance(channel, int)):
        raise ValueError(f"params.channel must be an integer channel index, got {channel!r}")
    return _Czi(io, src.urls[0], member, channel)
