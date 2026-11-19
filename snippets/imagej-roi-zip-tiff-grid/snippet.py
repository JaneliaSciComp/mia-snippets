"""An ImageJ ROI zip (a RoiSet: one .roi per object, as the ROI Manager and Cellpose's --save_rois write it)
filled onto the yx pixel grid of the TIFF it was drawn on, as instance ids (yx).

urls: the ROI zip and its TIFF, each a plain URL or a zip member (the ROI zip may be a zip nested in a zip). The
TIFF gives the grid (y and x size, voxel size) and none of its pixels are read. No ImageJ ROI library is on the
harness allowlist, so the .roi files are parsed here: the 64-byte big-endian header and the coordinate block
after it. See README.md."""
import struct
import zipfile

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

ROI_TYPES = {0: "polygon", 1: "rectangle", 2: "oval", 3: "line", 4: "freeline", 5: "polyline", 6: "noRoi",
             7: "freehand", 8: "traced", 9: "angle", 10: "point"}
OUTLINES = (0, 7, 8)            # the types stored as one closed list of vertices
SUB_PIXEL = 128                 # header option bit: float coordinates follow the int16 ones
VERTICES = ("corners", "pixels")
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form


def _name(url: str) -> str:
    """The file a url names: the member of `<zip>::<member>`, else the url without query or fragment."""
    return url.rsplit("::", 1)[1] if "::" in url else url.split("?", 1)[0].split("#", 1)[0]


def _outline(b: bytes, name: str) -> tuple[np.ndarray, np.ndarray]:
    """The vertices (xs, ys) of one .roi holding a polygon, freehand or traced outline, in image coordinates."""
    if len(b) < 64 or b[:4] != b"Iout":
        raise ValueError(f"{name}: not an ImageJ .roi (starts with {b[:4]!r}, {len(b)} bytes)")
    version, kind = struct.unpack_from(">hB", b, 4)
    top, left, _, _, n = struct.unpack_from(">hhhhH", b, 8)
    subtype, options = struct.unpack_from(">hh", b, 48)
    if kind not in OUTLINES or subtype != 0:
        raise ValueError(f"{name}: ROI type {ROI_TYPES.get(kind, kind)} (subtype {subtype}) is not read; only "
                         "plain polygon, freehand and traced outlines are")
    if n < 3:
        raise ValueError(f"{name}: an outline of {n} points has no interior")
    floats = bool(options & SUB_PIXEL) and version >= 222
    if len(b) < 64 + (12 if floats else 4) * n:
        raise ValueError(f"{name}: {len(b)} bytes are too few for {n} points"
                         + (" with sub-pixel coordinates" if floats else ""))
    if floats:                                                  # n int16 x, n int16 y, then n float x, n float y
        xy = np.frombuffer(b, ">f4", 2 * n, 64 + 4 * n).astype("f8")
        return xy[:n], xy[n:]
    xy = np.frombuffer(b, ">i2", 2 * n, 64).astype("f8")         # int16 offsets from the bounding box corner
    return xy[:n] + left, xy[n:] + top


def _fill(xs: np.ndarray, ys: np.ndarray, pixels: bool) -> tuple[int, int, np.ndarray]:
    """A closed outline as (y, x, mask), y and x being the image position of mask[0, 0].

    corners: an integer coordinate is a pixel corner, and the mask is the pixels whose centres the outline
    encloses (even-odd rule). pixels: an integer coordinate is the pixel of that index, and the mask is the pixels
    the outline passes through plus the pixels it encloses."""
    off = 0.0 if pixels else 0.5                                # where in a pixel its sample point lies
    y0, x0 = int(np.floor(ys.min())), int(np.floor(xs.min()))
    h = int(np.ceil(ys.max())) - y0 + (1 if pixels else 0)
    w = int(np.ceil(xs.max())) - x0 + (1 if pixels else 0)
    mask = np.zeros((max(h, 1), max(w, 1)), bool)
    xa, ya, xb, yb = xs, ys, np.roll(xs, -1), np.roll(ys, -1)
    # an edge crosses the rows r with min(ya, yb) <= r + off < max(ya, yb); every pixel of such a row whose sample
    # point lies left of the crossing changes side, so a pixel is inside when an odd number of crossings lie
    # right of it
    lo = np.ceil(np.minimum(ya, yb) - off).astype(np.int64)
    count = np.ceil(np.maximum(ya, yb) - off).astype(np.int64) - lo
    edge = np.repeat(np.arange(len(xs)), count)
    if edge.size:
        rows = lo[edge] + np.arange(edge.size) - np.repeat(np.cumsum(count) - count, count)
        cross = xa[edge] + (xb[edge] - xa[edge]) * (rows + off - ya[edge]) / (yb[edge] - ya[edge])
        left_of = np.clip(np.ceil(cross - off).astype(np.int64) - x0, 0, mask.shape[1])   # columns left of it
        flips = np.zeros((mask.shape[0], mask.shape[1] + 1), np.int64)
        np.add.at(flips, (rows - y0, left_of), 1)
        right = flips.sum(1, keepdims=True) - np.cumsum(flips, 1)       # crossings right of each column
        mask = (right[:, :-1] % 2).astype(bool)
    if pixels:                  # the outline's own pixels: each edge at unit steps along its longer axis
        steps = np.maximum(np.abs(xb - xa), np.abs(yb - ya))
        m = np.maximum(np.ceil(steps).astype(np.int64), 1)
        edge = np.repeat(np.arange(len(xs)), m)
        t = (np.arange(edge.size) - np.repeat(np.cumsum(m) - m, m)) / m[edge]
        px = np.floor(xa[edge] + t * (xb[edge] - xa[edge]) + 0.5).astype(np.int64) - x0
        py = np.floor(ya[edge] + t * (yb[edge] - ya[edge]) + 0.5).astype(np.int64) - y0
        mask[py, px] = True
    return y0, x0, mask


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


