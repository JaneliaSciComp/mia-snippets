"""One single-plane TIFF image (a zip member or a plain URL) as yx, or cyx when it has samples or channels; or,
given that TIFF and one or more ImageJ point / multi-point .roi files, the ROI points marked on the TIFF's pixel
grid as a yx uint16 label.

No ImageJ ROI library is on the harness allowlist, so the .roi is parsed here: the 64-byte big-endian header, the
coordinate block after it (integer, and float when the ROI has sub-pixel resolution) and, through the second
header, the per-point counter block. See README.md."""
import struct

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form
LETTER = {"S": "c", "C": "c", "Y": "y", "X": "x"}       # tifffile axis letter -> czyx
ROI_TYPES = {0: "polygon", 1: "rectangle", 2: "oval", 3: "line", 4: "freeline", 5: "polyline", 6: "noRoi",
             7: "freehand", 8: "traced", 9: "angle", 10: "point"}
ROI_POINT = 10
SUB_PIXEL = 128             # header option bit: float coordinates follow the integer ones
HEADER, HEADER2 = 64, 52    # bytes of the header, and of the second header up to its counters offset
IDS = ("point", "counter")
MAX_RADIUS = 64


class _Image:
    """The one image plane of a TIFF: yx, or cyx with its samples (RGB) or channels as c."""

    def __init__(self, tif: tifffile.TiffFile, access: str, name: str):
        self._tif, self.access = tif, access
        s = tif.series[0]
        kept = [(a, n) for a, n in zip(s.axes, s.shape) if n > 1 or a in "YX"]
        letters = "".join(LETTER.get(a, "?") for a, _ in kept)
        if "?" in letters or sorted(letters) not in (["x", "y"], ["c", "x", "y"]):
            raise ValueError(f"{name}: tifffile axes {s.axes!r} {tuple(s.shape)} are not one image plane (y, x and "
                             "at most one sample or channel axis); this snippet reads single-plane TIFFs only")
        self._drop = tuple(i for i, (a, n) in enumerate(zip(s.axes, s.shape)) if n == 1 and a not in "YX")
        self._order = tuple(letters.index(a) for a in "cyx" if a in letters)    # file axis of each array axis
        self.axes = "".join(letters[i] for i in self._order)
        self.shape = tuple(kept[i][1] for i in self._order)
        self.dtype = np.dtype(s.dtype)
        self.voxel_size_nm = _voxel_size(tif)
        self._plane: np.ndarray | None = None

    def __del__(self) -> None:      # the harness never closes arrays; a staged file's handle goes with the array
        self._tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:     # the image is one plane: decoded once, whatever the region
            a = np.squeeze(self._tif.asarray(series=0), axis=self._drop)
            self._plane = np.transpose(a, self._order)
        return self._plane[tuple(region.get(a, slice(None)) for a in self.axes)]


class _Points:
    """ImageJ ROI points on a TIFF image's yx grid: 0 where no point is, else the point's id."""

    def __init__(self, image: _Image, rois: list[tuple[str, bytes]], ids: str, radius: int):
        self.axes, self.dtype, self.access = "yx", np.dtype(np.uint16), "plane"
        self.shape = tuple(n for a, n in zip(image.axes, image.shape) if a in "yx")
        self.voxel_size_nm = image.voxel_size_nm
        ny, nx = self.shape
        self.points: list[dict[str, float | int | str]] = []     # every point, in id order, for whoever asks
        for name, b in rois:
            for x, y, counter, position in _roi_points(b, name):
                row, col = int(np.floor(y)), int(np.floor(x))
                if not (0 <= row < ny and 0 <= col < nx):
                    raise ValueError(f"{name}: point x={x:g} y={y:g} lies outside the {ny}x{nx} image; the ROI "
                                     "was not drawn on this grid")
                self.points.append({"roi": name, "x": x, "y": y, "row": row, "col": col, "counter": counter,
                                    "position": position, "id": len(self.points) + 1 if ids == "point"
                                    else counter + 1})
        if not self.points:
            raise ValueError(f"the {len(rois)} ROI file(s) hold no point")
        if len(self.points) > np.iinfo(np.uint16).max:
            raise ValueError(f"{len(self.points)} points do not fit uint16 ids")
        d = np.arange(-radius, radius + 1)
        self._disc = d[:, None] ** 2 + d[None, :] ** 2 <= radius ** 2       # the pixels a point marks
        self._radius = radius
        self._plane: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:     # painted once, in id order: where two marks overlap the later point is kept
            ny, nx = self.shape
            r, plane = self._radius, np.zeros(self.shape, np.uint16)
            for p in self.points:
                y0, x0 = int(p["row"]) - r, int(p["col"]) - r
                ys, xs = slice(max(y0, 0), min(y0 + 2 * r + 1, ny)), slice(max(x0, 0), min(x0 + 2 * r + 1, nx))
                disc = self._disc[ys.start - y0:ys.stop - y0, xs.start - x0:xs.stop - x0]
                plane[ys, xs][disc] = p["id"]
            self._plane = plane
        return self._plane[region.get("y", slice(None)), region.get("x", slice(None))]


