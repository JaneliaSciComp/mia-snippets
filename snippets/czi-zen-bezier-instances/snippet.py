"""The closed outlines a Zeiss CZI carries in its own XML metadata (ZEN graphics, `Bezier` elements of
Metadata/Layers/Layer/Elements), filled as instance ids on the yx grid of one scene of that CZI's mosaic.

A label form only: the pixels of the same scene are another snippet's (`wsi-czi-svs-rgb` with the same `scene`),
and the grid here is that snippet's canvas, the bounding box of the scene's full-resolution tiles. Only the file
header, the subblock directory and the metadata XML are read, never a tile. No CZI library is on the harness
allowlist, so the ZISRAW segments are parsed here. See README.md."""
import math
import struct
from typing import BinaryIO
from xml.etree import ElementTree

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SEGMENT = 32                    # every ZISRAW segment starts with: 16-byte id, int64 allocated, int64 used
OUTLINE = "Bezier"              # the one element kind that is drawn
LEFT_OUT = ("Line", "ScaleBar")  # kinds that enclose nothing and are left out; any other kind is refused
PLANE_MAX = 1 << 29             # pixels of a scene up to which a whole-plane read is offered (1 GiB of uint16)


def _span(key: int | slice, n: int) -> tuple[int, int, int | np.ndarray | None]:
    """What `key` asks of an axis of length n: (lo, hi, index into [lo, hi)); the index is None when it is all
    of [lo, hi) in order, and an int index drops the axis."""
    if isinstance(key, slice):
        start, stop, step = key.indices(n)
        if step == 1:
            return start, max(start, stop), None
        idx = np.arange(n)[key]
        if idx.size == 0:
            return 0, 0, None
        lo = int(idx.min())
        return lo, int(idx.max()) + 1, idx - lo
    k = int(key) + (n if key < 0 else 0)
    if not 0 <= k < n:
        raise IndexError(f"index {key} is outside an axis of length {n}")
    return k, k + 1, 0


def _segment(fh: BinaryIO, pos: int, want: bytes, n: int) -> bytes:
    """The first `n` data bytes of the ZISRAW segment at `pos`, whose id must be `want`."""
    fh.seek(pos)
    head = fh.read(SEGMENT + n)
    sid = head[:16].rstrip(b"\0")
    if sid != want or len(head) < SEGMENT + n:
        raise ValueError(f"CZI: expected segment {want.decode()} at byte {pos}, found {sid!r} "
                         f"({len(head)} of {SEGMENT + n} bytes)")
    return head[SEGMENT:]


def _tiles(fh: BinaryIO, directory: int) -> list[dict[str, tuple[int, int, int]]]:
    """The dimensions of every full-resolution subblock: name -> (start, size, stored size)."""
    (count,) = struct.unpack_from("<i", _segment(fh, directory, b"ZISRAWDIRECTORY", 128), 0)
    fh.seek(directory + SEGMENT - 8)
    (used,) = struct.unpack("<q", fh.read(8))
    fh.seek(directory + SEGMENT + 128)
    buf = fh.read(used - 128)
    out, p = [], 0
    for _ in range(count):                  # DirectoryEntryDV: 32 bytes, then 20 per dimension
        if buf[p:p + 2] != b"DV":
            raise ValueError(f"CZI: subblock directory entry has schema {buf[p:p + 2]!r}, only 'DV' is read")
        (n,) = struct.unpack_from("<i", buf, p + 28)
        dims = {}
        for j in range(n):                  # DimensionEntryDV1: char[4] name, int32 start, size, float32, int32 stored
            name, start, size, _, stored = struct.unpack_from("<4siifi", buf, p + 32 + 20 * j)
            dims[name.rstrip(b"\0").decode("ascii")] = (start, size, stored)
        p += 32 + 20 * n
        if "X" not in dims or "Y" not in dims:
            raise ValueError("CZI: a subblock has no X or Y dimension")
        # a pyramid copy stores fewer pixels than the extent it covers; the grid is the full-resolution tiles'
        if all(dims[a][2] == dims[a][1] for a in "XY"):
            out.append(dims)
    if not out:
        raise ValueError(f"CZI: no full-resolution subblock among {count}")
    return out