class _Rois:
    """Pixel (y, x) holds the 1-based position, in the zip's directory, of the ROI that covers it; 0 where none
    does."""

    def __init__(self, outlines: list[tuple[int, np.ndarray, np.ndarray]], count: int, shape: tuple[int, int],
                 voxel_size_nm: dict[str, float] | None, pixels: bool):
        self._outlines, self._pixels = outlines, pixels
        self._masks: list[tuple[int, int, int, np.ndarray]] | None = None
        self.axes, self.shape = "yx", shape
        self.dtype = np.dtype("uint16" if count <= 65535 else "uint32")
        self.voxel_size_nm = voxel_size_nm
        self.access = "plane"           # every outline is filled on the first read, whatever the region

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._masks is None:
            self._masks = [(i, *_fill(xs, ys, self._pixels)) for i, xs, ys in self._outlines]
        ny, nx = self.shape
        y0, y1, yi = _span(region.get("y", slice(None)), ny)
        x0, x1, xi = _span(region.get("x", slice(None)), nx)
        out = np.zeros((y1 - y0, x1 - x0), self.dtype)
        for i, my, mx, mask in self._masks:         # in zip order: where two ROIs overlap, the later one is kept
            a0, a1 = max(y0, my), min(y1, my + mask.shape[0])
            b0, b1 = max(x0, mx), min(x1, mx + mask.shape[1])
            if a0 < a1 and b0 < b1:
                out[a0 - y0:a1 - y0, b0 - x0:b1 - x0][mask[a0 - my:a1 - my, b0 - mx:b1 - mx]] = i
        return np.array(out[yi][..., xi])


def _grid(io: IO, url: str) -> tuple[tuple[int, int], dict[str, float] | None]:
    """The (y, x) size of a TIFF's first series and its y/x voxel size (OME PhysicalSize, or ImageJ unit with
    X/YResolution; None when the file doesn't say). Header only when the file can be read by range."""
    try:
        tif = tifffile.TiffFile(io.open(url))
    except NeedsFetch:          # deflated member, or a server without Range: stage the whole file
        tif = tifffile.TiffFile(io.fetch(url))
    with tif:
        s = tif.series[0]
        if s.axes.count("Y") != 1 or s.axes.count("X") != 1:
            raise ValueError(f"{_name(url)}: tifffile axes {s.axes!r} {tuple(s.shape)} have no single Y and X")
        shape = (int(s.shape[s.axes.index("Y")]), int(s.shape[s.axes.index("X")]))
        out = {}
        if tif.is_ome:
            img = tifffile.xml2dict(tif.ome_metadata)["OME"]["Image"]
            px = (img[0] if isinstance(img, list) else img)["Pixels"]
            for a in "yx":
                v, unit = px.get(f"PhysicalSize{a.upper()}"), px.get(f"PhysicalSize{a.upper()}Unit", "µm")
                if v and unit in NM:
                    out[a] = float(v) * NM[unit]
        elif tif.is_imagej and tif.imagej_metadata.get("unit") in NM:
            scale, tags = NM[tif.imagej_metadata["unit"]], tif.pages[0].tags
            for a, tag in (("y", "YResolution"), ("x", "XResolution")):
                num, den = tags[tag].value if tag in tags else (0, 0)
                if num:
                    out[a] = den / num * scale
    return shape, out or None


def _outlines(io: IO, url: str) -> tuple[list[tuple[int, np.ndarray, np.ndarray]], int]:
    """Every .roi of the ROI zip, in the order of the zip's directory, as (id, xs, ys); and how many there are."""
    try:
        fh = io.open(url)
    except NeedsFetch:          # a deflated member of an outer zip, or a server without Range
        fh = open(io.fetch(url), "rb")
    with fh, zipfile.ZipFile(fh) as z:
        members = [m for m in z.infolist() if not m.is_dir() and m.filename.lower().endswith(".roi")]
        if not members:
            raise ValueError(f"{_name(url)}: the zip holds no .roi file ({len(z.infolist())} members)")
        return [(i, *_outline(z.read(m), m.filename)) for i, m in enumerate(members, 1)], len(members)


def open_array(src: Source, io: IO) -> Array:
    vertices = src.params.get("vertices", "corners")
    if vertices not in VERTICES:
        raise ValueError(f"params.vertices must be one of {VERTICES}, got {vertices!r}")
    zips = [u for u in src.urls if _name(u).lower().endswith(".zip")]
    rest = [u for u in src.urls if u not in zips]
    if len(zips) != 1 or len(rest) != 1:
        raise ValueError("urls must name one ImageJ ROI zip (a name ending in .zip) and the one TIFF it was drawn "
                         f"on, got {len(zips)} and {len(rest)} such urls")
    shape, voxel_size_nm = _grid(io, rest[0])
    outlines, count = _outlines(io, zips[0])
    for i, xs, ys in outlines:
        if xs.max() < 0 or ys.max() < 0 or xs.min() >= shape[1] or ys.min() >= shape[0]:
            raise ValueError(f"{_name(zips[0])}: ROI {i} of {count} spans x {xs.min():g}..{xs.max():g}, y "
                             f"{ys.min():g}..{ys.max():g}, outside the TIFF's {shape[0]} x {shape[1]} (y, x) grid; "
                             "is this the image the ROIs were drawn on?")
    return _Rois(outlines, count, shape, voxel_size_nm, vertices == "pixels")
