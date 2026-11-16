"""One object of an IMOD binary model (.mod) rasterized, plane by plane, onto the model's own image grid."""
import struct
from typing import BinaryIO

import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

HEADER = 240                      # "IMODV1.2" + the 232-byte model structure; everything is big-endian
OBJ_OPEN, OBJ_SCATTERED = 1 << 3, 1 << 9          # OBJT flags
CONT_OPEN = 1 << 3                                # CONT flag: an open contour inside a closed object
MODEL_FLIPYZ = 1 << 16                            # model flag: y and z were swapped when the model was made
DRAW = ("fill", "outline")


class _ImodMask:
    axes: str = "zyx"
    access: str = "file"          # the first read parses the whole model (a few MB); nothing smaller is addressable
    dtype: np.dtype = np.dtype("uint8")
    voxel_size_nm: dict[str, float] | None = None   # pixsize/zscale in a model are display settings, see README

    def __init__(self, fh: BinaryIO, obj: int, draw: str) -> None:
        self._fh, self._obj, self._draw = fh, obj, draw
        h = fh.read(HEADER)
        if len(h) < HEADER or h[:4] != b"IMOD" or h[4:8] != b"V1.2":
            raise ValueError(f"not an IMOD V1.2 binary model: header={h[:8]!r}")
        nx, ny, nz, nobj, flags = struct.unpack_from(">4iI", h, 136)
        if min(nx, ny, nz) < 1:
            raise ValueError(f"bad IMOD model header: image dims x, y, z = {nx, ny, nz}")
        if flags & MODEL_FLIPYZ:
            raise ValueError("model has the flipped-y/z flag set; its contours are not on xy planes")
        if not 1 <= obj <= nobj:
            raise ValueError(f"object {obj} is not in this model (it has objects 1..{nobj})")
        self.shape: tuple[int, ...] = (nz, ny, nx)
        self.object_name: str | None = None                       # set by the first read
        self._planes: dict[int, list[tuple[np.ndarray, bool]]] | None = None   # z -> [(points x/row, closed)]

    def _parse(self) -> dict[int, list[tuple[np.ndarray, bool]]]:
        """Walk the chunks after the header and keep the selected object's contours, in array coordinates."""
        nz, ny, nx = self.shape
        self._fh.seek(0)
        b = self._fh.read()
        planes: dict[int, list[tuple[np.ndarray, bool]]] = {}
        p, n_obj, mine, closed_obj = HEADER, 0, False, True
        while p + 4 <= len(b):
            cid = b[p:p + 4]
            p += 4
            if cid == b"IEOF":
                break
            if cid == b"OBJT":                                    # name[64] extra[64] contsize flags ... (176 bytes)
                n_obj += 1
                mine = n_obj == self._obj
                if mine:
                    self.object_name = b[p:p + 64].split(b"\0")[0].decode("latin-1")
                    (oflags,) = struct.unpack_from(">I", b, p + 132)
                    if oflags & OBJ_SCATTERED:
                        raise ValueError(f"object {self._obj} {self.object_name!r} is scattered points, not contours")
                    closed_obj = not oflags & OBJ_OPEN
                    if not closed_obj and self._draw == "fill":
                        raise ValueError(f"object {self._obj} {self.object_name!r} has open contours, which have "
                                         f"no inside to fill; use draw: outline")
                p += 176
            elif cid == b"CONT":                                  # psize flags time surf, then psize * (x, y, z)
                psize, cflags = struct.unpack_from(">iI", b, p)
                p += 16
                if psize < 0 or p + 12 * psize > len(b):
                    raise ValueError(f"truncated IMOD model: contour of {psize} points at byte {p - 20}")
                if mine and psize:
                    pts = np.frombuffer(b, ">f4", 3 * psize, p).reshape(psize, 3).astype(np.float64)
                    z = np.floor(pts[:, 2] + 0.5).astype(np.int64)            # IMOD's own rounding to a section
                    if z.min() != z.max():
                        raise ValueError(f"object {self._obj} {self.object_name!r} has a contour spanning sections "
                                         f"{z.min()}..{z.max()}; only planar contours are rasterized")
                    closed = closed_obj and not cflags & CONT_OPEN
                    if not closed and self._draw == "fill":
                        raise ValueError(f"object {self._obj} {self.object_name!r} has an open contour on section "
                                         f"{z[0]}, which has no inside to fill; use draw: outline")
                    if 0 <= z[0] < nz:
                        # IMOD's y runs up from the bottom image row; array rows run down from the top
                        xy = np.column_stack([pts[:, 0], ny - pts[:, 1]])
                        planes.setdefault(int(z[0]), []).append((xy, closed))
                p += 12 * psize
            elif cid == b"MESH":                                  # vsize lsize flag time surf, vertices, index list
                vsize, lsize = struct.unpack_from(">2i", b, p)
                p += 16 + 12 * vsize + 4 * lsize
            else:                                                 # every other chunk: id, byte count, data
                (size,) = struct.unpack_from(">i", b, p)
                if size < 0:
                    raise ValueError(f"bad IMOD chunk {cid!r} at byte {p - 4}: size {size}")
                p += 4 + size
        if n_obj < self._obj:
            raise ValueError(f"object {self._obj} not found: the file holds {n_obj} objects")
        return planes

    def _fill(self, contours: list[tuple[np.ndarray, bool]], lo: int, hi: int) -> np.ndarray:
        """Even-odd fill of rows lo..hi: a pixel is 1 when its centre is inside an odd number of contours."""
        nx = self.shape[2]
        polys = [xy for xy, _ in contours if len(xy) >= 3]
        count = np.zeros((hi - lo, nx + 1), np.int32)
        if polys:
            a = np.concatenate(polys)
            b = np.concatenate([np.roll(xy, -1, axis=0) for xy in polys])
            keep = (np.minimum(a[:, 1], b[:, 1]) < hi) & (np.maximum(a[:, 1], b[:, 1]) > lo)
            a, b = a[keep], b[keep]
            yc = np.arange(lo, hi)[:, None] + 0.5                              # pixel-centre rows
            ri, ei = np.nonzero((a[:, 1] <= yc) != (b[:, 1] <= yc))            # edges crossing each row
            xc = a[ei, 0] + (yc[ri, 0] - a[ei, 1]) * (b[ei, 0] - a[ei, 0]) / (b[ei, 1] - a[ei, 1])
            first = np.clip(np.ceil(xc - 0.5), 0, nx).astype(np.int64)         # first pixel centre right of the edge
            np.add.at(count, (ri, first), 1)
        return (np.cumsum(count, axis=1)[:, :nx] & 1).astype(np.uint8)

    def _outline(self, contours: list[tuple[np.ndarray, bool]], lo: int, hi: int) -> np.ndarray:
        """Every pixel a contour line passes through (sampled every half pixel) in rows lo..hi."""
        nx = self.shape[2]
        out = np.zeros((hi - lo, nx), np.uint8)
        for xy, closed in contours:
            a = xy if closed or len(xy) == 1 else xy[:-1]
            d = (np.roll(xy, -1, axis=0) if closed or len(xy) == 1 else xy[1:]) - a
            n = np.ceil(np.abs(d).max(axis=1) * 2).astype(np.int64) + 1        # samples per segment, ends included
            seg = np.repeat(np.arange(len(a)), n)
            t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(np.maximum(n - 1, 1), n)
            pt = np.floor(a[seg] + d[seg] * t[:, None]).astype(np.int64)
            ok = (pt[:, 0] >= 0) & (pt[:, 0] < nx) & (pt[:, 1] >= lo) & (pt[:, 1] < hi)
            out[pt[ok, 1] - lo, pt[ok, 0]] = 1
        return out

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nz, ny, nx = self.shape
        z, y, x = (region.get(a, slice(None)) for a in "zyx")
        if not isinstance(z, int):
            planes = [self.read({"z": k, "y": y, "x": x}) for k in range(nz)[z]]
            return np.stack(planes) if planes else np.empty((0, ny, nx), self.dtype)[:, y, x]
        k, rows = range(nz)[z], range(ny)[y]
        if isinstance(rows, int):
            return self.read({"z": k, "y": slice(rows, rows + 1), "x": x})[0]
        if not rows:
            return np.empty((0, nx), self.dtype)[:, x]
        if self._planes is None:
            self._planes = self._parse()
        lo, hi = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
        contours = self._planes.get(k, [])
        block = (self._fill if self._draw == "fill" else self._outline)(contours, lo, hi)
        return block[np.asarray(rows) - lo][:, x]


