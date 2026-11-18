"""The helices of one EMAN helix .box text file, filled as instance ids on the grid of the MRC stack they were boxed on.

A helix .box file is a table, two rows per helix: `x y w h -1` (the box at the helix's start) and `x y w h -2` (the
box at its end), with x, y the box corner in pixels of the micrograph. It states no image size, so the grid comes
from the header of the paired MRC, which is the second url. No pixel of the MRC is read.
"""
import math
import struct

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

START, END = -1, -2          # the fifth column: the box at a helix's start point, the box at its end point
MAX_BOX_BYTES = 1 << 20      # a helix .box is a few hundred bytes; anything this large is not one

Helix = tuple[float, float, float, float, float]      # start x, start y, end x, end y, width; pixels of the grid


def _grid(header: bytes, url: str) -> tuple[int, int, int]:
    """(nz, ny, nx) from an MRC header; the planes are xy sections stored row by row (mapc/mapr/maps 1 2 3)."""
    if len(header) < 1024:
        raise ValueError(f"{url}: {len(header)} bytes, too short for an MRC header")
    order = ">" if header[212] == 0x11 else "<"                    # MACHST: 0x44 little, 0x11 big
    nx, ny, nz = struct.unpack_from(order + "3i", header, 0)
    maps = struct.unpack_from(order + "3i", header, 64)
    if min(nx, ny, nz) < 1:
        raise ValueError(f"{url}: bad MRC header: n = {nx, ny, nz}")
    if maps != (1, 2, 3):
        raise ValueError(f"{url}: unsupported axis order mapc/mapr/maps = {maps}, only 1 2 3")
    return nz, ny, nx


