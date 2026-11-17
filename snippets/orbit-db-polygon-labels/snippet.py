"""orbit-db-polygon-labels: the outlines Orbit Image Analysis keeps for one slide in its SQLite database
(`orbit.db`), filled onto that slide's full-resolution grid as yx uint16 instance ids.

urls are the database and the slide's TIFF, in any order. The TIFF gives the grid (only its header is read
when the file can be read by range) and its name finds the slide's row in RAW_DATA_FILE; RAW_ANNOTATION holds
one Java-serialized `PolygonExt` per outline. The blobs are walked with a small parser of the serialization
stream grammar that only ever builds dicts, strings and numpy arrays: no class named in a blob is looked up,
and anything outside the grammar's plain-object subset is an error.

A polygon's points are stored in the zoom it was drawn at (`scale`, in percent), so a full-resolution pixel
is `point * 100 / scale`. Only annotations of Orbit's subtype 0 (a plain annotation) are filled; ROI,
exclusion and inclusion areas (subtypes 1 to 3) mark regions, not objects, and are left out. An annotation's
id is its 1-based position among all of the slide's rows ordered by RAW_ANNOTATION_ID.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

_NUM = {"B": ">b", "C": ">H", "D": ">d", "F": ">f", "I": ">i", "J": ">q", "S": ">h", "Z": ">B"}   # field codes
_ROWS = 1 << 22         # row x edge cells of one fill step
_UNIT_NM = {2: 25.4e6, 3: 1e7}      # TIFF ResolutionUnit: inch, centimetre


class _Stream:
    """A Java Object Serialization stream read as data. Objects come back as {"class": name, field: value},
    primitive arrays as numpy arrays, block data as bytes."""

    def __init__(self, b: bytes):
        if b[:4] != b"\xac\xed\x00\x05":
            raise ValueError("not a Java serialization stream (magic aced0005)")
        self.b, self.p, self.handles = b, 4, []

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.b):
            raise ValueError("serialization stream is cut short")
        self.p += n
        return self.b[self.p - n:self.p]

    def num(self, fmt: str) -> Any:
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def utf(self) -> str:
        return self.take(self.num(">H")).decode("utf-8", "replace")

    def ref(self) -> Any:
        k = self.num(">i") - 0x7E0000
        if not 0 <= k < len(self.handles):
            raise ValueError("serialization stream refers to an object it has not written")
        return self.handles[k]

    def contents(self) -> list:
        out = []
        while self.p < len(self.b):
            out.append(self.content())
        return out

    def content(self) -> Any:
        t = self.b[self.p]
        if t in (0x77, 0x7A):                       # block data, short and long
            self.p += 1
            return self.take(self.num("B" if t == 0x77 else ">i"))
        return self.value()

    def value(self) -> Any:
        t = self.num("B")
        if t == 0x70:                               # null
            return None
        if t == 0x71:                               # reference to an earlier value
            return self.ref()
        if t == 0x74:                               # string
            self.handles.append(self.utf())
            return self.handles[-1]
        if t == 0x75:                               # array
            desc = self.classdesc()
            at = len(self.handles)
            self.handles.append(None)
            n, kind = self.num(">i"), (desc or {"name": ""})["name"][1:2]
            if kind in _NUM:
                dt = np.dtype(_NUM[kind])
                arr = np.frombuffer(self.take(n * dt.itemsize if n >= 0 else -1), dt)
            else:
                arr = [self.value() for _ in range(n)]
            self.handles[at] = arr
            return arr
        if t == 0x73:                               # object: its classes' fields, superclass first
            desc = self.classdesc()
            if desc is None:
                raise ValueError("object without a class description")
            obj, chain = {"class": desc["name"]}, []
            self.handles.append(obj)
            while desc is not None:
                chain.append(desc)
                desc = desc["super"]
            for d in reversed(chain):
                if d["flags"] != 0x02:              # serializable, default field layout only
                    raise ValueError(f"class {d['name']!r} writes itself in a custom layout (flags {d['flags']})")
                for code, name in d["fields"]:
                    obj[name] = self.num(_NUM[code]) if code in _NUM else self.value()
            return obj
        raise ValueError(f"unsupported serialization element 0x{t:02x}")

    def classdesc(self) -> dict | None:
        t = self.num("B")
        if t == 0x70:
            return None
        if t == 0x71:
            d = self.ref()
            if not (isinstance(d, dict) and "fields" in d):
                raise ValueError("a class reference points at something that is not a class description")
            return d
        if t != 0x72:
            raise ValueError(f"unsupported class description 0x{t:02x}")
        d = {"name": self.utf(), "fields": []}
        self.take(8)                                # serialVersionUID
        self.handles.append(d)
        d["flags"] = self.num("B")
        for _ in range(self.num(">H")):
            code, name = chr(self.num("B")), self.utf()
            if code in "[L":
                self.value()                        # the field's type name, a string
            elif code not in _NUM:
                raise ValueError(f"unknown field type {code!r}")
            d["fields"].append((code, name))
        if self.num("B") != 0x78:
            raise ValueError(f"class {d['name']!r} carries annotations")
        d["super"] = self.classdesc()
        return d


def _polygon(blob: bytes, what: str) -> tuple[np.ndarray, np.ndarray] | None:
    """The outline of one RAW_ANNOTATION.DATA blob as full-resolution (x, y) vertices, or None when the
    annotation is not a plain one (subtype 1 to 3) or has fewer than 3 points."""
    try:
        parts = _Stream(bytes(blob)).contents()
    except (ValueError, IndexError, struct.error) as e:
        raise ValueError(f"{what}: {e}") from None
    shapes = [p for p in parts if isinstance(p, dict)]
    if len(shapes) != 1 or not isinstance(parts[-1], dict | bytes):
        raise ValueError(f"{what}: expected one shape object, got {len(shapes)}")
    s = shapes[0]
    xs, ys, n, scale = s.get("xpoints"), s.get("ypoints"), s.get("npoints"), s.get("scale")
    if not (s["class"].endswith(".PolygonExt") and isinstance(xs, np.ndarray) and isinstance(ys, np.ndarray)
            and isinstance(n, int) and isinstance(scale, float)):
        raise ValueError(f"{what}: shape {s['class']!r} is not an Orbit PolygonExt with points and a scale")
    if not (0 <= n <= min(xs.size, ys.size)) or not 0 < scale < float("inf"):
        raise ValueError(f"{what}: {n} points in arrays of {xs.size} and {ys.size}, scale {scale}")
    odd = [k for k in ("inverse", "onlyPoints") if s.get(k)] + [k for k in ("closed",) if s.get(k) == 0]
    offs = s.get("viewPortOffs")                # a java.awt.Point; only (0, 0) has been seen and is understood
    if offs is not None and not (isinstance(offs, dict) and offs.get("x") == 0 and offs.get("y") == 0):
        odd.append("viewPortOffs")
    if odd:
        raise ValueError(f"{what}: polygon variant not supported ({', '.join(odd)} set)")
    tail = parts[-1] if isinstance(parts[-1], bytes) else b""       # subtype and group, after the shape
    if len(tail) >= 4 and struct.unpack(">i", tail[:4])[0] != 0:
        return None
    if n < 3:
        return None
    return xs[:n] * (100.0 / scale), ys[:n] * (100.0 / scale)


def _fill(out: np.ndarray, y0: int, x0: int, vx: np.ndarray, vy: np.ndarray, value: int) -> None:
    """Set `out`, whose corner is pixel (y0, x0), to `value` where a pixel's centre lies inside the polygon
    (even-odd rule)."""
    ya, yb = max(y0, int(np.floor(vy.min()))), min(y0 + out.shape[0], int(np.ceil(vy.max())) + 1)
    xa, xb = max(x0, int(np.floor(vx.min()))), min(x0 + out.shape[1], int(np.ceil(vx.max())) + 1)
    if ya >= yb or xa >= xb:
        return
    x1, y1, x2, y2 = vx, vy, np.roll(vx, -1), np.roll(vy, -1)
    keep = y1 != y2
    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    if x1.size == 0:
        return
    slope, w, step = (x2 - x1) / (y2 - y1), xb - xa, max(1, _ROWS // x1.size)
    for ra in range(ya, yb, step):
        yc = np.arange(ra, min(ra + step, yb))[:, None] + 0.5
        rows, edges = np.nonzero((y1 <= yc) != (y2 <= yc))
        cross = x1[edges] + (yc[rows, 0] - y1[edges]) * slope[edges]
        first = np.clip(np.ceil(cross - 0.5) - xa, 0, w).astype(np.int64)   # first pixel not left of the crossing
        passed = np.bincount(rows * (w + 1) + first, minlength=yc.size * (w + 1)).reshape(yc.size, w + 1)
        left = np.bincount(rows, minlength=yc.size)[:, None] - np.cumsum(passed[:, :w], axis=1)
        out[ra - y0:ra - y0 + yc.size, xa - x0:xb - x0][left % 2 == 1] = value


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


class _Labels:
    axes, access, dtype = "yx", "chunk", np.dtype("uint16")

    def __init__(self, shape: tuple[int, int], voxel_size_nm: dict[str, float] | None,
                 polygons: list[tuple[int, np.ndarray, np.ndarray]]):
        self.shape, self.voxel_size_nm, self.polygons = shape, voxel_size_nm, polygons
        self.boxes = np.array([[vy.min(), vy.max(), vx.min(), vx.max()] for _, vx, vy in polygons]).reshape(-1, 4)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if set(region) - set(self.axes):
            raise KeyError(f"region names axes {sorted(set(region) - set(self.axes))}, the array has {self.axes!r}")
        (y0, y1, ky), (x0, x1, kx) = (_span(region.get(a, slice(None)), n) for a, n in zip(self.axes, self.shape))
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        b = self.boxes
        for i in np.nonzero((b[:, 1] >= y0) & (b[:, 0] <= y1) & (b[:, 3] >= x0) & (b[:, 2] <= x1))[0]:
            ident, vx, vy = self.polygons[i]        # ascending ids: where outlines overlap the higher id stays
            _fill(out, y0, x0, vx, vy, ident)
        out = out[ky] if isinstance(ky, int) else out[ky, :]
        return out[..., kx]


def _grid(fh: BinaryIO | Path) -> tuple[tuple[int, int], dict[str, float] | None, int]:
    """(y, x) size of the TIFF's full-resolution level, its pixel size when the resolution tags give a
    physical unit, and the file's length in bytes."""
    with tifffile.TiffFile(fh) as tif:
        page = tif.series[0].levels[0].keyframe
        size = tif.filehandle.size
        shape, tags = (int(page.imagelength), int(page.imagewidth)), page.tags
        nm = _UNIT_NM.get(int(tags["ResolutionUnit"].value) if "ResolutionUnit" in tags else 0)
        vs = {}
        for a, tag in (("y", "YResolution"), ("x", "XResolution")):
            num, den = tags[tag].value if tag in tags else (0, 0)
            if nm and num and den:
                vs[a] = nm * den / num
    return shape, (vs if len(vs) == 2 else None), int(size)


