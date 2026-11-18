"""One brightfield whole-slide image at a plain URL as cyx uint8 with c = red, green, blue: either a tiled colour
Zeiss CZI (ZISRAW) mosaic of Bgr24 JPEG XR tiles, stitched by placing each full-resolution tile at the pixel
position the file gives it, or the first (full-resolution) page of a tiled RGB TIFF such as an Aperio SVS.

Both are read tile by tile by byte range: a read fetches and decodes only the tiles its region touches. No CZI
library is on the harness allowlist, so the CZI is parsed here (file header -> subblock directory -> subblocks);
the TIFF goes through tifffile, one page only. The two are told apart by their first bytes. See README.md."""
import re
import struct
from typing import BinaryIO
from xml.etree import ElementTree

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SEGMENT = 32                    # every ZISRAW segment starts with: 16-byte id, int64 allocated, int64 used
BGR24, JPEGXR = 3, 4            # the one CZI pixel type and tile compression this snippet reads
PIXEL_NAMES = {0: "Gray8", 1: "Gray16", 2: "Gray32Float", 3: "Bgr24", 4: "Bgr48", 8: "Bgr96Float", 9: "Bgra32",
               10: "Gray64ComplexFloat", 11: "Bgr192ComplexFloat", 12: "Gray32", 13: "Gray64"}
COMPRESSIONS = {0: "uncompressed", 1: "JPEG", 2: "LZW", 4: "JPEG XR", 5: "zstd0", 6: "zstd1"}
CZI_CACHE = 8                   # decoded CZI tiles kept (8 x 2236 x 2236 x 3 uint8 = 120 MiB)
TIFF_CACHE = 512                # decoded TIFF tiles kept (512 x 240 x 240 x 3 uint8 = 88 MiB)
TIFF_CACHE_BYTES = 128 << 20    # and never more than this, whatever the tile size
MPP = re.compile(r"\|\s*MPP\s*=\s*([0-9.eE+-]+)")      # Aperio ImageDescription: "...|AppMag = 20|MPP = 0.5030|..."


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


class _Tiled:
    """A yx grid of RGB tiles presented as cyx uint8. A subclass sets shape, voxel_size_nm and access, and gives
    the tiles that intersect a box and the decoded pixels of one tile."""

    axes = "cyx"
    dtype = np.dtype("uint8")
    shape: tuple[int, int, int]
    voxel_size_nm: dict[str, float] | None
    access: str
    _fh: BinaryIO
    _cache: dict[int, np.ndarray]
    _keep: int

    def __del__(self):          # the harness never closes arrays; the handle goes with the array
        self._fh.close()

    def _touching(self, y0: int, y1: int, x0: int, x1: int) -> list[tuple[int, int, int]]:
        """(key, y, x) of every tile that intersects the box, in the order they are drawn."""
        raise NotImplementedError

    def _decode(self, key: int) -> np.ndarray:
        """Tile `key` as (rows, columns, 3) uint8, samples red, green, blue."""
        raise NotImplementedError

    def _tile(self, key: int) -> np.ndarray:
        if key in self._cache:
            self._cache[key] = self._cache.pop(key)         # most recently used last
            return self._cache[key]
        tile = self._decode(key)
        self._cache[key] = tile
        while len(self._cache) > self._keep:
            self._cache.pop(next(iter(self._cache)))
        return tile

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nc, ny, nx = self.shape
        c0, c1, ci = _span(region.get("c", slice(None)), nc)
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0, c1 - c0), self.dtype)     # a pixel no tile covers stays 0
        for key, ty, tx in (self._touching(y0, y1, x0, x1) if out.size else []):
            tile = self._tile(key)
            a0, a1 = max(y0, ty), min(y1, ty + tile.shape[0])
            b0, b1 = max(x0, tx), min(x1, tx + tile.shape[1])
            if a0 < a1 and b0 < b1:
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = tile[a0 - ty:a1 - ty, b0 - tx:b1 - tx, c0:c1]
        return np.array(np.moveaxis(out, 2, 0)[ci][..., yi, :][..., xi])


