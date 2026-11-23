"""A set of single-page TIFF images, or the JSON point annotations written for them one file per image, from zip
members or directory files chosen by glob (or listed one by one), stacked along z, one file per plane.

An image keeps its samples: an RGB TIFF is three `c` planes (`zcyx`), a grey one is `zyx`. An annotation file is
one JSON object whose keys are class names, each holding a list of `{"x": ..., "y": ...}` object centroids as
fractions of the image's width and height (BreCaHAD's groundTruth/<stem>.json). It becomes a label plane on the
grid of the TIFF with the same stem, whose size is read from that TIFF's header: the pixel holding a point is the
point's class id, the 1-based position of its key in `params.classes`, and every other pixel is 0. The label
form is the one with `params.classes`; its planes are the images `params.images` names, in that order, so they
pair with the image form bound with the same urls. `open_array` reads the first file's header (labels: the first
annotation file and its TIFF's header), decoding nothing; `read` gets the rest.
"""
import json
import math
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

EPS = 1e-6      # pixels: a product such as 584 / 1360 * 1360 = 583.9999999999999 is the whole number it stands for

Point = tuple[int, float, float]        # class id, x and y as fractions of the image's width and height


def _file(io: IO, url: str) -> BinaryIO:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).open("rb")


def _series(tif: tifffile.TiffFile, url: str) -> tifffile.TiffPageSeries:
    """The file's one image, refused unless it is a single page of grey (YX) or colour (YXS, SYX) samples."""
    series = tif.series
    if len(series) != 1 or len(tif.pages) != 1 or series[0].axes not in ("YX", "YXS", "SYX"):
        first = f"the first with axes {series[0].axes!r} {tuple(series[0].shape)}" if series else "none to read"
        raise ValueError(f"{url}: the TIFF has {len(tif.pages)} page(s) in {len(series)} series, {first}; this "
                         "snippet reads single-page YX or YXS (RGB) images only")
    return series[0]


def _header(io: IO, url: str) -> tuple[tuple[int, ...], np.dtype]:
    """((Y, X) or (C, Y, X), dtype) of a TIFF as `_image` returns it, from its tags; no pixel is decoded."""
    with _file(io, url) as fh, tifffile.TiffFile(fh) as tif:
        s = _series(tif, url)
        shape = tuple(int(n) for n in s.shape)
        return (shape[2:] + shape[:2] if s.axes == "YXS" else shape), np.dtype(s.dtype)


def _image(io: IO, url: str) -> np.ndarray:
    """A TIFF's pixels as stored, (Y, X) or with the samples first (C, Y, X)."""
    with _file(io, url) as fh, tifffile.TiffFile(fh) as tif:
        s = _series(tif, url)
        a = np.asarray(s.asarray())
        return np.ascontiguousarray(np.moveaxis(a, 2, 0)) if s.axes == "YXS" else a