def _rows(db: Path, name: str, size: int) -> list[bytes]:
    """The DATA blobs of the slide called `name`, ordered by RAW_ANNOTATION_ID."""
    with open(db, "rb") as fh:
        if fh.read(16) != b"SQLite format 3\x00":
            raise ValueError("the database url is not an SQLite file")
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        files = con.execute("SELECT RAW_DATA_FILE_ID, FILESIZE FROM RAW_DATA_FILE WHERE FILENAME = ?", (name,)).fetchall()
        if len(files) > 1:                          # the same name in two folders: the file's length decides
            files = [f for f in files if f[1] == size]
        if len(files) != 1:
            raise ValueError(f"RAW_DATA_FILE has {len(files)} rows for {name!r}; exactly one is needed")
        if files[0][1] is not None and files[0][1] != size:
            raise ValueError(f"{name!r} is {size} bytes, but the database annotated a file of {files[0][1]} bytes")
        return [r[0] for r in con.execute(
            "SELECT DATA FROM RAW_ANNOTATION WHERE RAW_DATA_FILE_ID = ? ORDER BY RAW_ANNOTATION_ID", (files[0][0],))]
    finally:
        con.close()


def open_array(src: Source, io: IO) -> Array:
    if src.params:
        raise ValueError(f"orbit-db-polygon-labels takes no params, got {sorted(src.params)}")
    slides = [u for u in src.urls if u.lower().endswith((".tif", ".tiff"))]
    if len(src.urls) != 2 or len(slides) != 1:
        raise ValueError(f"orbit-db-polygon-labels needs two urls, the Orbit database and one slide TIFF, "
                         f"got {len(src.urls)} with {len(slides)} TIFF")
    slide = slides[0]
    try:
        shape, voxel_size_nm, size = _grid(io.open(slide))
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        shape, voxel_size_nm, size = _grid(io.fetch(slide))
    name = PurePosixPath(slide.split("::")[-1].split("?")[0]).name
    blobs = _rows(io.fetch(next(u for u in src.urls if u != slide)), name, size)
    if len(blobs) > np.iinfo(_Labels.dtype).max:
        raise ValueError(f"{name!r} has {len(blobs)} annotations, more than uint16 ids hold")
    polygons = []
    for ident, blob in enumerate(blobs, 1):
        got = _polygon(blob, f"annotation {ident} of {name!r}")
        if got is not None:
            polygons.append((ident, *got))
    if not polygons:
        raise ValueError(f"the database holds no plain polygon annotation for {name!r}")
    return _Labels(shape, voxel_size_nm, polygons)
