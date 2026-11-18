"""RGB photographs stored as JPEG or PNG, chosen by glob from the members of a zip, one image per z plane and
the red, green and blue samples as the c axis (`zcyx`). A file is a JPEG or a PNG by its first bytes, never by
its name: a PNG named `.jpg` is read as the PNG it is. An RGBA PNG gives its three colour samples; its alpha
sample is dropped only when it is fully opaque everywhere (so nothing is lost), otherwise the file raises.

Each url names one zip (`<zip url>`, or `<outer zip url>::<inner zip member>` for a zip nested in another);
`params.glob` picks the images inside it. An array has one shape, so every image must have the first image's
height and width: a set that mixes sizes is bound as one array per size, the files of each chosen by their file
number with `params.numbers`. Nothing is padded, cropped or resampled. `open_array` reads each zip's directory
and decodes the first image; `read` decodes the images it needs.
"""
import re
from fnmatch import fnmatchcase
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
PNG_COLOUR = {2: "RGB", 6: "RGBA"}      # the IHDR colour types read; 0 and 4 are grey, 3 is a palette


def _decode(data: bytes, name: str) -> np.ndarray:
    """The image in `data` as (Y, X, 3) RGB, told to be a JPEG or a PNG by its first bytes."""
    if data[:8] == PNG:
        # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type
        if len(data) < 26 or data[25] not in PNG_COLOUR or data[24] not in (8, 16):
            kind = f"colour type {data[25]}, {data[24]} bit" if len(data) >= 26 else "cut short"
            raise ValueError(f"{name}: a PNG that is not 8 or 16 bit RGB or RGBA ({kind}); this snippet reads "
                             "three-sample colour images only")
        a = np.asarray(imagecodecs.png_decode(data))
        if a.ndim == 3 and a.shape[2] == 4:     # RGBA, or RGB with a transparent colour (tRNS)
            opaque = np.iinfo(a.dtype).max
            if int(a[:, :, 3].min()) != opaque:
                raise ValueError(f"{name}: the PNG's alpha is not {opaque} everywhere (its minimum is "
                                 f"{int(a[:, :, 3].min())}), so it says something about the pixels; this snippet "
                                 "drops only an alpha sample that is fully opaque")
            a = a[:, :, :3]
        what = "PNG"
    elif data[:3] == JPEG:
        a = np.asarray(imagecodecs.jpeg_decode(data))
        what = "JPEG"
    else:
        raise ValueError(f"{name} is neither a JPEG nor a PNG (starts {data[:8]!r})")
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"{name}: the {what} decoded as {a.dtype} {a.shape}, not three samples (Y, X, 3); this "
                         "snippet reads RGB images only (a grey JPEG belongs to jpeg-zip-glob)")
    return a


class _Stack:
    access = "plane"            # one file per z plane: any y/x region costs decoding that whole image
    voxel_size_nm = None        # a JPEG's JFIF density and a PNG's pHYs are print sizes, not pixel sizes
    axes = "zcyx"

    def __init__(self, files: list[tuple[ZipFile, str, str]]):
        self._files = files                             # (open zip, its url, member name), one per z plane
        first = self._load(0)                           # (3, Y, X); every file must match its shape and dtype
        self._plane_shape = first.shape
        self.shape = (len(files), *first.shape)
        self.dtype = np.dtype(first.dtype)
        self._last: tuple[int, np.ndarray] = (0, first)     # the last image decoded: its c planes share it

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(a, slice(None)) for a in "cyx")      # within one plane
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
        """Image k as (3, Y, X), checked against the first image."""
        if self._last[0] != k:
            a = self._load(k)
            if a.shape != self._plane_shape or a.dtype != self.dtype:
                raise ValueError(f"{self._name(k)}: a {a.dtype} image of {a.shape[1]}x{a.shape[2]} (height x "
                                 f"width), but the first file {self._name(0)} is {self.dtype} "
                                 f"{self._plane_shape[1]}x{self._plane_shape[2]}; all files of one array must "
                                 "agree, so bind each size as its own array and choose its files with "
                                 "params.numbers")
            self._last = (k, a)
        return self._last[1]

    def _load(self, k: int) -> np.ndarray:
        data = self._files[k][0].read(self._files[k][2])    # zipfile inflates the member and checks its CRC
        return np.ascontiguousarray(np.moveaxis(_decode(data, self._name(k)), 2, 0))


def _open_zip(url: str, io: IO) -> ZipFile:
    """The zip at `url`, read by range when it can be (a plain URL, a stored member of an outer zip), else staged
    whole (a deflated member of an outer zip, a server without Range)."""
    try:
        return ZipFile(io.open(url))
    except NeedsFetch:
        return ZipFile(io.fetch(url))


def _numbers(value: object) -> set[int]:
    """A list of file numbers and inclusive runs (`7`, `"60-61"`) as the set of numbers."""
    out: set[int] = set()
    if not isinstance(value, list) or not value:
        raise ValueError(f"params.numbers must be a list of file numbers and 'a-b' runs, got {value!r}")
    for item in value:
        ok = isinstance(item, (int, str)) and not isinstance(item, bool)
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(item)) if ok else None
        if m is None or (m[2] is not None and int(m[2]) < int(m[1])):
            raise ValueError(f"params.numbers: {item!r} is not a file number or an 'a-b' run")
        run = range(int(m[1]), int(m[2] or m[1]) + 1)
        if not out.isdisjoint(run):
            raise ValueError(f"params.numbers: {item!r} repeats a number")
        out.update(run)
    return out


def _select(files: list[tuple[ZipFile, str, str]], numbers: object) -> list[tuple[ZipFile, str, str]]:
    """The files whose number `numbers` names, in the files' order. A file's number is the last run of digits in
    its name (without folders). Every number listed must be a file's, so a missing file is not passed over."""
    want = _numbers(numbers)
    by_number: dict[int, tuple[ZipFile, str, str]] = {}
    for f in files:
        digits = re.findall(r"\d+", f[2].rpartition("/")[2])
        if not digits:
            raise ValueError(f"{f[1]}::{f[2]}: no number in the file name to choose it by")
        other = by_number.setdefault(int(digits[-1]), f)
        if other is not f:
            raise ValueError(f"{f[1]}::{f[2]} and {other[1]}::{other[2]} have the same file number")
    if missing := sorted(want - set(by_number)):
        raise ValueError(f"params.numbers: no file has the number(s) {missing[:10]}"
                         f"{' ...' if len(missing) > 10 else ''}")
    return [f for n, f in by_number.items() if n in want]


def open_array(src: Source, io: IO) -> Array:
    glob, numbers, count = src.params.get("glob"), src.params.get("numbers"), src.params.get("count")
    if not isinstance(glob, str) or not glob:
        raise ValueError(f"params.glob must be a member name pattern such as 'images/*.jpg', got {glob!r}")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 1):
        raise ValueError(f"params.count must be a positive whole number, got {count!r}")
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
    if numbers is not None:
        files = _select(files, numbers)
    if count is not None and count != len(files):
        raise ValueError(f"params.count is {count}, but the urls, glob and numbers choose {len(files)} files")
    return _Stack(files)