def _box(tiles: list[dict[str, tuple[int, int, int]]]) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of the tiles' bounding box, in the directory's pixel coordinates."""
    return (min(t["X"][0] for t in tiles), min(t["Y"][0] for t in tiles),
            max(t["X"][0] + t["X"][1] for t in tiles), max(t["Y"][0] + t["Y"][1] for t in tiles))


def _outlines(root: ElementTree.Element) -> list[np.ndarray]:
    """The points of every Bezier element of every layer, in file order, as (n, 2) arrays of x, y."""
    out, other = [], set()
    for el in root.findall("./Metadata/Layers/Layer/Elements/*"):
        if el.tag != OUTLINE:
            if el.tag not in LEFT_OUT:
                other.add(el.tag)
            continue
        pairs = [p.split(",") for p in (el.findtext("Geometry/Points") or "").split()]
        try:
            if any(len(p) != 2 for p in pairs):
                raise ValueError("not a pair")
            pts = np.array([[float(v) for v in p] for p in pairs], dtype=np.float64).reshape(-1, 2)
            if not np.isfinite(pts).all():
                raise ValueError("not finite")
        except ValueError:
            raise ValueError(f"CZI: Bezier element {len(out) + 1} (Id {el.get('Id')!r}) has Geometry/Points that "
                             "are not 'x,y' pairs of numbers") from None
        out.append(pts)
    if other:
        raise ValueError(f"CZI: the annotation layers hold {sorted(other)} elements; this snippet draws Bezier "
                         f"outlines, leaves out {list(LEFT_OUT)} and reads no other kind")
    return out


def _scaling(root: ElementTree.Element) -> dict[str, float] | None:
    """Metadata/Scaling/Items/Distance[@Id=X|Y]/Value, which CZI gives in metres; None when it is not there."""
    out = {}
    for d in root.findall("./Metadata/Scaling/Items/Distance"):
        axis, value = (d.get("Id") or "").lower(), d.findtext("Value")
        if axis in ("y", "x") and value:
            metres = float(value)
            if metres > 0:
                out[axis] = metres * 1e9
    return out or None


def _fill(pts: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """The pixels of rows [y0, y1) and columns [x0, x1) whose centres the closed polygon `pts` encloses
    (even-odd rule), as a boolean (y1 - y0, x1 - x0) array. Pixel (row, column) has its centre at
    (x, y) = (column + 0.5, row + 0.5)."""
    mask = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    xc = np.arange(x0, x1) + 0.5
    ax, ay = pts[:, 0], pts[:, 1]
    bx, by = np.roll(ax, -1), np.roll(ay, -1)       # the last point joins the first
    for xa, ya, xb, yb in zip(ax.tolist(), ay.tolist(), bx.tolist(), by.tolist()):
        if ya == yb:
            continue
        # the rows whose centre line the edge crosses: min(ya, yb) <= row + 0.5 < max(ya, yb)
        r0 = max(math.ceil(min(ya, yb) - 0.5), y0)
        r1 = min(math.ceil(max(ya, yb) - 0.5), y1)
        if r0 >= r1:
            continue
        cross = xa + (np.arange(r0, r1) + 0.5 - ya) * ((xb - xa) / (yb - ya))
        mask[r0 - y0:r1 - y0] ^= xc[None, :] < cross[:, None]      # a pixel left of an odd number of crossings
    return mask


class _Outlines:
    """Closed outlines as instance ids on the yx grid of one CZI scene."""

    axes = "yx"

    def __init__(self, fh: BinaryIO, scene: int | None):
        head = _segment(fh, 0, b"ZISRAWFILE", 80)
        major, _ = struct.unpack_from("<ii", head, 0)
        part, directory, metadata = struct.unpack_from("<iqq", head, 48)
        if major != 1 or part != 0 or directory <= 0 or metadata <= 0:
            raise ValueError(f"CZI: file header has major version {major}, file part {part}, directory at "
                             f"{directory}, metadata at {metadata}; only a single-part version 1 file with a "
                             "subblock directory and a metadata segment is read")
        tiles = _tiles(fh, directory)
        by_scene: dict[int, list[dict[str, tuple[int, int, int]]]] = {}
        for t in tiles:
            by_scene.setdefault(t.get("S", (0,))[0], []).append(t)
        scenes = sorted(by_scene)
        if scene is None:
            if len(scenes) > 1:
                raise ValueError(f"CZI: the file holds scenes (S) {scenes}; params.scene names the one to read")
            scene = scenes[0]
        elif scene not in by_scene:
            raise ValueError(f"CZI: params.scene is {scene}, the file's scenes (S) are {scenes}")
        # graphics are in pixels from the corner of the bounding box of all scenes' full-resolution tiles; the
        # grid is this scene's own bounding box, so every point is moved by the scene's corner in that frame
        gx, gy, _, _ = _box(tiles)
        boxes = {s: _box(ts) for s, ts in by_scene.items()}
        sx0, sy0, sx1, sy1 = boxes[scene]
        self.shape = (sy1 - sy0, sx1 - sx0)

        (xml_size,) = struct.unpack_from("<i", _segment(fh, metadata, b"ZISRAWMETADATA", 256), 0)
        fh.seek(metadata + SEGMENT + 256)
        xml = fh.read(xml_size)
        if len(xml) != xml_size:
            raise ValueError(f"CZI: the metadata XML read back short ({len(xml)} of {xml_size} bytes)")
        root = ElementTree.fromstring(xml)
        self.voxel_size_nm = _scaling(root)
        outlines = _outlines(root)
        if not outlines:
            raise ValueError("CZI: the metadata XML holds no Bezier element (Metadata/Layers/Layer/Elements): the "
                             "file has no outline to draw")
        self.dtype = np.dtype("uint16" if len(outlines) <= 0xFFFF else "uint32")
        # (id, points in this scene's pixels, the rows and columns its bounding box covers), ids from 1 in file
        # order over all scenes; an outline of fewer than 3 points encloses nothing and keeps its id unused
        self._outlines: list[tuple[int, np.ndarray, tuple[int, int, int, int]]] = []
        on: dict[int, int] = {}
        for i, pts in enumerate(outlines, start=1):
            if len(pts) < 3:
                continue
            x, y = pts[:, 0] + gx, pts[:, 1] + gy            # in the directory's coordinates
            for s, (bx0, by0, bx1, by1) in boxes.items():
                if x.min() < bx1 and x.max() > bx0 and y.min() < by1 and y.max() > by0:
                    on[s] = on.get(s, 0) + 1
            here = np.stack([x - sx0, y - sy0], axis=1)
            rows = max(math.floor(here[:, 1].min()), 0), min(math.ceil(here[:, 1].max()), self.shape[0])
            cols = max(math.floor(here[:, 0].min()), 0), min(math.ceil(here[:, 0].max()), self.shape[1])
            if rows[0] < rows[1] and cols[0] < cols[1]:
                self._outlines.append((i, here, (rows[0], rows[1], cols[0], cols[1])))
        if not self._outlines:
            raise ValueError(f"CZI: none of the file's {len(outlines)} Bezier outlines lies on scene {scene}; "
                             f"outlines per scene: {dict(sorted(on.items()))}")
        # every read is computed from the outlines, at no cost in bytes. A whole plane is offered as the read
        # unit while it fits in memory, so a reader can look for the few annotated pixels of a slide
        self.access = "plane" if self.shape[0] * self.shape[1] <= PLANE_MAX else "chunk"

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)          # 0: no outline encloses this pixel
        for i, pts, (r0, r1, c0, c1) in (self._outlines if out.size else []):
            a0, a1, b0, b1 = max(r0, y0), min(r1, y1), max(c0, x0), min(c1, x1)
            if a0 < a1 and b0 < b1:                             # where outlines overlap, the later one is kept
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][_fill(pts, a0, a1, b0, b1)] = i
        if yi is not None:
            out = out[yi]
        if xi is not None:
            out = out[..., xi]
        return out


def open_array(src: Source, io: IO) -> Array:
    scene = src.params.get("scene")
    if scene is not None and (isinstance(scene, bool) or not isinstance(scene, int)):
        raise ValueError(f"params.scene must be an integer, got {scene!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(urls) != 1:
        raise ValueError(f"urls must name exactly one CZI file (one scene of one slide is one array), got "
                         f"{len(urls)}")
    try:
        fh = io.open(urls[0])
    except NeedsFetch:          # a server without Range: stage the whole file
        fh = open(io.fetch(urls[0]), "rb")
    with fh:                    # everything a read needs is parsed here; no pixel is ever read
        magic = fh.read(4)
        if magic != b"ZISR":
            raise ValueError(f"{urls[0]} starts with {magic!r}: not a CZI (ZISRAW) file")
        return _Outlines(fh, scene)
