"""An EMAN helix .box file as instance ids on the grid of the MRC micrograph or movie stack it was boxed on.

The file is the text table EMAN1's boxer and EMAN2's e2helixboxer write for helices: two rows per helix,
`x y width height -1` for its start and `x y width height -2` for its end, where (x, y) is the corner of a
width x height square centred on that end of the helix axis. A helix becomes the rectangle EMAN draws for it: the
band of that width along the axis between the two end points, filled with the helix's 1-based number in the file.

A .box file has no image size, so the second url is the MRC the helices were boxed on; only its 1024-byte header
is read, for the shape (and voxel size) mrc-http-3 gives that file. The picks are 2D: every z plane of the label
(every frame of a movie stack) is the same plane.
"""
import math
import struct

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

START, END = -1, -2                                 # fifth column: this row is the start / the end of a helix
ROWS = 256                                          # rows rasterised at a time, to bound the temporaries
Helix = tuple[float, float, float, float, float]    # x0, y0, x1, y1 of the axis end points and the width, pixels


def _text(io: IO, url: str) -> str:
    try:
        with io.open(url) as f:
            data = f.read()
    except NeedsFetch:          # a deflated zip member, or a server without Range: stage the whole (small) file
        data = io.fetch(url).read_bytes()
    try:
        return data.decode("ascii")
    except UnicodeDecodeError as e:
        raise ValueError(f"{url} is not an EMAN .box text file: {e}") from None


def parse_helices(text: str, name: str = ".box") -> list[Helix]:
    """The helices of an EMAN helix .box table, in file order. Blank lines are skipped; anything else that is
    not a start row followed by an end row of the same square box is refused."""
    helices: list[Helix] = []
    start: tuple[float, float, float] | None = None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        where = f"{name} line {n}"
        fields = line.split()
        try:
            if len(fields) != 5:
                raise ValueError
            x, y, w, h = (float(v) for v in fields[:4])
            flag = int(fields[4])
        except ValueError:
            raise ValueError(f"{where}: expected 'x y width height flag', got {line.strip()[:80]!r}") from None
        if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0:
            raise ValueError(f"{where}: bad box {line.strip()[:80]!r}")
        if w != h:
            raise ValueError(f"{where}: box is {w:g} x {h:g}; a helix box is square (its side is the helix width)")
        if flag not in (START, END):
            raise ValueError(f"{where}: flag {flag} is not {START} (helix start) or {END} (helix end); this "
                             f"snippet reads helix box files only, not particle boxes")
        if (flag == START) != (start is None):
            raise ValueError(f"{where}: a helix {'start' if flag == START else 'end'} row where "
                             f"{'an end' if flag == START else 'a start'} row was expected")
        centre = (x + w / 2, y + w / 2)                 # EMAN writes the corner: end point - width / 2
        if start is None:
            start = (*centre, w)
        else:
            if start[2] != w:
                raise ValueError(f"{where}: end box width {w:g} differs from its start's {start[2]:g}")
            helices.append((start[0], start[1], *centre, w))
            start = None
    if start is not None:
        raise ValueError(f"{name}: the last helix has a start row and no end row")
    return helices


def _grid(io: IO, url: str) -> tuple[tuple[int, int, int], dict[str, float] | None]:
    """(nz, ny, nx) and the voxel size of an MRC file, from its header, as mrc-http-3 reports them."""
    with io.open(url) as f:
        h = f.read(1024)
    if len(h) < 1024:
        raise ValueError(f"{url} is {len(h)} bytes, too short for an MRC header")
    order = ">" if h[212] == 0x11 else "<"                     # MACHST: 0x44 little, 0x11 big
    nx, ny, nz = struct.unpack_from(order + "3i", h, 0)
    mx, my, mz, *cell = struct.unpack_from(order + "3i3f", h, 28)
    maps = struct.unpack_from(order + "3i", h, 64)
    if min(nx, ny, nz) < 1 or max(nx, ny, nz) > 1 << 24:
        raise ValueError(f"{url}: bad MRC header, n = {nx, ny, nz}")
    if maps != (1, 2, 3):
        raise ValueError(f"{url}: unsupported axis order mapc/mapr/maps = {maps}, only 1 2 3")
    n = [m if m > 0 else k for m, k in zip((mx, my, mz), (nx, ny, nz))]
    voxel = {a: c / k / 10 for a, c, k in zip("xyz", cell, n)} if all(c > 0 for c in cell) else None
    return (nz, ny, nx), voxel


def rasterise(helices: list[Helix], ny: int, nx: int) -> np.ndarray:
    """A (ny, nx) uint16 plane: 0, or the 1-based number of the last helix whose band covers the pixel's centre.
    Pixel (row, col) covers [col, col + 1) x [row, row + 1) in box coordinates; parts off the grid are dropped."""
    plane = np.zeros((ny, nx), np.uint16)
    for number, (x0, y0, x1, y1, w) in enumerate(helices, 1):
        r = w / 2
        cols = slice(max(math.floor(min(x0, x1) - r), 0), min(math.ceil(max(x0, x1) + r), nx))
        top, bottom = max(math.floor(min(y0, y1) - r), 0), min(math.ceil(max(y0, y1) + r), ny)
        if cols.start >= cols.stop:
            continue
        dx = np.arange(cols.start, cols.stop) + 0.5 - x0
        length = math.hypot(x1 - x0, y1 - y0)
        for lo in range(top, bottom, ROWS):
            rows = slice(lo, min(lo + ROWS, bottom))
            dy = (np.arange(rows.start, rows.stop) + 0.5 - y0)[:, None]
            if length == 0:                                 # start = end: the box itself
                inside = (np.abs(dx) <= r) & (np.abs(dy) <= r)
            else:
                ux, uy = (x1 - x0) / length, (y1 - y0) / length
                along, across = dx * ux + dy * uy, dy * ux - dx * uy
                inside = (along >= 0) & (along <= length) & (np.abs(across) <= r)
            plane[rows, cols][inside] = number
    return plane


class _HelixBoxes:
    axes, access = "zyx", "file"
    dtype: np.dtype = np.dtype("uint16")

    def __init__(self, helices: list[Helix], shape: tuple[int, int, int], voxel: dict[str, float] | None) -> None:
        self.helices, self.shape, self.voxel_size_nm = helices, shape, voxel
        self._plane: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:                             # computed once; later reads slice it
            self._plane = rasterise(self.helices, *self.shape[1:])
        z, y, x = (region.get(a, slice(None)) for a in "zyx")
        planes = range(self.shape[0])[z]                    # IndexError for a z off the stack
        out = np.array(self._plane[y, x])
        return out if isinstance(planes, int) else np.repeat(out[None], len(planes), axis=0)


def open_array(src: Source, io: IO) -> Array:
    boxes = [u for u in src.urls if u.lower().endswith(".box")]
    grids = [u for u in src.urls if not u.lower().endswith(".box")]
    if len(boxes) != 1 or len(grids) != 1:
        raise ValueError(f"eman-helix-box-mrc takes two urls, one .box file and the MRC it was boxed on; got "
                         f"{len(boxes)} .box and {len(grids)} other")
    shape, voxel = _grid(io, grids[0])
    helices = parse_helices(_text(io, boxes[0]), boxes[0].rsplit("/", 1)[-1])
    if len(helices) > np.iinfo(np.uint16).max:
        raise ValueError(f"{len(helices)} helices do not fit uint16 ids")
    return _HelixBoxes(helices, shape, voxel)
