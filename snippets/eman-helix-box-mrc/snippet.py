"""An EMAN helix .box file rasterised as instance ids on the grid of the MRC micrograph or movie stack it was
boxed on.

The .box is text, one box per line: `x y w h flag`, x and y the corner of a w x h box in micrograph pixels. A
helix is two consecutive lines, flag -1 (start) then -2 (end): the centres of the two boxes are the ends of the
helix axis and the box size is the helix width (as EMAN1 boxer and e2helixboxer write them). Each helix is
filled as the rectangle of that width around its axis, with its 1-based number in the file.

The .box does not say how large the micrograph is, so the second url is the MRC: its 1024-byte header gives the
shape (no pixel is read). The label is zyx with the MRC's nz, every z plane the same, so it matches the stack as
mrc-http-3 presents it. See README.md.
"""
import math
import struct

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MRC_MODES = (0, 1, 2, 6, 12)                 # the modes mrc-http-3 reads, so the grid is one it can present
START, END = -1, -2                          # the flag column of a helix box: its start box, its end box
Helix = tuple[float, float, float, float, float]   # axis start x, y; axis end x, y; width (pixels)


def _is_box(url: str) -> bool:
    return url.split("?", 1)[0].split("#", 1)[0].lower().endswith(".box")


def _mrc_grid(io: IO, url: str) -> tuple[tuple[int, int, int], dict[str, float] | None]:
    """((nz, ny, nx), voxel size in nm or None) from the MRC header, as mrc-http-3 reads them."""
    with io.open(url) as fh:
        h = fh.read(1024)
    if len(h) < 1024:
        raise ValueError(f"{url}: {len(h)} bytes, too short for an MRC header")
    order = ">" if h[212] == 0x11 else "<"                     # MACHST: 0x44 little, 0x11 big
    nx, ny, nz, mode = struct.unpack_from(order + "4i", h, 0)
    mx, my, mz, *cell = struct.unpack_from(order + "3i3f", h, 28)
    maps = struct.unpack_from(order + "3i", h, 64)
    if mode not in MRC_MODES or min(nx, ny, nz) < 1:
        raise ValueError(f"{url}: not an MRC header this snippet reads (n = {nx, ny, nz}, mode {mode})")
    if maps != (1, 2, 3):
        raise ValueError(f"{url}: unsupported axis order mapc/mapr/maps = {maps}, only 1 2 3")
    n = [m if m > 0 else k for m, k in zip((mx, my, mz), (nx, ny, nz))]
    voxel = {a: c / k / 10 for a, c, k in zip("xyz", cell, n)} if all(c > 0 for c in cell) else None
    return (nz, ny, nx), voxel


def _helices(text: str, name: str, ny: int, nx: int) -> list[Helix]:
    """The helices of a .box file's text, in file order. Raises on anything that is not a start/end pair."""
    boxes: list[tuple[float, float, float, float, int]] = []
    for i, line in enumerate(text.splitlines(), 1):
        f = line.split()
        if not f:
            continue
        try:
            if len(f) != 5:
                raise ValueError
            x, y, w, h = (float(v) for v in f[:4])
            flag = int(f[4])
        except ValueError:
            raise ValueError(f"{name} line {i}: expected 'x y w h flag', got {line[:80]!r}") from None
        if not all(map(math.isfinite, (x, y, w, h))) or flag not in (START, END):
            raise ValueError(f"{name} line {i}: not a helix box (flag {START} or {END}, finite numbers): {line[:80]!r}")
        if w <= 0 or w != h:
            raise ValueError(f"{name} line {i}: box {w:g} x {h:g}; a helix box is square, its size the helix width")
        boxes.append((x, y, w, h, flag))
    if len(boxes) % 2 or any(b[4] != (START, END)[k % 2] for k, b in enumerate(boxes)):
        raise ValueError(f"{name}: flags {[b[4] for b in boxes][:20]} are not start ({START}) / end ({END}) pairs")
    helices: list[Helix] = []
    for k in range(0, len(boxes), 2):
        (x0, y0, w0, _, _), (x1, y1, w1, _, _) = boxes[k], boxes[k + 1]
        ends = (x0 + w0 / 2, y0 + w0 / 2, x1 + w1 / 2, y1 + w1 / 2)
        if w0 != w1:
            raise ValueError(f"{name} helix {k // 2 + 1}: start box is {w0:g} wide, end box {w1:g}")
        if ends[:2] == ends[2:]:
            raise ValueError(f"{name} helix {k // 2 + 1}: start and end are the same point {ends[:2]}")
        if not (0 <= ends[0] <= nx and 0 <= ends[2] <= nx and 0 <= ends[1] <= ny and 0 <= ends[3] <= ny):
            raise ValueError(f"{name} helix {k // 2 + 1}: axis {ends[:2]} -> {ends[2:]} leaves the {nx} x {ny} "
                             f"(x, y) grid of the MRC: not the micrograph this file was boxed on?")
        helices.append((*ends, w0))
    if len(helices) > np.iinfo(np.uint16).max:
        raise ValueError(f"{name}: {len(helices)} helices do not fit uint16 ids")
    return helices