def _points(data: bytes, classes: list[str], url: str) -> list[Point]:
    """The points of one annotation file, class by class in the order of `classes`, each class in file order."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except ValueError as e:     # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ValueError(f"{url}: not a JSON document: {e}") from None
    if not isinstance(doc, dict):
        raise ValueError(f"{url}: the JSON is a {type(doc).__name__}, not an object with one key per class")
    unknown = [k for k in doc if k not in classes]
    if unknown:
        raise ValueError(f"{url}: key(s) {unknown} are not in params.classes {classes}; every key of the file "
                         "is a class and must be listed, so no annotation is dropped unseen")
    out: list[Point] = []
    for class_id, name in enumerate(classes, 1):
        entries = doc.get(name, [])     # a class the file does not mention has no points in it
        if not isinstance(entries, list):
            raise ValueError(f"{url}: {name!r} holds a {type(entries).__name__}, not a list of points")
        for n, p in enumerate(entries):
            x, y = (p.get("x"), p.get("y")) if isinstance(p, dict) else (None, None)
            for v in (x, y):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:   # NaN fails too
                    raise ValueError(f"{url}: {name}[{n}] is {str(p)[:80]}, not an object with numbers x and y "
                                     "between 0 and 1 (fractions of the image's width and height)")
            out.append((class_id, float(x), float(y)))
    return out


def _paint(shape: tuple[int, int], points: list[Point], radius: int, dtype: np.dtype) -> np.ndarray:
    """A (Y, X) plane of class ids: a point at fractions (x, y) marks pixel (floor(y * Y), floor(x * X)), where
    pixel (i, j) covers x in [j, j + 1) and y in [i, i + 1) and a point on the right or bottom edge (1.0) is in
    the last column or row. With a radius every pixel whose centre is within it of a point's pixel is marked too,
    a pixel in reach of several points taking the nearest. Two points in one pixel: the later one is kept."""
    ny, nx = shape
    out = np.zeros(shape, dtype)
    if not points:
        return out
    ids = np.array([p[0] for p in points], dtype)
    xs = np.minimum(np.floor(np.array([p[1] for p in points]) * nx + EPS).astype(np.int64), nx - 1)
    ys = np.minimum(np.floor(np.array([p[2] for p in points]) * ny + EPS).astype(np.int64), ny - 1)
    reach = range(-radius, radius + 1)
    offsets = [(dy, dx) for dy in reach for dx in reach if dy * dy + dx * dx <= radius * radius]
    for dy, dx in sorted(offsets, key=lambda o: -(o[0] ** 2 + o[1] ** 2)):  # farthest first, so the nearest stays
        yy, xx = ys + dy, xs + dx
        ok = (yy >= 0) & (yy < ny) & (xx >= 0) & (xx < nx)
        out[yy[ok], xx[ok]] = ids[ok]
    return out


class _Planes:
    """One file per z plane. Images: (Y, X) or (C, Y, X) per file, axes z[c]yx. Labels: (Y, X), axes zyx.
    `files` is one (image url, annotation url or None) per plane; `classes` makes this the label form."""

    def __init__(self, io: IO, files: list[tuple[str, str | None]], classes: list[str] | None, radius: int):
        self._io, self._files, self._classes, self._radius = io, files, classes, radius
        self._last: tuple[int, np.ndarray] | None = None    # the last plane made: its c planes are read in turn
        image, label = files[0]
        self._file_shape, self.dtype = _header(io, image)
        if classes is not None:
            with _file(io, label) as fh:
                _points(fh.read(), classes, label)          # a file that is not such a JSON fails at open
            self._file_shape = self._file_shape[-2:]
            self.dtype = np.dtype("uint8" if len(classes) <= 255 else "uint16")
        self.axes = "z" + ("c" if len(self._file_shape) == 3 else "") + "yx"
        self.shape = (len(files),) + self._file_shape
        self.access = "plane"           # a y/x region costs its file: one member, staged whole when deflated
        self.voxel_size_nm = None       # a plain TIFF's resolution tags are a print size; the JSON has none

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._plane(k)[inner] for k in range(len(self._files))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self._file_shape, bool)[inner].shape,
                                                            self.dtype)
        return self._plane(range(len(self._files))[z])[inner]   # IndexError when out of range, like numpy

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (C, Y, X), checked against the first file's shape and dtype."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        image, label = self._files[k]
        if self._classes is None:
            a = _image(self._io, image)
            if a.shape != self._file_shape or a.dtype != self.dtype:
                raise ValueError(f"{image}: {a.dtype} {a.shape}, not the first file's {self.dtype} "
                                 f"{self._file_shape}; all images of one array must have one shape and dtype, so "
                                 "bind the files of another as a second array")
        else:
            shape = _header(self._io, image)[0][-2:]
            if shape != self._file_shape:
                raise ValueError(f"{label}: its image {image} is {shape}, not the first image's "
                                 f"{self._file_shape}; all images of one array must have one shape")
            with _file(self._io, label) as fh:
                a = _paint(shape, _points(fh.read(), self._classes, label), self._radius, self.dtype)
        self._last = (k, a)
        return a


def _expand(urls: list[str], io: IO, what: str) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) as its matching files, sorted by name as `io.expand`
    sorts them; other urls pass through. The order of the urls is kept."""
    out = [u for url in urls for u in io.expand(url)]
    if not out:
        raise ValueError(f"{what} name no file")
    if len(set(out)) != len(out):
        raise ValueError(f"{what} (after glob expansion) name the same file twice")
    return out


def _by_stem(urls: list[str], what: str) -> dict[str, str]:
    """Files by stem, the name without folders and extension, in the given order; two of one stem raise."""
    out: dict[str, str] = {}
    for url in urls:
        stem = url.rpartition("/")[2].rpartition("::")[2].rpartition(".")[0]
        if not stem:
            raise ValueError(f"{url}: {what} needs a name and an extension to be paired by")
        if stem in out:
            raise ValueError(f"{url} and {out[stem]} have the same stem; {what}s are paired by stem")
        out[stem] = url
    return out


def _pairs(urls: list[str], images: list[str], io: IO) -> list[tuple[str, str | None]]:
    """(image, annotation file) per plane: the images in the order `images` gives them, each with the annotation
    file of its stem. An image without an annotation file, or the reverse, raises."""
    labels = _by_stem(_expand(urls, io, "the urls"), "an annotation file")
    tiffs = _by_stem(_expand(images, io, "params.images"), "an image")
    if labels.keys() != tiffs.keys():
        lone = sorted(labels[k] for k in labels.keys() - tiffs.keys())
        bare = sorted(tiffs[k] for k in tiffs.keys() - labels.keys())
        raise ValueError(f"params.images and the urls do not pair one to one by stem: {len(lone)} annotation "
                         f"file(s) without an image {lone[:3]}, {len(bare)} image(s) without an annotation file "
                         f"{bare[:3]}")
    return [(tiff, labels[stem]) for stem, tiff in tiffs.items()]


def open_array(src: Source, io: IO) -> Array:
    classes, images, radius = src.params.get("classes"), src.params.get("images"), src.params.get("radius")
    if classes is None:
        if images is not None or radius is not None:
            raise ValueError("params.images and params.radius belong to the label form; set params.classes with "
                             "them, or drop them for images")
        return _Planes(io, [(u, None) for u in _expand(list(src.urls), io, "the urls")], None, 0)
    if (not isinstance(classes, list) or not 1 <= len(classes) <= 65535 or len(set(map(str, classes))) != len(classes)
            or not all(isinstance(c, str) and c for c in classes)):
        raise ValueError("params.classes must be the list of the files' class keys in id order (the first is id "
                         f"1), each once, got {classes!r}")
    if not isinstance(images, list) or not images or not all(isinstance(u, str) and u for u in images):
        raise ValueError("params.images must be the list of the urls (globs included) of the TIFFs the annotation "
                         f"files were written for, as the image array's binding lists them, got {images!r}")
    if radius is None:
        radius = 0
    if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
        raise ValueError(f"params.radius must be a whole number of pixels >= 0, got {radius!r}")
    return _Planes(io, _pairs(list(src.urls), images, io), classes, radius)
