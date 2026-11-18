"""A YOLO segmentation image set in a zip: the JPEG images chosen by glob, one per z plane, or (with
`params.polygons`) the polygons of each image's label .txt rasterised on that image's grid.

Each url names one zip; `params.glob` picks the images inside it. The label file of `<dir>/images/<stem>.jpg`
is `<dir>/labels/<stem>.txt` (the YOLO layout): one polygon per line, `<class id> x1 y1 x2 y2 ...` with x and y
as fractions of the image width and height. `params.shape` keeps only the images of one (Y, X), for a set whose
images differ in shape. `open_array` reads each zip's directory and the first image's header (every image's
header with `shape`); `read` decodes or rasterises the planes it needs.
"""
from fnmatch import fnmatchcase
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("instance", "semantic")
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
HEAD = 1 << 16      # bytes of a JPEG inflated for its frame header before the whole member is read

File = tuple[ZipFile, str, str]     # an open zip, its url, the image member's name


def _name(f: File) -> str:
    return f"{f[1]}::{f[2]}"


def _frame(data: bytes) -> tuple[int, int, int] | None:
    """(Y, X, components) from the frame header in a JPEG's leading bytes; None when `data` holds none."""
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                                          # a fill byte before the marker
            pos += 1
        elif marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:      # markers without a length
            pos += 2
        elif marker in SOF:
            if pos + 10 > len(data):
                break
            ny, nx = int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 7:pos + 9], "big")
            return ny, nx, data[pos + 9]
        elif marker == 0xDA:                                        # start of scan: no frame header before it
            break
        else:
            pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    return None


def _header(f: File) -> tuple[int, int, int]:
    """(Y, X, components) of image `f` from its header; no pixel is decoded."""
    with f[0].open(f[2]) as fh:
        data = fh.read(HEAD)
        if data[:2] != b"\xff\xd8":
            raise ValueError(f"{_name(f)} is not a JPEG (starts {data[:4]!r})")
        frame = _frame(data) or _frame(data + fh.read())
    if frame is None or frame[2] not in (1, 3) or 0 in frame[:2]:
        raise ValueError(f"{_name(f)}: no frame header of a grey or three-component JPEG found ({frame})")
    return frame


def _label_name(image: str) -> str:
    """`<dir>/labels/<stem>.txt` for `<dir>/images/<stem>.<ext>`: the last `images` folder becomes `labels`."""
    parts = image.split("/")
    if "images" not in parts[:-1]:
        raise ValueError(f"{image!r} is not under an images/ folder, so it has no labels/<stem>.txt beside it")
    parts[len(parts) - 2 - parts[-2::-1].index("images")] = "labels"
    parts[-1] = parts[-1].rsplit(".", 1)[0] + ".txt"
    return "/".join(parts)


def _fill(xs: np.ndarray, ys: np.ndarray, shape: tuple[int, int]) -> tuple[int, int, np.ndarray]:
    """(y0, x0, mask) of the pixels whose centre the closed polygon encloses (even-odd rule); xs, ys in pixels,
    pixel (r, c) spanning [c, c + 1) x [r, r + 1)."""
    x0, y0 = int(np.floor(xs.min())), int(np.floor(ys.min()))
    x1, y1 = min(int(np.ceil(xs.max())), shape[1]), min(int(np.ceil(ys.max())), shape[0])
    py, px = np.mgrid[y0:y1, x0:x1] + 0.5
    mask = np.zeros(py.shape, bool)
    for xa, ya, xb, yb in zip(xs, ys, np.roll(xs, -1), np.roll(ys, -1)):
        if ya != yb:
            mask ^= ((ya > py) != (yb > py)) & (px < xa + (xb - xa) * (py - ya) / (yb - ya))
    return y0, x0, mask


