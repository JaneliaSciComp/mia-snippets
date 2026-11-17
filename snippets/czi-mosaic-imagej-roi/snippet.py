"""A tiled Zeiss CZI (ZISRAW) mosaic at a plain URL, stitched as cyx by placing each full-resolution tile at the
pixel position the file gives it; or, given that CZI and a set of ImageJ .roi files, the area ROIs filled onto the
mosaic's pixel grid as instance ids (yx).

No CZI or ImageJ ROI library is on the harness allowlist, so both formats are parsed here. CZI: file header ->
subblock directory -> the tiles a read touches, each fetched by byte range and decoded (uncompressed, zstd0 or
zstd1). ROI: the 64-byte big-endian header and the coordinate block that follows it. See README.md."""
import struct
from typing import BinaryIO
from xml.etree import ElementTree

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SEGMENT = 32                    # every ZISRAW segment starts with: 16-byte id, int64 allocated, int64 used
PIXEL_TYPES = {0: "u1", 1: "<u2", 2: "<f4"}                     # Gray8, Gray16, Gray32Float
PIXEL_NAMES = {0: "Gray8", 1: "Gray16", 2: "Gray32Float", 3: "Bgr24", 4: "Bgr48", 8: "Bgr96Float", 9: "Bgra32",
               10: "Gray64ComplexFloat", 11: "Bgr192ComplexFloat", 12: "Gray32", 13: "Gray64"}
COMPRESSIONS = {0: "uncompressed", 1: "JPEG", 2: "LZW", 4: "JPEG XR", 5: "zstd0", 6: "zstd1"}
TILE_CACHE = 16                 # decoded tiles kept (16 x 2048 x 2048 uint8 = 64 MiB)

ROI_TYPES = {0: "polygon", 1: "rectangle", 2: "oval", 3: "line", 4: "freeline", 5: "polyline", 6: "noRoi",
             7: "freehand", 8: "traced", 9: "angle", 10: "point"}
ROI_AREA, ROI_LINE = (0, 1, 2, 7, 8), (3, 4, 5, 9, 10)
SUB_PIXEL = 128                 # header option bit: float coordinates follow the int16 ones


def _segment(fh: BinaryIO, pos: int, want: bytes, n: int) -> bytes:
    """The first `n` data bytes of the segment at `pos`, whose id must be `want`."""
    fh.seek(pos)
    head = fh.read(SEGMENT + n)
    sid = head[:16].rstrip(b"\0")
    if sid != want or len(head) < SEGMENT + n:
        raise ValueError(f"CZI: expected segment {want.decode()} at byte {pos}, found {sid!r} "
                         f"({len(head)} of {SEGMENT + n} bytes)")
    return head[SEGMENT:]


def _entry(buf: bytes, p: int) -> tuple[dict, int]:
    """One DirectoryEntryDV at buf[p:]: (entry, its length in bytes)."""
    if buf[p:p + 2] != b"DV":
        raise ValueError(f"CZI: subblock directory entry has schema {buf[p:p + 2]!r}, only 'DV' is read")
    pixel_type, position, part, compression = struct.unpack_from("<iqii", buf, p + 2)
    (count,) = struct.unpack_from("<i", buf, p + 28)
    dims = {}
    for j in range(count):          # DimensionEntryDV1: char[4] name, int32 start, size, float32, int32 stored
        name, start, size, _, stored = struct.unpack_from("<4siifi", buf, p + 32 + 20 * j)
        dims[name.rstrip(b"\0").decode("ascii")] = (start, size, stored)
    return {"pixel_type": pixel_type, "position": position, "part": part, "compression": compression,
            "dims": dims}, 32 + 20 * count


def _span(key: int | slice, n: int) -> tuple[int, int, int | np.ndarray]:
    """What `key` asks of an axis of length n: (lo, hi, index into [lo, hi)); an int index drops the axis."""
    if isinstance(key, slice):
        idx = np.arange(n)[key]
        if idx.size == 0:
            return 0, 0, idx
        lo = int(idx.min())
        return lo, int(idx.max()) + 1, idx - lo
    k = int(key) + (n if key < 0 else 0)
    if not 0 <= k < n:
        raise IndexError(f"index {key} is outside an axis of length {n}")
    return k, k + 1, 0


