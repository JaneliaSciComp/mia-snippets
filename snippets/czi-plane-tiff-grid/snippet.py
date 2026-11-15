"""One 2D Zeiss CZI (ZISRAW) image at a plain URL, read as yx with its own parser; or, given that CZI and one
single-page integer TIFF, the TIFF placed on the CZI's pixel grid (`label_factor: k`).

No CZI library is on the harness allowlist, so the container is parsed here: file header -> subblock directory
-> the one full-resolution subblock, which must be uncompressed and greyscale. Its pixels are then a plain
little-endian block read by byte range, row band by row band (access `chunk`).

The second form exists for labels stored on a coarser grid than their image (a segmentation made on a k-times
reduced copy): raw pixel (y, x) takes the TIFF's pixel (y // k, x // k). Nothing is interpolated and no value is
made up; see README.md for the edge rule and why this is a view of the file, not a resampling."""
import struct
from typing import BinaryIO
from xml.etree import ElementTree

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SEGMENT = 32                    # every ZISRAW segment starts with: 16-byte id, int64 allocated, int64 used
PIXEL_TYPES = {0: "u1", 1: "<u2", 2: "<f4"}                     # Gray8, Gray16, Gray32Float
PIXEL_NAMES = {0: "Gray8", 1: "Gray16", 2: "Gray32Float", 3: "Bgr24", 4: "Bgr48", 8: "Bgr96Float", 9: "Bgra32",
               10: "Gray64ComplexFloat", 11: "Bgr192ComplexFloat", 12: "Gray32", 13: "Gray64"}
COMPRESSIONS = {0: "uncompressed", 1: "JPEG", 2: "LZW", 4: "JPEG XR", 5: "zstd0", 6: "zstd1"}


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


def _full_resolution(e: dict) -> bool:
    """False for a pyramid copy, which stores fewer pixels than the extent it covers."""
    return all(e["dims"][a][2] == e["dims"][a][1] for a in "XY")


class _Czi:
    """The one full-resolution subblock of a CZI: geometry, dtype, pixel offset and scaling, read at open."""

    def __init__(self, fh: BinaryIO, ranged: bool):
        self._fh = fh
        self.access = "chunk" if ranged else "file"
        self.axes = "yx"
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
        full = [e for e in entries if _full_resolution(e)]
        if len(full) != 1:
            raise ValueError(f"CZI: {len(full)} full-resolution subblocks (of {count}); this snippet reads a file "
                             "that holds one 2D image in one subblock, not tiles, channels, scenes, z or time")
        e = full[0]
        if e["compression"] != 0:
            raise ValueError(f"CZI: the subblock's compression is "
                             f"{COMPRESSIONS.get(e['compression'], e['compression'])}; only uncompressed is read")
        if e["pixel_type"] not in PIXEL_TYPES:
            raise ValueError(f"CZI: pixel type {PIXEL_NAMES.get(e['pixel_type'], e['pixel_type'])}; only "
                             "Gray8, Gray16 and Gray32Float are read")
        if e["part"] != 0:
            raise ValueError(f"CZI: the subblock is in file part {e['part']}, not in this file")
        self.dtype = np.dtype(PIXEL_TYPES[e["pixel_type"]])
        self.shape = (e["dims"]["Y"][1], e["dims"]["X"][1])
        # SubBlock segment: int32 metadata size, int32 attachment size, int64 data size, its own copy of the
        # directory entry, filled to 256 bytes; then metadata, then the pixels
        sub = _segment(fh, e["position"], b"ZISRAWSUBBLOCK", 256)
        meta_size, _, data_size = struct.unpack_from("<iiq", sub, 0)
        own, n = _entry(sub, 16)
        if (own["pixel_type"], own["compression"], own["dims"]) != (e["pixel_type"], e["compression"], e["dims"]):
            raise ValueError("CZI: the subblock's own directory entry differs from the file directory's")
        if data_size != self.shape[0] * self.shape[1] * self.dtype.itemsize:
            raise ValueError(f"CZI: subblock holds {data_size} bytes, not {self.shape[0]}x{self.shape[1]} "
                             f"{self.dtype.name} pixels")
        self._data = e["position"] + SEGMENT + max(256, 16 + n) + meta_size
        self.voxel_size_nm = _scaling(fh, metadata) if metadata > 0 else None

    def __del__(self):          # the harness never closes arrays; the handle goes with the array
        self._fh.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self.shape
        ys, xs = region.get("y", slice(None)), region.get("x", slice(None))
        rows = np.arange(ny)[ys]
        if rows.size == 0:
            return np.empty((0, nx), self.dtype)[:, xs]
        lo, hi = int(rows.min()), int(rows.max()) + 1          # one byte range: the rows asked for, full width
        self._fh.seek(self._data + lo * nx * self.dtype.itemsize)
        raw = self._fh.read((hi - lo) * nx * self.dtype.itemsize)
        if len(raw) != (hi - lo) * nx * self.dtype.itemsize:
            raise ValueError(f"CZI: rows {lo}..{hi} read back short ({len(raw)} bytes)")
        band = np.frombuffer(raw, self.dtype).reshape(hi - lo, nx)
        return np.array(band[rows - lo][..., xs])


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


