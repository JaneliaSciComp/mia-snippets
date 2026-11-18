"""One Hamamatsu NDPI whole-slide image at a plain URL, its full-resolution level as yxc uint8, read strip by strip
by byte range; or, given that NDPI and its NDPA annotation file (NDP.view XML), the closed freehand regions filled
onto the same pixel grid as yx uint8 class ids chosen by region title.

An NDPA point is in nanometres from the centre of the slide. The NDPI says where its image lies in that frame
(XOffsetFromSlideCentre and YOffsetFromSlideCentre, tags 65422 and 65423: the image centre, in nanometres) and
how large a pixel is (XResolution, YResolution), so a point maps to level-0 pixels with the file's own numbers.
See README.md."""
from typing import BinaryIO
from xml.etree import ElementTree

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

X_OFFSET, Y_OFFSET = 65422, 65423       # NDPI tags: image centre from slide centre, nanometres
UNIT_NM = {2: 2.54e7, 3: 1e7}           # ResolutionUnit: inch, centimetre


def _span(key: int | slice, n: int) -> tuple[int, int, int | slice | np.ndarray]:
    """What `key` asks of an axis of length n: (lo, hi, index into [lo, hi)); an int index drops the axis."""
    if isinstance(key, slice):
        idx = np.arange(n)[key]
        if idx.size == 0:
            return 0, 0, idx
        lo, hi = int(idx.min()), int(idx.max()) + 1
        if key.step in (None, 1):       # the usual window: all of [lo, hi) in order, so no copy to reorder it
            return lo, hi, slice(None)
        return lo, hi, idx - lo
    k = int(key) + (n if key < 0 else 0)
    if not 0 <= k < n:
        raise IndexError(f"index {key} is outside an axis of length {n}")
    return k, k + 1, 0