class _Czi:
    """The full-resolution tiles of one CZI scene on one canvas: geometry, dtype and scaling, read at open."""

    def __init__(self, fh: BinaryIO, ranged: bool):
        self._fh = fh
        self.access = "chunk" if ranged else "file"
        head = _segment(fh, 0, b"ZISRAWFILE", 80)
        major, _ = struct.unpack_from("<ii", head, 0)
        part, directory, metadata = struct.unpack_from("<iqq", head, 48)
        if major != 1 or part != 0 or directory <= 0:
            raise ValueError(f"CZI: file header has major version {major}, file part {part}, directory at "
                             f"{directory}; only a single-part version 1 file with a subblock directory is read")
        (count,) = struct.unpack_from("<i", _segment(fh, directory, b"ZISRAWDIRECTORY", 128), 0)
        fh.seek(directory + SEGMENT - 8)
        (used,) = struct.unpack("<q", fh.read(8))
        fh.seek(directory + SEGMENT + 128)
        buf = fh.read(used - 128)
        entries, p = [], 0
        for _ in range(count):
            e, n = _entry(buf, p)
            entries.append(e)
            p += n
        if any("X" not in e["dims"] or "Y" not in e["dims"] for e in entries):
            raise ValueError("CZI: a subblock has no X or Y dimension")
        # a pyramid copy stores fewer pixels than the extent it covers; only full-resolution tiles are read
        full = [e for e in entries if all(e["dims"][a][2] == e["dims"][a][1] for a in "XY")]
        if not full:
            raise ValueError(f"CZI: no full-resolution subblock among {count}")
        for name in sorted({k for e in full for k in e["dims"]} - set("XYCM")):
            starts = {e["dims"].get(name, (0,))[0] for e in full}
            if len(starts) > 1:
                raise ValueError(f"CZI: the tiles span {len(starts)} positions of dimension {name}; this snippet "
                                 "reads one scene, one z plane and one time point (dimensions X, Y, C and M only)")
        kinds = {(e["pixel_type"], e["part"]) for e in full}
        if len(kinds) != 1 or full[0]["pixel_type"] not in PIXEL_TYPES or full[0]["part"] != 0:
            raise ValueError("CZI: tiles have pixel type / file part "
                             f"{sorted((PIXEL_NAMES.get(t, t), q) for t, q in kinds)}; only one of Gray8, Gray16 "
                             "and Gray32Float for all tiles, in this file, is read")
        bad = sorted({COMPRESSIONS.get(e["compression"], e["compression"]) for e in full
                      if e["compression"] not in (0, 5, 6)}, key=str)
        if bad:
            raise ValueError(f"CZI: tiles are compressed with {bad}; only uncompressed, zstd0 and zstd1 are read")
        self.dtype = np.dtype(PIXEL_TYPES[full[0]["pixel_type"]])
        self._y0 = min(e["dims"]["Y"][0] for e in full)
        self._x0 = min(e["dims"]["X"][0] for e in full)
        ny = max(e["dims"]["Y"][0] + e["dims"]["Y"][1] for e in full) - self._y0
        nx = max(e["dims"]["X"][0] + e["dims"]["X"][1] for e in full) - self._x0
        channels = sorted({e["dims"].get("C", (0,))[0] for e in full})
        # per channel, tiles in ascending M: where two overlap, the higher M index is what is read
        self._tiles = [sorted((e for e in full if e["dims"].get("C", (0,))[0] == c),
                              key=lambda e: e["dims"].get("M", (0,))[0]) for c in channels]
        self.axes, self.shape = "cyx", (len(channels), ny, nx)
        self._cache: dict[int, np.ndarray] = {}
        self.voxel_size_nm = _scaling(fh, metadata) if metadata > 0 else None

    def __del__(self):          # the harness never closes arrays; the handle goes with the array
        self._fh.close()

    def _tile(self, e: dict) -> np.ndarray:
        """The decoded pixels of one subblock, (Y size, X size)."""
        if e["position"] in self._cache:
            self._cache[e["position"]] = self._cache.pop(e["position"])         # most recently used last
            return self._cache[e["position"]]
        # SubBlock segment: int32 metadata size, int32 attachment size, int64 data size, its own copy of the
        # directory entry, filled to 256 bytes; then metadata, then the pixel data
        sub = _segment(self._fh, e["position"], b"ZISRAWSUBBLOCK", 256)
        meta_size, _, data_size = struct.unpack_from("<iiq", sub, 0)
        own, n = _entry(sub, 16)
        if (own["pixel_type"], own["compression"], own["dims"]) != (e["pixel_type"], e["compression"], e["dims"]):
            raise ValueError("CZI: a subblock's own directory entry differs from the file directory's")
        self._fh.seek(e["position"] + SEGMENT + max(256, 16 + n) + meta_size)
        data = self._fh.read(data_size)
        if len(data) != data_size:
            raise ValueError(f"CZI: subblock at byte {e['position']} read back short ({len(data)} of {data_size})")
        shape = (e["dims"]["Y"][1], e["dims"]["X"][1])
        if e["compression"] == 5:
            data = imagecodecs.zstd_decode(data)
        elif e["compression"] == 6:
            # zstd1: a header (its size in the first byte; then chunk type 1 with one flag byte, bit 0 = the
            # bytes of 16-bit pixels are stored as all low bytes, then all high bytes), then one zstd frame
            size = data[0]
            if size == 1:
                packed = False
            elif size == 3 and data[1] == 1:
                packed = bool(data[2] & 1)
            else:
                raise ValueError(f"CZI: zstd1 subblock header {data[:3].hex()} is not understood")
            data = imagecodecs.zstd_decode(data[size:])
            if packed and self.dtype.itemsize == 2:
                half = len(data) // 2
                data = np.stack([np.frombuffer(data, "u1", half), np.frombuffer(data, "u1", half, half)], 1).tobytes()
            elif packed and self.dtype.itemsize != 1:
                raise ValueError(f"CZI: zstd1 byte packing on {self.dtype.name} pixels is not read")
        if len(data) != shape[0] * shape[1] * self.dtype.itemsize:
            raise ValueError(f"CZI: subblock at byte {e['position']} holds {len(data)} bytes, not "
                             f"{shape[0]}x{shape[1]} {self.dtype.name} pixels")
        tile = np.frombuffer(data, self.dtype).reshape(shape)
        self._cache[e["position"]] = tile
        while len(self._cache) > TILE_CACHE:
            self._cache.pop(next(iter(self._cache)))
        return tile

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nc, ny, nx = self.shape
        c0, c1, ci = _span(region.get("c", slice(None)), nc)
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((c1 - c0, y1 - y0, x1 - x0), self.dtype)     # a pixel no tile covers stays 0
        chans = np.atleast_1d(ci) + c0 if out.size else []
        for c in sorted({int(v) for v in chans}):
            for e in self._tiles[c]:
                ty, tx = e["dims"]["Y"][0] - self._y0, e["dims"]["X"][0] - self._x0
                a0, a1 = max(y0, ty), min(y1, ty + e["dims"]["Y"][1])
                b0, b1 = max(x0, tx), min(x1, tx + e["dims"]["X"][1])
                if a0 < a1 and b0 < b1:
                    out[c - c0, a0 - y0:a1 - y0, b0 - x0:b1 - x0] = self._tile(e)[a0 - ty:a1 - ty, b0 - tx:b1 - tx]
        return np.array(out[ci][..., yi, :][..., xi])


