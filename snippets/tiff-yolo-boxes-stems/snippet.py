"""A set of 2D single-page TIFF images (grey or RGB), or of the YOLO box tables beside them, chosen by glob from
zip members or an HTTP directory (or listed one by one), stacked along z, one file per plane. `stems` picks the
files of one array by name, for a set whose images come in several shapes that a glob cannot tell apart.

A YOLO file is text with one row per box, `class_id x_center y_center width height`, the four numbers as
fractions of the image width and height. It has no size of its own, so its plane is painted on the grid of the
image with the same stem (the name without its last extension), read from that image's TIFF header: by a per-file
box number (`boxes: instance`) or by the class id + 1 (`boxes: semantic`). A file without rows is an
all-background plane. The kind is taken from the first file's leading bytes (TIFF magic or not), not from its
name. `open_array` reads the first file (and, for a table, its image's header) without decoding pixels; `read`
gets the rest.

Requests are kept few, for hosts that limit them: an image is staged whole with `io.fetch`, once per job and
dataset cache, so the header a table asks for and the pixels its raw array reads are the same two requests; a
table is read by range. A step answered with HTTP 429 (a host's request limit) waits and asks again.
"""
import math
import time
from pathlib import Path
from typing import Callable, TypeVar

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
IMAGE_EXTS = ("tif", "tiff")
ATTEMPTS = 6                    # of one step that makes requests, while the host answers 429
WAITS = (10.0, 90.0)            # seconds before asking again: at least, and at most (a limit counted by the minute)
T = TypeVar("T")

Box = tuple[int, float, float, float, float]        # class id, x0, y0, x1, y1 in pixels (fractional)


def _wait(e: Exception) -> float | None:
    """Seconds to wait before a step answered with HTTP 429 (too many requests) is tried again: the answer's
    Retry-After + 1 within WAITS, the least of WAITS when it names no seconds; None for any other error. Told
    by class name, since a snippet may not import the HTTP client."""
    response = getattr(e, "response", None)
    if "HTTPStatusError" not in {c.__name__ for c in type(e).__mro__} or getattr(response, "status_code", 0) != 429:
        return None
    try:
        told = float(response.headers.get("retry-after")) + 1
    except (AttributeError, TypeError, ValueError):     # no header, or an HTTP date
        told = WAITS[0]
    return min(max(told, WAITS[0]), WAITS[1]) if math.isfinite(told) else WAITS[1]


def _retry(step: Callable[[], T]) -> T:
    """`step()`, tried up to ATTEMPTS times while the host answers 429; any other error is raised at once (the
    harness itself asks again after a dropped connection or a 5xx)."""
    for attempt in range(ATTEMPTS):
        try:
            return step()
        except Exception as e:      # BudgetExceeded, HostNotAllowed, a 404, ValueError: raised as they are
            wait = _wait(e)
            if wait is None or attempt == ATTEMPTS - 1:
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


def _is_tiff(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) in TIFF_MAGIC


def _content(io: IO, url: str) -> Path | bytes:
    """A file by what its leading bytes say: a TIFF as a local Path, staged whole; anything else as its bytes.

    The name only chooses the way there. A file named .tif / .tiff is staged at once (`io.fetch`: the member's
    local header and its data the first time, no request after that, whichever array asks). Any other file is
    opened by range (`io.open`: the local header every time, the data once per job), which also reads an empty
    stored member, a range `io.fetch` cannot ask for; a deflated one is staged."""
    if url.rpartition(".")[2].lower() not in IMAGE_EXTS:
        def by_range() -> bytes | None:
            try:
                f = io.open(url)
            except NeedsFetch:
                return None
            head = f.read(4)
            return None if head in TIFF_MAGIC else head + f.read()

        if (data := _retry(by_range)) is not None:
            return data
    path = _retry(lambda: io.fetch(url))
    return path if _is_tiff(path) else path.read_bytes()


def _tiff_series(tif: tifffile.TiffFile, url: str) -> tifffile.TiffPageSeries:
    s = tif.series[0]
    if len(tif.series) != 1 or s.axes not in ("YX", "YXS"):
        raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.series)} series; this "
                         "snippet reads single-page YX or YXS (RGB) images only")
    if tif.pages[0].photometric == tifffile.PHOTOMETRIC.PALETTE:
        raise ValueError(f"{url}: a palette (colour-mapped) TIFF; its values are colour indices, not intensities")
    return s


def _image_header(io: IO, url: str) -> tuple[tuple[int, ...], np.dtype]:
    """(Y, X) or (Y, X, S) and the dtype of a TIFF, from its header."""
    path = _content(io, url)
    if not isinstance(path, Path):
        raise ValueError(f"{url}: not a TIFF file")
    with tifffile.TiffFile(path) as tif:
        s = _tiff_series(tif, url)
        return tuple(s.shape), np.dtype(s.dtype)