class _Ndpi:
    """Page 0 (the full-resolution level) of an NDPI: geometry and calibration at open, pixels strip by strip."""

    def __init__(self, fh: BinaryIO, ranged: bool, url: str):
        self._tif = tif = tifffile.TiffFile(fh)
        self.access = "chunk" if ranged else "file"
        if not tif.is_ndpi:
            raise ValueError(f"{url}: a TIFF, but not a Hamamatsu NDPI (no NDPI tags on its first page)")
        self._page = page = tif.pages[0]
        if (page.compression != 7 or page.samplesperpixel != 3 or page.dtype != np.uint8 or page.ndim != 3
                or not page.is_tiled):
            raise ValueError(f"{url}: first page is {page.shape} {page.dtype}, compression {int(page.compression)}; "
                             "only a JPEG RGB uint8 level with restart markers (which tifffile reads as tiles) "
                             "is read")
        ny, nx, nc = page.shape
        if any(p.shape[0] > ny or p.shape[1] > nx for p in tif.pages if p.tags.get(X_OFFSET) is not None
               and p.tags.valueof(65421, 0) > 0):       # 65421 is the lens magnification: macro and map are < 0
            raise ValueError(f"{url}: the first page is not the largest level of the file")
        self.axes, self.shape, self.dtype = "yxc", (ny, nx, nc), np.dtype("uint8")
        unit = UNIT_NM.get(int(page.tags.valueof(296, 0)))
        res = [page.tags.valueof(code) for code in (283, 282)]         # YResolution, XResolution: (num, den)
        self.voxel_size_nm: dict[str, float] | None = None
        if unit and all(r and r[0] > 0 and r[1] > 0 for r in res):
            self.voxel_size_nm = {a: unit * r[1] / r[0] for a, r in zip("yx", res)}
        self.centre_nm = tuple(page.tags.valueof(code) for code in (Y_OFFSET, X_OFFSET))

    def __del__(self):          # the harness never closes arrays; the handle goes with the array
        self._tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        ny, nx, nc = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        c0, c1, ci = _span(region.get("c", slice(None)), nc)
        out = np.zeros((y1 - y0, x1 - x0, nc), self.dtype)
        page, fh = self._page, self._tif.filehandle
        th, tw = page.tilelength, page.tilewidth
        across = -(-nx // tw)
        cols = range(x0 // tw, -(-x1 // tw)) if out.size else range(0)
        for row in range(y0 // th, -(-y1 // th)) if out.size else ():
            tiles = [row * across + col for col in cols]
            spans = [(page.dataoffsets[i], page.databytecounts[i]) for i in tiles]
            lo, hi = min(o for o, _ in spans), max(o + n for o, n in spans)
            if hi - lo <= 2 * sum(n for _, n in spans):         # neighbours in the file: one request for the run
                fh.seek(lo)
                run = fh.read(hi - lo)
                blobs = [run[o - lo:o - lo + n] for o, n in spans]
            else:
                blobs = []
                for o, n in spans:
                    fh.seek(o)
                    blobs.append(fh.read(n))
            for i, col, blob, (_, n) in zip(tiles, cols, blobs, spans):
                if len(blob) != n:
                    raise ValueError(f"NDPI: strip {i} read back short ({len(blob)} of {n} bytes)")
                tile = page.decode(blob, i, jpegtables=page.jpegtables, jpegheader=page.jpegheader)[0]
                tile = np.asarray(tile).reshape(-1, tw, nc) if tile.size == th * tw * nc else None
                if tile is None:
                    raise ValueError(f"NDPI: strip {i} did not decode to {th}x{tw}x{nc} samples")
                ty, tx = row * th, col * tw
                a0, a1, b0, b1 = max(y0, ty), min(y1, ty + th), max(x0, tx), min(x1, tx + tw)
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = tile[a0 - ty:a1 - ty, b0 - tx:b1 - tx]
        return out[..., c0:c1][..., ci][:, xi][yi]       # last axis first: an int index drops its axis


class _Outline:
    """One closed outline in level-0 pixel coordinates (pixel (r, c) is the square [c, c+1) x [r, r+1)), kept as
    the places where its edges cross the rows of pixel centres, so a window is filled without the whole mask."""

    def __init__(self, xs: np.ndarray, ys: np.ndarray):
        xa, ya, xb, yb = xs, ys, np.roll(xs, -1), np.roll(ys, -1)
        lo, hi = np.minimum(ya, yb), np.maximum(ya, yb)
        r0 = np.ceil(lo - 0.5).astype(np.int64)                 # rows whose centre r + 0.5 lies in [lo, hi)
        n = np.maximum(np.ceil(hi - 0.5).astype(np.int64) - r0, 0)
        edge = np.repeat(np.arange(len(xs)), n)
        rows = r0[edge] + np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        cross = xa[edge] + (xb[edge] - xa[edge]) * (rows + 0.5 - ya[edge]) / (yb[edge] - ya[edge])
        cols = np.ceil(cross - 0.5).astype(np.int64)            # first pixel whose centre is right of the crossing
        order = np.lexsort((cols, rows))
        self.rows, self.cols = rows[order], cols[order]
        self.turn = np.where(yb[edge] > ya[edge], 1, -1).astype(np.int32)[order]
        inside = np.cumsum(self.turn)[:-1] != 0         # a row's turns sum to 0, so this never spans two rows
        self.area = int(np.diff(self.cols)[inside].sum())       # pixels it fills
        self.box = (int(np.floor(ys.min())), int(np.ceil(ys.max())), int(np.floor(xs.min())), int(np.ceil(xs.max())))

    def fill(self, out: np.ndarray, y0: int, x0: int, value: int) -> None:
        """Set to `value` the pixels of `out`, a window whose corner is (y0, x0), whose centres the outline winds
        around (nonzero rule)."""
        a0, a1 = max(y0, self.box[0]), min(y0 + out.shape[0], self.box[1])
        b0, b1 = max(x0, self.box[2]), min(x0 + out.shape[1], self.box[3])
        if a0 >= a1 or b0 >= b1:
            return
        i, j = np.searchsorted(self.rows, [a0, a1])
        step = np.zeros((a1 - a0, b1 - b0 + 1), np.int32)       # a crossing left of the window counts from b0
        np.add.at(step, (self.rows[i:j] - a0, np.clip(self.cols[i:j] - b0, 0, b1 - b0)), self.turn[i:j])
        out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][np.cumsum(step[:, :-1], axis=1) != 0] = value


class _Regions:
    """The closed freehand regions of an NDPA on its NDPI's level-0 grid: a pixel holds the 1-based position, in
    `classes`, of the title of the region that encloses its centre (the smallest such region, where one is drawn
    inside another); 0 where none does."""

    def __init__(self, io: IO, url: str, grid: _Ndpi, classes: list[str]):
        if grid.voxel_size_nm is None or len(grid.voxel_size_nm) != 2 or None in grid.centre_nm:
            raise ValueError("the NDPI states no resolution or no offset from the slide centre (tags 282, 283, "
                             "296, 65422, 65423), so NDPA nanometres cannot be placed on its pixels")
        self._io, self._url, self._grid, self._classes = io, url, grid, classes
        self._outlines: list[tuple[int, _Outline]] | None = None
        self.axes, self.shape, self.dtype = "yx", grid.shape[:2], np.dtype("uint8")
        self.voxel_size_nm = grid.voxel_size_nm
        self.access = "plane"           # the whole annotation file is read on the first read, whatever the region

    def _load(self) -> list[tuple[int, _Outline]]:
        try:
            with self._io.open(self._url) as fh:
                text = fh.read()
        except NeedsFetch:
            text = self._io.fetch(self._url).read_bytes()
        name = self._url.rsplit("/", 1)[-1]
        root = ElementTree.fromstring(text)
        if root.tag != "annotations":
            raise ValueError(f"{name}: root element <{root.tag}>, not an NDPA <annotations>")
        (ny, nx), (cy, cx) = self.shape, self._grid.centre_nm
        sy, sx = (self._grid.voxel_size_nm[a] for a in "yx")
        found = []
        for i, state in enumerate(root.findall("ndpviewstate"), 1):
            title = (state.findtext("title") or "").strip()
            unit, ann = (state.findtext("coordformat") or "").strip(), state.find("annotation")
            where = f"{name}: region {i} ({title!r})"
            if title not in self._classes:
                raise ValueError(f"{where}: its title is not in params.classes {self._classes}")
            if unit != "nanometers":
                raise ValueError(f"{where}: coordformat {unit!r}; only 'nanometers' is read")
            if ann is None or ann.get("type") != "freehand" or (ann.findtext("closed") or "").strip() != "1":
                kind = None if ann is None else (ann.get("type"), ann.findtext("closed"))
                raise ValueError(f"{where}: annotation (type, closed) = {kind}; only a closed freehand outline "
                                 "is filled")
            points = ann.findall("pointlist/point")
            if len(points) < 3:         # a click or a stroke that was never drawn round anything: no area
                continue
            xy = np.array([[float(p.findtext("x")), float(p.findtext("y"))] for p in points])
            found.append((self._classes.index(title) + 1,
                          _Outline((xy[:, 0] - cx) / sx + nx / 2, (xy[:, 1] - cy) / sy + ny / 2)))
        if found and not any(o.box[0] < ny and o.box[1] > 0 and o.box[2] < nx and o.box[3] > 0 for _, o in found):
            raise ValueError(f"{name}: none of its {len(found)} regions touches the image ({ny}x{nx} pixels "
                             f"centred {cy}, {cx} nm from the slide centre); is it this slide's annotation?")
        return sorted(found, key=lambda f: -f[1].area)  # stable: largest first, then file order

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._outlines is None:
            self._outlines = self._load()
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for value, outline in self._outlines if out.size else ():      # a smaller region is painted over a larger
            outline.fill(out, y0, x0, value)
        return out[yi][..., xi]


def _open(io: IO, url: str) -> tuple[BinaryIO, bool]:
    """(file, read by range?)"""
    try:
        return io.open(url), True
    except NeedsFetch:          # a server without Range: stage the whole file
        return open(io.fetch(url), "rb"), False


def open_array(src: Source, io: IO) -> Array:
    classes = src.params.get("classes")
    ndpa = [u for u in src.urls if u.split("?", 1)[0].split("#", 1)[0].lower().endswith(".ndpa")]
    rest = [u for u in src.urls if u not in ndpa]
    if len(rest) != 1 or len(ndpa) > 1:
        raise ValueError("urls must name one NDPI, and for a label its one .ndpa as well; got "
                         f"{len(rest)} and {len(ndpa)}")
    if ndpa and not (isinstance(classes, list) and classes and all(isinstance(c, str) and c for c in classes)
                     and len(set(classes)) == len(classes) and len(classes) <= 255):
        raise ValueError("params.classes must list the region titles of the NDPA, in class id order (1 to 255 "
                         f"distinct names), got {classes!r}")
    if not ndpa and "classes" in src.params:
        raise ValueError("params.classes is for the label form (urls with an .ndpa); got one NDPI only")
    fh, ranged = _open(io, rest[0])
    grid = _Ndpi(fh, ranged, rest[0])
    return _Regions(io, ndpa[0], grid, list(classes)) if ndpa else grid