def _segment(fh: BinaryIO, pos: int, want: bytes, n: int) -> bytes:
    """The first `n` data bytes of the ZISRAW segment at `pos`, whose id must be `want`."""
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


class _Czi(_Tiled):
    """The full-resolution Bgr24 JPEG XR tiles of one CZI scene on one canvas."""

    def __init__(self, fh: BinaryIO, ranged: bool, scene: int | None):
        self._fh, self._cache, self._keep = fh, {}, CZI_CACHE
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
        scenes = sorted({e["dims"].get("S", (0,))[0] for e in full})
        if scene is not None:
            if scene not in scenes:
                raise ValueError(f"CZI: params.scene is {scene}, the file's scenes (S) are {scenes}")
            full = [e for e in full if e["dims"].get("S", (0,))[0] == scene]
        elif len(scenes) > 1:
            raise ValueError(f"CZI: the file holds scenes (S) {scenes}; params.scene names the one to read")
        for name in sorted({k for e in full for k in e["dims"]} - set("XYMS")):
            starts = {e["dims"].get(name, (0,))[0] for e in full}
            if len(starts) > 1:
                raise ValueError(f"CZI: the tiles span {len(starts)} positions of dimension {name}; this snippet "
                                 "reads one colour image: one channel of Bgr24 pixels, one z plane, one time point")
        kinds = sorted({(PIXEL_NAMES.get(e["pixel_type"], str(e["pixel_type"])),
                         COMPRESSIONS.get(e["compression"], str(e["compression"])), e["part"]) for e in full})
        if kinds != [("Bgr24", "JPEG XR", 0)]:
            raise ValueError(f"CZI: tiles have pixel type / compression / file part {kinds}; only Bgr24 tiles "
                             "compressed with JPEG XR, in this file, are read")
        y0 = min(e["dims"]["Y"][0] for e in full)
        x0 = min(e["dims"]["X"][0] for e in full)
        ny = max(e["dims"]["Y"][0] + e["dims"]["Y"][1] for e in full) - y0
        nx = max(e["dims"]["X"][0] + e["dims"]["X"][1] for e in full) - x0
        # tiles in ascending M: where two overlap, the higher M index is what is read
        full.sort(key=lambda e: e["dims"].get("M", (0,))[0])
        self._tiles = full
        self._boxes = np.array([[e["dims"]["Y"][0] - y0, e["dims"]["X"][0] - x0, e["dims"]["Y"][1],
                                 e["dims"]["X"][1]] for e in full], dtype=np.int64)
        self.shape = (3, ny, nx)
        self.voxel_size_nm = _scaling(fh, metadata) if metadata > 0 else None

    def _touching(self, y0: int, y1: int, x0: int, x1: int) -> list[tuple[int, int, int]]:
        ty, tx, th, tw = self._boxes.T
        hit = np.flatnonzero((ty < y1) & (ty + th > y0) & (tx < x1) & (tx + tw > x0))
        return [(int(i), int(ty[i]), int(tx[i])) for i in hit]

    def _decode(self, key: int) -> np.ndarray:
        e = self._tiles[key]
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
        # the codec converts the stream's 24bppBGR pixels to red, green, blue sample order
        tile = imagecodecs.jpegxr_decode(data)
        want = (e["dims"]["Y"][1], e["dims"]["X"][1], 3)
        if tile.shape != want or tile.dtype != self.dtype:
            raise ValueError(f"CZI: subblock at byte {e['position']} decodes to {tile.shape} {tile.dtype}, not "
                             f"{want} uint8")
        return tile


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


