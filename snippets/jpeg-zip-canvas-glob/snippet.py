"""JPEG images of differing height and width, chosen by glob from the members of a zip, one image per z plane.
An array has one shape, so every plane is `params.canvas` (height, width): an image's pixels sit unchanged in
the centre of its plane and the rest of the plane is 0. Nothing is cropped or resampled: an image larger than
the canvas raises.

Each url names one zip (`<zip url>`, or `<outer zip url>::<inner zip member>` for a zip nested in another);
`params.glob` picks the images inside it. A colour JPEG's samples become the c axis, all of them or the ones
`params.samples` names. `open_array` reads each zip's directory and decodes the first image; `read` decodes the
images it needs.
"""
from fnmatch import fnmatchcase
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SAMPLES = "rgb"     # the letters of params.samples, in the decoded image's sample order


class _Canvas:
    access = "plane"            # one file per z plane: any y/x region costs decoding that whole image
    voxel_size_nm = None        # a JPEG's JFIF density is a print size, not a pixel size

    def __init__(self, files: list[tuple[ZipFile, str, str]], canvas: tuple[int, int], samples: str | None):
        self._files = files                             # (open zip, its url, member name), one per z plane
        self._canvas = canvas
        first = self._decode(0)
        self._ndim, self._file_dtype = first.ndim, first.dtype      # every file must match both
        if first.ndim == 2:
            if samples is not None:
                raise ValueError(f"{self._name(0)} is a grey JPEG with no sample axis; drop params.samples")
            self._sel: list[int] | None = None
        else:
            self._sel = [SAMPLES.index(ch) for ch in samples or SAMPLES]
        nc = len(self._sel or [0])
        self.axes = "z" + ("c" if nc > 1 else "") + "yx"
        self._plane_shape = ((nc,) if nc > 1 else ()) + canvas
        self.shape = (len(files), *self._plane_shape)
        self.dtype = np.dtype(first.dtype)
        # the last plane built (the c planes of one file share it); a first file larger than the canvas raises here
        self._last: tuple[int, np.ndarray] = (0, self._place(0, first))

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(a, slice(None)) for a in self.axes[1:])      # ([c,] y, x) within one plane
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(len(self._files))[z])[idx]     # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._files))[z]]
        if not planes:      # an empty z range, as numpy
            return np.empty((0, *np.empty(self._plane_shape, self.dtype)[idx].shape), self.dtype)
        return np.stack(planes)

    def _name(self, k: int) -> str:
        return f"{self._files[k][1]}::{self._files[k][2]}"

    def _plane(self, k: int) -> np.ndarray:
        """Plane k: image k centred on a canvas of zeros."""
        if self._last[0] != k:
            self._last = (k, self._place(k, self._decode(k)))
        return self._last[1]

    def _place(self, k: int, img: np.ndarray) -> np.ndarray:
        """`img`, the decoded image of file k, on a canvas of zeros: (Y, X), or (C, Y, X) with the chosen samples
        as c. Raises when the image is larger than the canvas or differs from the first file in dtype or kind."""
        if img.ndim != self._ndim or img.dtype != self._file_dtype:
            kinds = {2: "grey", 3: "colour"}
            raise ValueError(f"{self._name(k)}: a {kinds[img.ndim]} {img.dtype} image, but the first file is "
                             f"{kinds[self._ndim]} {self._file_dtype}; all files of one array must agree")
        (h, w), (H, W) = img.shape[:2], self._canvas
        if h > H or w > W:
            raise ValueError(f"{self._name(k)}: the image is {h}x{w} (height x width), larger than params.canvas "
                             f"{H}x{W}; nothing is cropped, so give a canvas that holds the largest image of the "
                             "array")
        y, x = (H - h) // 2, (W - w) // 2
        plane = np.zeros(self._plane_shape, self.dtype)
        if self._sel is None:
            plane[y:y + h, x:x + w] = img
        elif len(self._sel) == 1:
            plane[y:y + h, x:x + w] = img[:, :, self._sel[0]]
        else:
            plane[:, y:y + h, x:x + w] = np.moveaxis(img[:, :, self._sel], 2, 0)
        return plane

    def _decode(self, k: int) -> np.ndarray:
        """The decoded image of file k as stored: (Y, X) or (Y, X, 3)."""
        data = self._files[k][0].read(self._files[k][2])    # zipfile inflates the member and checks its CRC
        if data[:2] != b"\xff\xd8":
            raise ValueError(f"{self._name(k)} is not a JPEG (starts {data[:4]!r})")
        a = np.asarray(imagecodecs.jpeg_decode(data))
        if a.ndim not in (2, 3) or (a.ndim == 3 and a.shape[2] != 3):
            raise ValueError(f"{self._name(k)}: decoded as {a.dtype} {a.shape}; this snippet reads grey (Y, X) "
                             "and three-sample (Y, X, 3) JPEGs only")
        return a


def _open_zip(url: str, io: IO) -> ZipFile:
    """The zip at `url`, read by range when it can be (a plain URL, a stored member of an outer zip), else staged
    whole (a deflated member of an outer zip, a server without Range)."""
    try:
        return ZipFile(io.open(url))
    except NeedsFetch:
        return ZipFile(io.fetch(url))


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def open_array(src: Source, io: IO) -> Array:
    glob, canvas, samples = src.params.get("glob"), src.params.get("canvas"), src.params.get("samples")
    if not isinstance(glob, str) or not glob:
        raise ValueError(f"params.glob must be a member name pattern such as 'crops/*.jpg', got {glob!r}")
    if not (isinstance(canvas, list) and len(canvas) == 2 and all(_whole(v) for v in canvas)):
        raise ValueError(f"params.canvas must be [height, width], two positive whole numbers, got {canvas!r}")
    if samples is not None and (not isinstance(samples, str) or not samples or len(set(samples)) != len(samples)
                                or not set(samples) <= set(SAMPLES)):
        raise ValueError(f"params.samples must be distinct letters of {SAMPLES!r} such as 'rg', got {samples!r}")
    for url in src.urls:
        if url.count("::", max(url.find("/", url.find("://") + 3), 0)) > 1:
            raise ValueError(f"{url}: a url names the zip that holds the images (at most one '::'); the pattern "
                             "for the images inside it goes in params.glob")
    urls = [u for url in src.urls for u in io.expand(url)]      # an outer `<zip>::<glob>` over several inner zips
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same zip twice")
    files: list[tuple[ZipFile, str, str]] = []
    for url in urls:
        z = _open_zip(url, io)
        names = sorted(n for n in z.namelist() if not n.endswith("/") and fnmatchcase(n, glob))
        if not names:
            raise ValueError(f"no member of {url} matches {glob!r}")
        files += [(z, url, n) for n in names]
    return _Canvas(files, (canvas[0], canvas[1]), samples)