class _Set:
    def __init__(self, files: list[File], frame: tuple[int, int, int], polygons: str | None, classes: int | None,
                 checked: bool):
        self._files, self._polygons, self._classes = files, polygons, classes
        self._yx, self._nc = frame[:2], frame[2]
        self._checked = checked         # every image's header was read at open (params.shape)
        self._last: tuple[int, np.ndarray] | None = None    # the last plane made: (Y, X[, 3]) image or (Y, X) ids
        n, nc = len(files), 1 if polygons else self._nc
        self.axes = ("z" if n > 1 else "") + ("c" if nc > 1 else "") + "yx"
        self.shape = ((n,) if n > 1 else ()) + ((nc,) if nc > 1 else ()) + self._yx
        self.dtype = np.dtype("uint16" if polygons else "uint8")
        self.voxel_size_nm = None       # a JPEG's JFIF density is a print size; YOLO label files carry none
        self.access = "plane"           # one file per plane: any y/x region costs that whole image or label file

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if "z" not in self.axes:
            return self._cut(self._plane(0), region)
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._cut(self._plane(range(len(self._files))[z]), region)   # IndexError like numpy
        planes = [self._cut(self._plane(k), region) for k in range(len(self._files))[z]]
        if planes:
            return np.stack(planes)
        blank = self._cut(np.empty(self._yx + ((3,) if "c" in self.axes else ()), self.dtype), region)
        return np.empty((0, *blank.shape), blank.dtype)       # an empty z range, as numpy

    def _cut(self, plane: np.ndarray, region: dict[str, int | slice]) -> np.ndarray:
        """The c, y, x part of `region` out of one plane (Y, X) or (Y, X, 3), as ([c,] y, x)."""
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if plane.ndim == 2:
            return plane[y, x]
        c = region.get("c", slice(None))
        if isinstance(c, slice):
            return np.moveaxis(plane[:, :, c], 2, 0)[:, y, x]
        return plane[:, :, c][y, x]

    def _plane(self, k: int) -> np.ndarray:
        if self._last is None or self._last[0] != k:
            self._last = (k, self._ids(k) if self._polygons else self._image(k))
        return self._last[1]

    def _image(self, k: int) -> np.ndarray:
        """The decoded image of file k, (Y, X) or (Y, X, 3)."""
        z, _, member = self._files[k]
        data = z.read(member)                               # zipfile inflates the member and checks its CRC
        if data[:2] != b"\xff\xd8":
            raise ValueError(f"{_name(self._files[k])} is not a JPEG (starts {data[:4]!r})")
        a = np.asarray(imagecodecs.jpeg_decode(data))
        if a.dtype != np.uint8 or a.shape != self._yx + ((3,) if self._nc == 3 else ()):
            raise ValueError(f"{_name(self._files[k])}: decoded as {a.dtype} {a.shape}, not the array's uint8 "
                             f"{self._yx} with {self._nc} sample(s); all images of one array must agree "
                             "(params.shape picks the images of one shape)")
        return a

    def _ids(self, k: int) -> np.ndarray:
        """The polygons of image k's label file on its grid, uint16 (Y, X); 0 where there is none."""
        f = self._files[k]
        if not self._checked and _header(f)[:2] != self._yx:    # the fractions are of THIS image's width and height
            raise ValueError(f"{_name(f)} is {_header(f)[:2]}, not the array's {self._yx}; all images of one "
                             "array must agree (params.shape picks the images of one shape)")
        out = np.zeros(self._yx, np.uint16)
        label = _label_name(f[2])
        try:
            text = f[0].read(label).decode("utf-8")
        except KeyError:                                        # YOLO: an image without a label file is background
            return out
        where = f"{f[1]}::{label}"
        masks: list[tuple[int, int, int, int, np.ndarray]] = []     # (-pixels, value, y0, x0, mask)
        for number, line in enumerate(text.splitlines(), 1):
            fields = line.split()
            if not fields:
                continue
            if not fields[0].isdecimal() or int(fields[0]) >= (self._classes or 0):
                raise ValueError(f"{where} line {number}: class id {fields[0]!r} is not one of the "
                                 f"{self._classes} classes 0..{(self._classes or 0) - 1} (params.classes)")
            v = np.array([float(t) for t in fields[1:]])
            if len(v) < 6 or len(v) % 2:
                raise ValueError(f"{where} line {number}: {len(v)} numbers after the class id; a polygon is three "
                                 "or more x y pairs (four numbers are a YOLO box, which this snippet does not read)")
            if not (np.isfinite(v).all() and v.min() >= 0 and v.max() <= 1):
                raise ValueError(f"{where} line {number}: coordinates outside [0, 1] ({v.min()}..{v.max()}); they "
                                 "must be fractions of the image width and height")
            if number > 65535:
                raise ValueError(f"{where}: more than 65535 lines do not fit uint16 ids")
            y0, x0, mask = _fill(v[0::2] * self._yx[1], v[1::2] * self._yx[0], self._yx)
            value = number if self._polygons == "instance" else int(fields[0]) + 1
            masks.append((-int(mask.sum()), value, y0, x0, mask))
        for _, value, y0, x0, mask in sorted(masks, key=lambda m: m[0]):    # largest first: the smaller is on top
            out[y0:y0 + mask.shape[0], x0:x0 + mask.shape[1]][mask] = value
        return out