class _Tiff(_Tiled):
    """The first page of a TIFF, which must be tiled 8-bit RGB (or YCbCr JPEG) with interleaved samples."""

    def __init__(self, fh: BinaryIO, ranged: bool):
        self._fh, self._cache = fh, {}
        self.access = "chunk" if ranged else "file"
        self._tif = tifffile.TiffFile(fh)
        page = self._tif.pages[0]           # only this IFD is parsed; the pyramid, label and macro pages are not
        if not isinstance(page, tifffile.TiffPage):
            raise ValueError("TIFF: the first IFD is not an image page")
        if not page.is_tiled or page.samplesperpixel != 3 or page.planarconfig != 1 or page.dtype != self.dtype \
                or page.imagedepth != 1 or page.tiledepth != 1 or page.photometric.name not in ("RGB", "YCBCR"):
            raise ValueError(f"TIFF: the first page is {page.shape} {page.dtype}, photometric "
                             f"{page.photometric.name}, planar configuration {int(page.planarconfig)}, "
                             f"{'tiled' if page.is_tiled else 'in strips'}; only a tiled 8-bit RGB page with "
                             "interleaved samples is read")
        self._page = page
        self._th, self._tw = page.tilelength, page.tilewidth
        self._across = -(-page.imagewidth // self._tw)
        down = -(-page.imagelength // self._th)
        if len(page.dataoffsets) != self._across * down or len(page.databytecounts) != self._across * down:
            raise ValueError(f"TIFF: the first page lists {len(page.dataoffsets)} tiles, not {down}x{self._across}")
        self._keep = max(1, min(TIFF_CACHE, TIFF_CACHE_BYTES // (self._th * self._tw * 3)))
        self.shape = (3, page.imagelength, page.imagewidth)
        mpp = MPP.search(page.description or "")
        nm = float(mpp.group(1)) * 1000 if mpp else 0.0
        self.voxel_size_nm = {"y": nm, "x": nm} if nm > 0 else None

    def _touching(self, y0: int, y1: int, x0: int, x1: int) -> list[tuple[int, int, int]]:
        return [(r * self._across + c, r * self._th, c * self._tw)
                for r in range(y0 // self._th, -(-y1 // self._th)) for c in range(x0 // self._tw, -(-x1 // self._tw))]

    def _decode(self, key: int) -> np.ndarray:
        offset, size = self._page.dataoffsets[key], self._page.databytecounts[key]
        ty, tx = divmod(key, self._across)
        h = min(self._th, self.shape[1] - ty * self._th)        # the part of an edge tile inside the image
        w = min(self._tw, self.shape[2] - tx * self._tw)
        if size == 0:                       # a tile the file does not store
            return np.zeros((h, w, 3), self.dtype)
        self._fh.seek(offset)
        data = self._fh.read(size)
        if len(data) != size:
            raise ValueError(f"TIFF: tile {key} at byte {offset} read back short ({len(data)} of {size})")
        tile = self._page.decode(data, key, jpegtables=self._page.jpegtables)[0]
        if tile is None or tile.ndim != 4 or tile.shape[0] != 1 or tile.shape[1] < h or tile.shape[2] < w \
                or tile.shape[3] != 3 or tile.dtype != self.dtype:
            raise ValueError(f"TIFF: tile {key} decodes to {None if tile is None else (tile.shape, tile.dtype)}, "
                             f"not at least {h}x{w} RGB uint8 pixels")
        return tile[0, :h, :w]


def _open(io: IO, url: str) -> tuple[BinaryIO, bool]:
    """(file, read by range?)"""
    try:
        return io.open(url), True
    except NeedsFetch:          # a server without Range: stage the whole file
        return open(io.fetch(url), "rb"), False


def open_array(src: Source, io: IO) -> Array:
    scene = src.params.get("scene")
    if scene is not None and (isinstance(scene, bool) or not isinstance(scene, int)):
        raise ValueError(f"params.scene must be an integer, got {scene!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(urls) != 1:
        raise ValueError(f"urls must name exactly one file (one slide is one array), got {len(urls)}")
    fh, ranged = _open(io, urls[0])
    magic = fh.read(4)
    fh.seek(0)
    if magic == b"ZISR":
        return _Czi(fh, ranged, scene)
    if magic in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
        if scene is not None:
            raise ValueError("params.scene is for a CZI; this file is a TIFF")
        return _Tiff(fh, ranged)
    raise ValueError(f"{urls[0]} starts with {magic!r}: neither a CZI (ZISRAW) nor a TIFF")