class _HelixLabel:
    axes, access = "zyx", "plane"            # nothing is fetched by read: a plane is painted from the parsed table
    dtype: np.dtype = np.dtype(np.uint16)

    def __init__(self, helices: list[Helix], shape: tuple[int, int, int], voxel: dict[str, float] | None) -> None:
        self._helices = helices
        self.shape: tuple[int, ...] = shape
        self.voxel_size_nm: dict[str, float] | None = voxel

    def _paint(self, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
        """ids at the pixels (rows x cols): a pixel belongs to a helix when its centre (col + 0.5, row + 0.5) lies
        in the rectangle of the helix width around the axis. A later helix is painted over an earlier one."""
        out = np.zeros((len(rows), len(cols)), self.dtype)
        for ident, (ax, ay, bx, by, width) in enumerate(self._helices, 1):
            length = math.hypot(bx - ax, by - ay)
            ux, uy = (bx - ax) / length, (by - ay) / length
            reach = width / 2                # the rectangle's bounding box: the axis's, grown by half the width
            r = np.flatnonzero((rows + 0.5 >= min(ay, by) - reach) & (rows + 0.5 <= max(ay, by) + reach))
            c = np.flatnonzero((cols + 0.5 >= min(ax, bx) - reach) & (cols + 0.5 <= max(ax, bx) + reach))
            if not len(r) or not len(c):
                continue
            dy, dx = (rows[r] + 0.5 - ay)[:, None], (cols[c] + 0.5 - ax)[None, :]
            along, across = dx * ux + dy * uy, dx * uy - dy * ux
            inside = (along >= 0) & (along <= length) & (np.abs(across) <= reach)
            block = out[np.ix_(r, c)]
            block[inside] = ident
            out[np.ix_(r, c)] = block
        return out

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        nz, ny, nx = self.shape
        z, rows, cols = (np.arange(n)[region.get(a, slice(None))] for a, n in zip("zyx", (nz, ny, nx)))
        plane = self._paint(np.atleast_1d(rows), np.atleast_1d(cols)).reshape(rows.shape + cols.shape)
        return np.broadcast_to(plane, z.shape + plane.shape).copy()       # every frame holds the same boxes


def open_array(src: Source, io: IO) -> Array:
    boxes = [u for u in src.urls if _is_box(u)]
    rest = [u for u in src.urls if not _is_box(u)]
    if len(boxes) != 1 or len(rest) != 1:
        raise ValueError(f"urls must be one .box file and the one MRC it was boxed on, got {len(boxes)} .box "
                         f"and {len(rest)} other urls")
    shape, voxel = _mrc_grid(io, rest[0])
    try:
        with io.open(boxes[0]) as fh:
            raw = fh.read()
    except NeedsFetch:                       # a server without Range: stage the (small) file whole
        raw = io.fetch(boxes[0]).read_bytes()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(f"{boxes[0]}: not text; an EMAN .box file is ASCII lines of 'x y w h flag'") from None
    return _HelixLabel(_helices(text, boxes[0], shape[1], shape[2]), shape, voxel)