def _open_zip(url: str, io: IO) -> ZipFile:
    """The zip at `url`, read by range when it can be (a plain URL, a stored member of an outer zip), else staged
    whole (a deflated member of an outer zip, a server without Range)."""
    try:
        return ZipFile(io.open(url))
    except NeedsFetch:
        return ZipFile(io.fetch(url))


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    glob, shape, polygons, classes = p.get("glob"), p.get("shape"), p.get("polygons"), p.get("classes")
    if not isinstance(glob, str) or not glob:
        raise ValueError(f"params.glob must be an image member pattern such as 'train/images/*.jpg', got {glob!r}")
    if shape is not None and not (isinstance(shape, list) and len(shape) == 2
                                  and all(type(n) is int and n > 0 for n in shape)):
        raise ValueError(f"params.shape must be [Y, X] in pixels, got {shape!r}")
    if polygons is not None and polygons not in MODES:
        raise ValueError(f"params.polygons must be one of {MODES}, got {polygons!r}")
    if (polygons is None) != (classes is None) or (classes is not None and (type(classes) is not int or classes < 1)):
        raise ValueError("params.classes (the number of classes, 1 or more) goes with params.polygons and only "
                         f"with it; got polygons {polygons!r}, classes {classes!r}")
    for url in src.urls:
        if url.count("::", max(url.find("/", url.find("://") + 3), 0)) > 1:
            raise ValueError(f"{url}: a url names the zip that holds the images (at most one '::'); the pattern "
                             "for the images inside it goes in params.glob")
    urls = [u for url in src.urls for u in io.expand(url)]      # an outer `<zip>::<glob>` over several inner zips
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same zip twice")
    files: list[File] = []
    for url in urls:
        z = _open_zip(url, io)
        names = sorted(n for n in z.namelist() if not n.endswith("/") and fnmatchcase(n, glob))
        if not names:
            raise ValueError(f"no member of {url} matches {glob!r}")
        files += [(z, url, n) for n in names]
    if polygons:
        for f in files:
            _label_name(f[2])           # raises for an image that is not under images/
    if shape is None:
        return _Set(files, _header(files[0]), polygons, classes, False)
    frames = [_header(f) for f in files]
    keep = [k for k, frame in enumerate(frames) if list(frame[:2]) == shape]
    if not keep:
        raise ValueError(f"none of the {len(files)} images matching {glob!r} is {shape[0]}x{shape[1]} (Y x X)")
    if len({frames[k][2] for k in keep}) > 1:
        raise ValueError(f"the {shape[0]}x{shape[1]} images matching {glob!r} mix grey and colour JPEGs")
    return _Set([files[k] for k in keep], frames[keep[0]], polygons, classes, True)