def _scaling(fh: BinaryIO, pos: int) -> dict[str, float] | None:
    """Metadata/Scaling/Items/Distance[@Id=X|Y]/Value, which CZI gives in metres; None when it is not there."""
    (xml_size,) = struct.unpack_from("<i", _segment(fh, pos, b"ZISRAWMETADATA", 256), 0)
    fh.seek(pos + SEGMENT + 256)
    root = ElementTree.fromstring(fh.read(xml_size))
    out = {}
    for d in root.findall("./Metadata/Scaling/Items/Distance"):
        axis, value = (d.get("Id") or "").lower(), d.findtext("Value")
        if axis in ("y", "x") and value:
            metres = float(value)
            if metres > 0:
                out[axis] = metres * 1e9
    return out or None


def _roi_mask(b: bytes, name: str, skip_lines: bool) -> tuple[int, int, np.ndarray] | None:
    """One ImageJ .roi as (y, x, mask): the pixels whose centres lie inside its outline, with the image position
    of mask[0, 0]. None for a line ROI when `skip_lines`."""
    if len(b) < 64 or b[:4] != b"Iout":
        raise ValueError(f"{name}: not an ImageJ .roi (starts with {b[:4]!r}, {len(b)} bytes)")
    version, kind = struct.unpack_from(">hB", b, 4)
    top, left, bottom, right, n = struct.unpack_from(">hhhhH", b, 8)
    (composite,) = struct.unpack_from(">i", b, 36)
    subtype, options = struct.unpack_from(">hh", b, 48)
    (arc,) = struct.unpack_from(">h", b, 54)
    what = ROI_TYPES.get(kind, kind)
    if kind in ROI_LINE:
        if skip_lines:
            return None
        raise ValueError(f"{name}: a {what} ROI encloses no pixels; params.skip_lines: true leaves such ROIs out")
    if kind not in ROI_AREA or (kind == 1 and (composite or arc)) or subtype not in (0, 3):
        raise ValueError(f"{name}: ROI type {what} (subtype {subtype}, composite {composite}, arc {arc}) is not "
                         "read; only polygon, freehand, traced, plain rectangle and oval outlines are")
    box = (float(left), float(top), float(right), float(bottom))
    if kind in (1, 2) and options & SUB_PIXEL and version >= 223:
        x, y, w, h = struct.unpack_from(">ffff", b, 18)
        box = (x, y, x + w, y + h)
    if kind in (1, 2):
        xs, ys = np.array(box[0::2]), np.array(box[1::2])
    elif options & SUB_PIXEL and version >= 222:                # n int16 x, n int16 y, then n float x, n float y
        xy = np.frombuffer(b, ">f4", 2 * n, 64 + 4 * n).astype("f8")
        xs, ys = xy[:n], xy[n:]
    else:                                                       # int16 offsets from the bounding box corner
        xy = np.frombuffer(b, ">i2", 2 * n, 64).astype("f8")
        xs, ys = xy[:n] + left, xy[n:] + top
    if n < 3 and kind not in (1, 2):
        raise ValueError(f"{name}: a {what} ROI with {n} points has no interior")
    x0, y0 = int(np.floor(xs.min())), int(np.floor(ys.min()))
    py, px = np.mgrid[y0:int(np.ceil(ys.max())), x0:int(np.ceil(xs.max()))] + 0.5      # pixel centres
    if kind == 1:
        mask = np.ones(py.shape, bool)
    elif kind == 2:
        cx, cy, rx, ry = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
        mask = ((px - cx) / rx) ** 2 + ((py - cy) / ry) ** 2 <= 1
    else:                                                       # even-odd rule over the closed polygon
        mask = np.zeros(py.shape, bool)
        for xa, ya, xb, yb in zip(xs, ys, np.roll(xs, -1), np.roll(ys, -1)):
            if ya != yb:
                mask ^= ((ya > py) != (yb > py)) & (px < xa + (xb - xa) * (py - ya) / (yb - ya))
    return y0, x0, mask