def _yolo(data: bytes, shape: tuple[int, int], n_classes: int, url: str) -> list[Box]:
    """The rows of a YOLO table as boxes in pixels of a (Y, X) image, in file order; [] for a file without rows."""
    ny, nx = shape
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: neither a TIFF nor a text file: header={data[:4]!r}") from None
    boxes = []
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        try:
            if len(fields) != 5 or not fields[0].isascii() or not fields[0].isdigit():
                raise ValueError
            xc, yc, w, h = (float(f) for f in fields[1:])
        except ValueError:
            raise ValueError(f"{url} line {number}: not a YOLO row 'class_id x_center y_center width height': "
                             f"{line[:80]!r}") from None
        cls = int(fields[0])
        if cls >= n_classes:
            raise ValueError(f"{url} line {number}: class id {cls} is not in params.classes ({n_classes} names)")
        if not (all(math.isfinite(v) for v in (xc, yc, w, h)) and w > 0 and h > 0
                and -1e-6 <= xc - w / 2 and xc + w / 2 <= 1 + 1e-6 and -1e-6 <= yc - h / 2 and yc + h / 2 <= 1 + 1e-6):
            raise ValueError(f"{url} line {number}: box centre {xc}, {yc} size {w}, {h} is empty or not inside "
                             "the image (fractions of its width and height, 0..1)")
        boxes.append((cls, (xc - w / 2) * nx, (yc - h / 2) * ny, (xc + w / 2) * nx, (yc + h / 2) * ny))
    return boxes


def _span(lo: float, hi: float, n: int) -> tuple[int, int]:
    """The pixels [a, b) of an axis of n pixels whose centres lie in [lo, hi): both edges rounded to the nearest
    pixel boundary. A box thinner than that keeps the one pixel its middle lies in."""
    a, b = min(max(math.floor(lo + 0.5), 0), n), min(max(math.floor(hi + 0.5), 0), n)
    if a >= b:
        a = min(max(math.floor((lo + hi) / 2), 0), n - 1)
        b = a + 1
    return a, b


def _paint(shape: tuple[int, int], boxes: list[Box], mode: str, url: str) -> np.ndarray:
    """Boxes filled, largest first, so the smaller of two overlapping boxes is on top (file order breaks ties).
    instance: 1-based row number among the file's boxes; semantic: class id + 1. 0 is background."""
    if len(boxes) > 65535:
        raise ValueError(f"{url}: {len(boxes)} boxes exceed uint16 ids")
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    order = sorted(range(len(boxes)), key=lambda i: -(boxes[i][3] - boxes[i][1]) * (boxes[i][4] - boxes[i][2]))
    for i in order:
        cls, x0, y0, x1, y1 = boxes[i]
        (xa, xb), (ya, yb) = _span(x0, x1, nx), _span(y0, y1, ny)
        out[ya:yb, xa:xb] = i + 1 if mode == "instance" else cls + 1
    return out


def _stem_key(url: str) -> tuple[str, str]:
    """A file's url without its last extension, then the extension: `a.tiff`, `b.tiff.tif` and `a.txt`,
    `b.tiff.txt` sort alike, so an image glob and a table glob over the same stems pair plane for plane."""
    head, sep, name = url.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    return (head + sep + stem, ext) if dot else (url, "")


def _stem(url: str) -> str:
    """The file name without its directory and its last extension."""
    return _stem_key(url)[0].rpartition("::")[2].rpartition("/")[2]