def _helices(data: bytes, url: str, ny: int, nx: int) -> list[Helix]:
    """The file's helices in file order. Refuses anything that is not start/end pairs of square boxes on the grid."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not an EMAN .box text file: starts {data[:16]!r}") from None
    rows: list[tuple[int, tuple[int, ...]]] = []
    for n, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        try:
            row = tuple(int(f) for f in fields)
        except ValueError:
            raise ValueError(f"{url} line {n}: not integers: {line[:80]!r}") from None
        if len(row) != 5:
            raise ValueError(f"{url} line {n}: {len(row)} columns, a helix box row has 5 (x y w h flag): {line[:80]!r}")
        rows.append((n, row))
    if len(rows) % 2:
        raise ValueError(f"{url}: {len(rows)} rows, a helix is a start row and an end row")
    out: list[Helix] = []
    for (n, (x1, y1, w1, h1, f1)), (_, (x2, y2, w2, h2, f2)) in zip(rows[::2], rows[1::2]):
        if (f1, f2) != (START, END):
            raise ValueError(f"{url} line {n}: flags {f1}, {f2}: a helix is a row flagged {START} then one flagged "
                             f"{END} (a file of particle boxes is not a helix file)")
        if not w1 == h1 == w2 == h2 or w1 < 1:
            raise ValueError(f"{url} line {n}: boxes {w1}x{h1} and {w2}x{h2}: the two boxes of a helix are equal "
                             f"squares, whose edge is the helix width")
        ax, ay, bx, by = x1 + w1 / 2, y1 + w1 / 2, x2 + w1 / 2, y2 + w1 / 2       # box centres: the axis ends
        if (ax, ay) == (bx, by):
            raise ValueError(f"{url} line {n}: the helix starts and ends at the same point {ax, ay}")
        if not (0 <= ax <= nx and 0 <= bx <= nx and 0 <= ay <= ny and 0 <= by <= ny):
            raise ValueError(f"{url} line {n}: helix from {ax, ay} to {bx, by} leaves the {nx} x {ny} grid of the "
                             f"MRC: the .box file was not made on this image")
        out.append((ax, ay, bx, by, float(w1)))
    if len(out) > 65535:
        raise ValueError(f"{url}: {len(out)} helices do not fit uint16 ids")
    return out


def _span(r: range) -> tuple[int, int]:
    """[lo, hi) covering the indices of a non-empty range of either direction."""
    return min(r[0], r[-1]), max(r[0], r[-1]) + 1


class _HelixBoxes:
    axes: str = "zyx"
    access: str = "file"          # the whole table is in memory after open; a read costs no bytes
    dtype: np.dtype = np.dtype("uint16")
    voxel_size_nm: dict[str, float] | None = None       # a .box file states no pixel size

    def __init__(self, shape: tuple[int, int, int], helices: list[Helix]) -> None:
        self.shape: tuple[int, ...] = shape
        self.helices: list[Helix] = helices             # id k + 1 is helices[k]

    def _fill(self, lo: int, hi: int, left: int, right: int) -> np.ndarray:
        """Rows lo..hi, columns left..right: pixel (column i, row j) is helix k's when the point (i, j) is in the
        rectangle around k's axis, as long as the axis and as wide as the box. A later helix overwrites an earlier."""
        out = np.zeros((hi - lo, right - left), self.dtype)
        for k, (ax, ay, bx, by, width) in enumerate(self.helices, 1):
            half, length = width / 2, math.hypot(bx - ax, by - ay)
            ux, uy = (bx - ax) / length, (by - ay) / length
            # the rectangle's bounding box, cut to the block
            ex, ey = abs(uy) * half, abs(ux) * half
            x0, x1 = max(left, math.floor(min(ax, bx) - ex)), min(right, math.ceil(max(ax, bx) + ex) + 1)
            y0, y1 = max(lo, math.floor(min(ay, by) - ey)), min(hi, math.ceil(max(ay, by) + ey) + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            dx, dy = np.arange(x0, x1) - ax, (np.arange(y0, y1) - ay)[:, None]
            along, across = dx * ux + dy * uy, dy * ux - dx * uy
            out[y0 - lo:y1 - lo, x0 - left:x1 - left][(along >= 0) & (along <= length) & (np.abs(across) <= half)] = k
        return out

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nz, ny, nx = self.shape
        z, y, x = (region.get(a, slice(None)) for a in "zyx")
        if not isinstance(z, int):                      # every frame of the stack carries the same plane
            plane = self.read({"z": 0, "y": y, "x": x})
            return np.repeat(plane[None], len(range(nz)[z]), axis=0)
        range(nz)[z]                                    # IndexError for a frame the stack does not have
        rows, cols = range(ny)[y], range(nx)[x]
        if isinstance(rows, int):
            return self.read({"z": z, "y": slice(rows, rows + 1), "x": x})[0]
        if isinstance(cols, int):
            return self.read({"z": z, "y": y, "x": slice(cols, cols + 1)})[:, 0]
        if not rows or not cols:
            return np.zeros((len(rows), len(cols)), self.dtype)
        (lo, hi), (left, right) = _span(rows), _span(cols)
        return self._fill(lo, hi, left, right)[np.asarray(rows) - lo][:, np.asarray(cols) - left]


def open_array(src: Source, io: IO) -> Array:
    boxes = [u for u in src.urls if u.lower().endswith(".box")]
    stacks = [u for u in src.urls if u not in boxes]
    if len(boxes) != 1 or len(stacks) != 1:
        raise ValueError(f"eman-helix-box-mrc-grid takes two urls, one .box file and the MRC it was boxed on, got "
                         f"{list(src.urls)}")
    nz, ny, nx = _grid(io.open(stacks[0]).read(1024), stacks[0])
    try:
        fh = io.open(boxes[0])
        size = fh.seek(0, 2)
        if size > MAX_BOX_BYTES:
            raise ValueError(f"{boxes[0]}: {size} bytes is too large for a helix .box file")
        fh.seek(0)
        data = fh.read()
    except NeedsFetch:
        path = io.fetch(boxes[0])
        if path.stat().st_size > MAX_BOX_BYTES:
            raise ValueError(f"{boxes[0]}: {path.stat().st_size} bytes is too large for a helix .box file") from None
        data = path.read_bytes()
    return _HelixBoxes((nz, ny, nx), _helices(data, boxes[0], ny, nx))