class _Rois:
    """ImageJ area ROIs on a CZI mosaic's grid: pixel (y, x) holds the 1-based position, in the sorted list of
    the ROI files, of the ROI whose outline encloses its centre; 0 where none does."""

    def __init__(self, io: IO, urls: list[str], grid: _Czi, skip_lines: bool):
        if len(urls) > 65535:
            raise ValueError(f"{len(urls)} ROI files do not fit uint16 ids")
        self._io, self._urls, self._grid, self._skip = io, urls, grid, skip_lines
        self._masks: list[tuple[int, int, int, np.ndarray]] | None = None
        self.axes, self.shape, self.dtype = "yx", grid.shape[1:], np.dtype("uint16")
        self.voxel_size_nm = grid.voxel_size_nm
        self.access = "plane"           # every ROI file is read on the first read, whatever the region

    def _load(self) -> list[tuple[int, int, int, np.ndarray]]:
        masks = []
        for i, url in enumerate(self._urls, 1):
            try:
                with self._io.open(url) as fh:
                    b = fh.read()
            except NeedsFetch:
                b = self._io.fetch(url).read_bytes()
            m = _roi_mask(b, url.rsplit("/", 1)[-1], self._skip)
            if m is not None:
                masks.append((i, *m))
        if not masks:
            raise ValueError(f"none of the {len(self._urls)} ROI files is an area ROI")
        return masks

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._masks is None:
            self._masks = self._load()
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for i, my, mx, mask in self._masks:         # in file order: where two ROIs overlap, the later one is kept
            a0, a1 = max(y0, my), min(y1, my + mask.shape[0])
            b0, b1 = max(x0, mx), min(x1, mx + mask.shape[1])
            if a0 < a1 and b0 < b1:
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][mask[a0 - my:a1 - my, b0 - mx:b1 - mx]] = i
        return np.array(out[yi][..., xi])


def _open(io: IO, url: str) -> tuple[BinaryIO, bool]:
    """(file, read by range?)"""
    try:
        return io.open(url), True
    except NeedsFetch:          # a server without Range: stage the whole file
        return open(io.fetch(url), "rb"), False


def open_array(src: Source, io: IO) -> Array:
    skip_lines = src.params.get("skip_lines", False)
    if not isinstance(skip_lines, bool):
        raise ValueError(f"params.skip_lines must be true or false, got {skip_lines!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    rois = sorted(u for u in urls if u.split("?", 1)[0].split("#", 1)[0].lower().endswith(".roi"))
    rest = [u for u in urls if u not in rois]
    if len(rest) != 1:
        raise ValueError(f"urls must name exactly one CZI besides any .roi files, got {len(rest)} such urls")
    fh, ranged = _open(io, rest[0])
    magic = fh.read(4)
    if magic != b"ZISR":
        raise ValueError(f"{rest[0]} starts with {magic!r}: not a CZI (ZISRAW)")
    czi = _Czi(fh, ranged)
    if not rois:
        if "skip_lines" in src.params:
            raise ValueError("params.skip_lines is for the label form (urls with .roi files); got one CZI only")
        return czi
    return _Rois(io, rois, czi, skip_lines)