class _OnGrid:
    """A yx TIFF whose pixel (i, j) covers raw pixels (i*k .. i*k+k-1, j*k .. j*k+k-1) of a CZI's grid."""

    def __init__(self, tif: tifffile.TiffFile, grid: _Czi, factor: int):
        self._tif = tif
        s = tif.series[0]
        if len(tif.series) != 1 or len(s.pages) != 1 or s.axes != "YX":
            raise ValueError(f"label TIFF has {len(tif.series)} series, the first with {len(s.pages)} pages and "
                             f"axes {s.axes!r} {tuple(s.shape)}; one single-page YX image is read")
        if np.dtype(s.dtype).kind not in "iub":
            raise ValueError(f"label TIFF dtype is {np.dtype(s.dtype).name}; pixels are repeated, never "
                             "interpolated, so only an integer label image is placed on a finer grid")
        want = tuple(n // factor for n in grid.shape)
        if tuple(s.shape) != want:
            raise ValueError(f"label TIFF is {tuple(s.shape)}; with label_factor {factor} the CZI's "
                             f"{grid.shape} needs {want} (each axis divided by the factor, rounded down)")
        self.axes, self.shape, self.dtype = "yx", grid.shape, np.dtype(s.dtype)
        self.voxel_size_nm = grid.voxel_size_nm     # the array is on the CZI's grid; the TIFF states no size
        self.access = "plane"                       # the TIFF page is read whole, once
        # raw index -> label index; the last 0..k-1 raw rows/columns past k*n take the last label row/column
        self._index = tuple(np.minimum(np.arange(n) // factor, m - 1) for n, m in zip(grid.shape, s.shape))
        self._page: np.ndarray | None = None

    def __del__(self):
        self._tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._page is None:
            self._page = self._tif.series[0].asarray()
        iy, ix = self._index
        return np.array(self._page[iy[region.get("y", slice(None))]][..., ix[region.get("x", slice(None))]])


def _open(io: IO, url: str) -> tuple[BinaryIO, bool]:
    """(file, read by range?)"""
    try:
        return io.open(url), True
    except NeedsFetch:          # a server without Range: stage the whole file
        return open(io.fetch(url), "rb"), False


def open_array(src: Source, io: IO) -> Array:
    factor = src.params.get("label_factor")
    files: dict[str, list[tuple[BinaryIO, bool]]] = {"czi": [], "tiff": []}
    for url in src.urls:
        fh, ranged = _open(io, url)
        magic = fh.read(4)
        if magic == b"ZISR":
            files["czi"].append((fh, ranged))
        elif magic in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
            files["tiff"].append((fh, ranged))
        else:
            raise ValueError(f"{url} starts with {magic!r}: neither a CZI (ZISRAW) nor a TIFF")
    if factor is None:
        if len(src.urls) != 1 or len(files["czi"]) != 1:
            raise ValueError(f"without params.label_factor this snippet reads one CZI url, got {len(src.urls)} "
                             f"urls ({len(files['czi'])} CZI, {len(files['tiff'])} TIFF)")
        return _Czi(*files["czi"][0])
    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 1:
        raise ValueError(f"params.label_factor must be a positive integer, got {factor!r}")
    if len(src.urls) != 2 or len(files["czi"]) != 1 or len(files["tiff"]) != 1:
        raise ValueError(f"with params.label_factor this snippet reads two urls, the label TIFF and the CZI whose "
                         f"grid it is placed on, got {len(files['czi'])} CZI and {len(files['tiff'])} TIFF")
    fh, _ = files["tiff"][0]
    fh.seek(0)
    return _OnGrid(tifffile.TiffFile(fh), _Czi(*files["czi"][0]), factor)