class _Union:
    """The same class drawn in several models of one image (e.g. one model per cell): 1 where any model has it."""
    axes: str = "zyx"
    access: str = "file"
    dtype: np.dtype = np.dtype("uint8")
    voxel_size_nm: dict[str, float] | None = None

    def __init__(self, models: list[_ImodMask]) -> None:
        shapes = sorted({m.shape for m in models})
        if len(shapes) != 1:
            raise ValueError(f"the models are not on one image grid: zyx dims {shapes}")
        self._models, self.shape = models, shapes[0]

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        return np.maximum.reduce([m.read(region) for m in self._models])


def open_array(src: Source, io: IO) -> Array:
    obj, objs, draw = src.params.get("object"), src.params.get("objects"), src.params.get("draw", "fill")
    if (obj is None) == (objs is None):
        raise ValueError("set exactly one of the params object and objects")
    if objs is not None:
        if not isinstance(objs, str) or not all(o.strip().isdigit() for o in objs.split(",")):
            raise ValueError(f"param objects must be comma-separated 1-based object numbers, got {objs!r}")
        numbers = [int(o) for o in objs.split(",")]
        if len(numbers) != len(src.urls):
            raise ValueError(f"param objects names {len(numbers)} objects for {len(src.urls)} urls; one per url")
    elif isinstance(obj, bool) or not isinstance(obj, int):
        raise ValueError(f"param object must be a 1-based object number, got {obj!r}")
    else:
        numbers = [obj] * len(src.urls)
    if draw not in DRAW:
        raise ValueError(f"param draw must be one of {DRAW}, got {draw!r}")
    if not src.urls:
        raise ValueError("imod-model-mask needs at least one model url")
    models = [_ImodMask(io.open(url), n, draw) for url, n in zip(src.urls, numbers)]
    return models[0] if len(models) == 1 else _Union(models)