def _roi_points(b: bytes, name: str) -> list[tuple[float, float, int, int]]:
    """The points of one ImageJ point / multi-point .roi as (x, y, counter, position), in file order. x and y are
    image pixel coordinates (float when the ROI has sub-pixel resolution); counter is the 0-based index of the
    multi-point tool's counter the point was clicked with and position the 1-based stack slice it was clicked
    on, both 0 when the file has no counter block."""
    if len(b) < HEADER or b[:4] != b"Iout":
        raise ValueError(f"{name}: not an ImageJ .roi (starts with {b[:4]!r}, {len(b)} bytes)")
    version, kind = struct.unpack_from(">hB", b, 4)
    top, left, _bottom, _right, n = struct.unpack_from(">4hH", b, 8)
    options, = struct.unpack_from(">H", b, 50)
    header2, = struct.unpack_from(">i", b, 60)
    if kind != ROI_POINT:
        raise ValueError(f"{name}: ROI type {ROI_TYPES.get(kind, kind)} is not a point ROI; this snippet marks "
                         "points only")
    if n == 0:          # more than 65535 points: the count is in the 4 bytes after the short
        n, = struct.unpack_from(">i", b, 18)
    sub_pixel = bool(options & SUB_PIXEL) and version >= 222
    end = HEADER + (12 if sub_pixel else 4) * n
    if n < 0 or end > len(b):
        raise ValueError(f"{name}: {n} points need {end} bytes, the file has {len(b)}")
    if sub_pixel:
        xs = struct.unpack_from(f">{n}f", b, HEADER + 4 * n)
        ys = struct.unpack_from(f">{n}f", b, HEADER + 8 * n)
    else:               # shorts relative to the bounding box; ImageJ reads a negative one as 0
        xs = tuple(float(left + max(v, 0)) for v in struct.unpack_from(f">{n}h", b, HEADER))
        ys = tuple(float(top + max(v, 0)) for v in struct.unpack_from(f">{n}h", b, HEADER + 2 * n))
    packed = (0,) * n
    if version >= 228 and header2 > 0 and header2 + HEADER2 <= len(b):
        offset, = struct.unpack_from(">i", b, header2 + 48)
        if offset > 0:
            if offset + 4 * n > len(b):
                raise ValueError(f"{name}: the counter block of {n} points at byte {offset} passes the file's "
                                 f"{len(b)} bytes")
            packed = struct.unpack_from(f">{n}i", b, offset)
    return [(float(x), float(y), v & 0xff, v >> 8) for x, y, v in zip(xs, ys, packed)]


def _voxel_size(tif: tifffile.TiffFile) -> dict[str, float] | None:
    """From OME PhysicalSizeX/Y or ImageJ unit + X/YResolution; None when the file doesn't say."""
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
    return out or None


def _is_roi(url: str) -> bool:
    return url.split("?", 1)[0].split("#", 1)[0].lower().endswith(".roi")


def _name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _bytes(io: IO, url: str) -> bytes:
    try:
        with io.open(url) as f:
            return f.read()
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def open_array(src: Source, io: IO) -> Array:
    urls = [u for url in src.urls for u in io.expand(url)]
    rois = sorted(u for u in urls if _is_roi(u))
    rest = [u for u in urls if not _is_roi(u)]
    if len(rest) != 1:
        raise ValueError(f"urls must name exactly one TIFF besides any .roi files, got {len(rest)} such urls")
    try:
        tif, access = tifffile.TiffFile(io.open(rest[0])), "plane"
    except NeedsFetch:
        tif, access = tifffile.TiffFile(io.fetch(rest[0])), "file"
    image = _Image(tif, access, _name(rest[0]))
    ids, radius = src.params.get("ids"), src.params.get("radius")
    if not rois:
        if ids is not None or radius is not None:
            raise ValueError("params.ids and params.radius are for the label form (urls with .roi files); got one "
                             "TIFF only")
        return image
    if ids is not None and ids not in IDS:
        raise ValueError(f"params.ids must be one of {IDS}, got {ids!r}")
    if radius is not None and (isinstance(radius, bool) or not isinstance(radius, int)
                               or not 0 <= radius <= MAX_RADIUS):
        raise ValueError(f"params.radius must be an integer from 0 to {MAX_RADIUS}, got {radius!r}")
    return _Points(image, [(_name(u), _bytes(io, u)) for u in rois], ids or "point", radius or 0)