class _Planes:
    """One file per z plane. Images: (Y, X) or (S, Y, X) per file, axes z[c]yx. Tables: (Y, X) per file, zyx."""

    def __init__(self, io: IO, urls: list[str], boxes: str | None, classes: list[str] | None):
        self._io, self._urls, self._boxes, self._classes = io, urls, boxes, classes
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded file: its c planes are read in turn
        self._listed: dict[str, list[str]] = {}             # container url -> its names, for finding images
        first = _content(io, urls[0])
        self._table = not isinstance(first, Path)
        if self._table:
            if boxes not in BOX_MODES or not classes:
                raise ValueError(f"{urls[0]} is not a TIFF, so it is read as a YOLO box table: set params.boxes "
                                 f"({' or '.join(BOX_MODES)}) and params.classes (the names, in class id order)")
            shape, _ = _image_header(io, self._image(urls[0]))
            self._file_shape = shape[:2]
            _yolo(first, self._file_shape, len(classes), urls[0])      # a bad first table fails at open
            self._file_dtype = np.dtype("uint16")
        else:
            if boxes is not None or classes is not None:
                raise ValueError(f"{urls[0]} is a TIFF image: params.boxes and params.classes apply to YOLO "
                                 "tables only")
            self._file_shape, self._file_dtype = _image_header(io, urls[0])
        c = self._file_shape[2:]                # () or (S,)
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape = (len(urls),) + c + self._file_shape[:2]
        self.dtype = self._file_dtype
        self.access = "plane"           # a y/x region costs its file
        self.voxel_size_nm = None       # TIFF resolution tags describe print size; a YOLO table has none

    def _image(self, url: str) -> str:
        """The image a table belongs to: the one TIFF (.tif / .tiff) beside it that has its stem."""
        zip_url, sep, member = url.rpartition("::")
        folder, name = (member if sep else url).rpartition("/")[::2]
        container = zip_url if sep else url[:len(url) - len(name)]
        if container not in self._listed:
            self._listed[container] = _retry(lambda: self._io.list(container))
        stem = name.rpartition(".")[0]
        prefix = folder + "/" if sep and folder else ""
        found = [n for n in self._listed[container]
                 if n.startswith(prefix) and "/" not in n[len(prefix):] and n[len(prefix):].rpartition(".")[0] == stem
                 and n.rpartition(".")[2].lower() in IMAGE_EXTS]
        if not stem or len(found) != 1:
            raise ValueError(f"{url}: a YOLO table takes its grid from the one .tif / .tiff file beside it named "
                             f"{stem!r} + extension; found {found}")
        return f"{zip_url}::{found[0]}" if sep else container + found[0]

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._file(k)[inner] for k in range(len(self._urls))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self.shape[1:], bool)[inner].shape,
                                                            self.dtype)
        return self._file(range(len(self._urls))[z])[inner]     # IndexError when out of range, like numpy

    def _file(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (S, Y, X), checked against the first file."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        if self._table:
            image = self._image(url)
            shape = _image_header(self._io, image)[0][:2]
            data = _content(self._io, url)
            if isinstance(data, Path):
                raise ValueError(f"{url} is a TIFF among YOLO tables; one array holds images or tables, not both")
            a = (_paint(shape, _yolo(data, shape, len(self._classes), url), self._boxes, url)
                 if shape == self._file_shape else None)
        else:
            path = _content(self._io, url)
            if not isinstance(path, Path):
                raise ValueError(f"{url} is not a TIFF among TIFF images; one array holds images or tables, "
                                 "not both")
            with tifffile.TiffFile(path) as tif:
                s = _tiff_series(tif, url)      # another shape is refused before its pixels are read
                shape = tuple(s.shape)
                a = np.asarray(s.asarray()) if shape == self._file_shape and s.dtype == self._file_dtype else None
        if a is None:
            raise ValueError(f"{url}: {'the image of this table is ' if self._table else ''}{shape}, not the "
                             f"first file's {self._file_dtype} {self._file_shape}; all files of one array must "
                             "agree, choose each shape's files with params.stems")
        a = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, other urls stand for
    themselves; all of them together are sorted by stem (the url without its last extension), whatever the order
    of the urls: two image globs (`*.tiff`, `*.tif`) then pair plane for plane with one table glob (`*.txt`)."""
    out = sorted((u for url in urls for u in _retry(lambda: io.expand(url))), key=_stem_key)
    if not out:
        raise ValueError("the urls match no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def _select(urls: list[str], stems: object) -> list[str]:
    """The files whose stem is listed, in the urls' order. Every stem listed must be exactly one file's, so a
    missing file is not passed over and z cannot shift."""
    if stems is None:
        return urls
    if not (isinstance(stems, list) and stems and all(isinstance(s, str) and s for s in stems)
            and len(set(stems)) == len(stems)):
        raise ValueError("params.stems must be a list of distinct file stems (names without directory and last "
                         f"extension), got {str(stems)[:200]}")
    want = set(stems)
    chosen = [u for u in urls if _stem(u) in want]
    got = [_stem(u) for u in chosen]
    if missing := sorted(want - set(got)):
        raise ValueError(f"params.stems: no file has the stem(s) {missing[:5]}{' ...' if len(missing) > 5 else ''}")
    if len(set(got)) != len(got):
        twice = sorted(s for s in set(got) if got.count(s) > 1)
        raise ValueError(f"params.stems: more than one file has the stem(s) {twice[:5]}; narrow the urls")
    return chosen


def open_array(src: Source, io: IO) -> Array:
    boxes, classes = src.params.get("boxes"), src.params.get("classes")
    if boxes is not None and boxes not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {boxes!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    return _Planes(io, _select(_expand(src.urls, io), src.params.get("stems")), boxes, classes)
