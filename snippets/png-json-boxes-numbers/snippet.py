"""Numbered PNG images, or the boxes of the one COCO-style JSON that annotates them, stacked along z in
ascending file number, one image per plane.

An image array is a set of PNG files (plain urls, zip members or globs). Its samples are kept as `c`, or the ones
`samples` names. A box array is one JSON document with `images[]` (`file_name`, `width`, `height`, `id`),
`categories[]` (`id`, `name`) and `annotations[]` (`image_id`, `category_id`, `bbox`): plane k is the image with
the k-th file number, on the grid `images[]` gives it, with every box of the chosen classes filled, by a
per-image object number (`boxes: instance`) or by its category id (`boxes: semantic`). An image without
annotations is an all-background plane. `numbers` chooses the images of one array by file number in both kinds,
so a set whose images come in several shapes is bound as one array per shape and the two kinds pair by number.

The kind is taken from the first file's leading bytes. `open_array` reads the first PNG's header chunks, or the
whole JSON; `read` gets one PNG per plane, and paints boxes without any further bytes.
"""
import json
import math
import re
import struct
from io import BytesIO
from typing import BinaryIO

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

SIGNATURE = b"\x89PNG\r\n\x1a\n"
SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}      # IHDR colour type -> samples per pixel; 3 (palette) is refused
DTYPES = {8: np.dtype("uint8"), 16: np.dtype("uint16")}     # IHDR bit depth -> dtype; 1/2/4 bit is refused
MAX_CHUNKS = 64                         # chunks looked at between IHDR and the first IDAT
BOX_MODES = ("instance", "semantic")
BBOX_FORMS = ("corners", "xywh")
IMAGE_PARAMS = ("samples",)
BOX_PARAMS = ("bbox", "boxes", "classes")

Header = tuple[int, int, int, int, tuple[int, int, int] | None]     # height, width, bit depth, colour type, pHYs
Box = tuple[int, int, int, int, int]                                # category id, x0, y0, x1, y1 (far corner excl.)


def _bytes(io: IO, url: str) -> bytes:
    try:
        with io.open(url) as f:
            return f.read()
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def _png_header(f: BinaryIO, url: str) -> Header:
    """IHDR and, when one comes before the pixel data, pHYs. Nothing is decoded."""
    head = f.read(33)
    if len(head) < 33 or head[:8] != SIGNATURE or head[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={head[:8]!r}")
    nx, ny, bits, colour = struct.unpack(">IIBB", head[16:26])
    if bits not in DTYPES or colour not in SAMPLES:
        raise ValueError(f"{url}: a palette or 1/2/4-bit PNG (bit depth {bits}, colour type {colour}); the decoder "
                         "would replace the stored values, so this snippet does not read it")
    phys, pos = None, 33
    for _ in range(MAX_CHUNKS):
        f.seek(pos)
        chunk = f.read(8)
        if len(chunk) < 8 or chunk[4:] in (b"IDAT", b"IEND"):
            break
        length = int.from_bytes(chunk[:4], "big")
        if chunk[4:] == b"pHYs" and length == 9:
            body = f.read(9)
            phys = struct.unpack(">IIB", body) if len(body) == 9 else None
            break
        pos += 12 + length
    return ny, nx, bits, colour, phys


def _header(io: IO, url: str) -> Header:
    try:
        with io.open(url) as f:
            return _png_header(f, url)
    except NeedsFetch:
        return _png_header(BytesIO(io.fetch(url).read_bytes()), url)


def _says(header: Header) -> str:
    ny, nx, bits, colour, phys = header
    return f"{nx}x{ny}, {bits} bit, {SAMPLES[colour]} sample(s), pHYs {phys}"


def _nm(phys: tuple[int, int, int] | None) -> dict[str, float] | None:
    """pHYs is (pixels per unit in x, in y, unit); unit 1 is the metre, unit 0 gives an aspect ratio only."""
    if phys is None or phys[2] != 1 or phys[0] <= 0 or phys[1] <= 0:
        return None
    return {"y": 1e9 / phys[1], "x": 1e9 / phys[0]}


class _Stack:
    """One image per z plane; `_file(k)` gives plane k as (Y, X) or (C, Y, X)."""
    axes: str
    shape: tuple[int, ...]
    dtype: np.dtype
    voxel_size_nm: dict[str, float] | None
    access = "plane"            # a y/x region of an image costs its whole file: a PNG is one deflate stream

    def _file(self, k: int) -> np.ndarray:
        raise NotImplementedError

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._file(k)[inner] for k in range(self.shape[0])[z]]
            if planes:
                return np.stack(planes)
            return np.zeros((0,) + np.broadcast_to(np.zeros((), bool), self.shape[1:])[inner].shape, self.dtype)
        return self._file(range(self.shape[0])[z])[inner]       # IndexError when out of range, like numpy


class _Images(_Stack):
    """PNG files, axes z[c]yx. Every file must state what the first states: size, bit depth, samples, pHYs."""

    def __init__(self, io: IO, urls: list[str], samples: list[int] | None):
        self._io, self._urls = io, urls
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded file: its c planes are read in turn
        self._first = _header(io, urls[0])
        ny, nx, bits, colour, phys = self._first
        ns = SAMPLES[colour]
        if samples is not None and not all(s < ns for s in samples):
            raise ValueError(f"params.samples {samples} names a sample {urls[0]} does not have: it has {ns}")
        self._decoded = (ny, nx) + ((ns,) if ns > 1 else ())
        self._keep: list[int] | slice | None = None
        if samples is not None and samples != list(range(ns)):
            run = samples == list(range(samples[0], samples[-1] + 1))
            self._keep = slice(samples[0], samples[-1] + 1) if run else samples     # a run is a view, not a copy
        c = () if ns == 1 else (len(samples) if samples is not None else ns,)
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape = (len(urls),) + c + (ny, nx)
        self.dtype = DTYPES[bits]
        self.voxel_size_nm = _nm(phys)

    def _file(self, k: int) -> np.ndarray:
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        self._last = None                                   # free the previous image before the next is decoded
        url = self._urls[k]
        data = _bytes(self._io, url)
        header = _png_header(BytesIO(data), url)            # refused before decoding when it states another grid
        if header != self._first:
            raise ValueError(f"{url}: {_says(header)}, not the first file's {_says(self._first)}; the files of one "
                             "array must agree (it has one shape and one voxel size), choose each group's files "
                             "with params.numbers")
        a = np.asarray(imagecodecs.png_decode(data))
        if a.shape != self._decoded or a.dtype != self.dtype:
            raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, its header says {self.dtype} "
                             f"{self._decoded} (a tRNS chunk adds an alpha sample; this snippet does not read it)")
        if a.ndim == 3:
            a = np.moveaxis(a, 2, 0)
            a = a if self._keep is None else a[self._keep]
        self._last = (k, a)
        return a


def _int(value: object, what: str, url: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{url}: {what} is {value!r}, not a whole number")
    return value


def _corners(bbox: object, form: str, nx: int, ny: int, where: str) -> tuple[int, int, int, int]:
    """x0, y0, x1, y1 in whole pixels, far corner exclusive: a fractional corner is moved outwards (every pixel
    the box touches) and the box is cut at the image's edge. A box with no pixel inside the image raises."""
    if not (isinstance(bbox, list) and len(bbox) == 4 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in bbox)):
        raise ValueError(f"{where}: bbox {bbox!r} is not four finite numbers")
    x0, y0, x1, y1 = bbox if form == "corners" else (bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3])
    x0, y0, x1, y1 = max(math.floor(x0), 0), max(math.floor(y0), 0), min(math.ceil(x1), nx), min(math.ceil(y1), ny)
    if not (x0 < x1 and y0 < y1):
        raise ValueError(f"{where}: bbox {bbox!r} read as {form} is empty or lies outside the {nx}x{ny} image")
    return x0, y0, x1, y1


class _Boxes(_Stack):
    """The boxes of one COCO-style JSON, axes zyx, uint16. Everything is checked at open; read only paints."""

    def __init__(self, url: str, data: bytes, params: dict):
        form, self._mode, classes = params.get("bbox"), params.get("boxes"), params.get("classes")
        if form not in BBOX_FORMS or self._mode not in BOX_MODES or not classes:
            raise ValueError(f"{url} is a JSON annotation file: set params.bbox ({' or '.join(BBOX_FORMS)}), "
                             f"params.boxes ({' or '.join(BOX_MODES)}) and params.classes (the category names "
                             "painted)")
        try:
            doc = json.loads(data)
        except ValueError as e:
            raise ValueError(f"{url}: not JSON: {e}") from None
        if not (isinstance(doc, dict) and all(isinstance(doc.get(k), list) and all(
                isinstance(item, dict) for item in doc[k]) for k in ("images", "categories", "annotations"))):
            raise ValueError(f"{url}: not a COCO-style JSON object with images, categories and annotations lists")

        names: dict[int, str] = {}                          # category id -> name
        for cat in doc["categories"]:
            cid, name = _int(cat.get("id"), "a category id", url), cat.get("name")
            if not 1 <= cid <= 65535 or cid in names or not isinstance(name, str) or name in names.values():
                raise ValueError(f"{url}: category {cat!r}: ids must be distinct and in 1..65535 (0 is background, "
                                 "labels are uint16), names distinct strings")
            names[cid] = name
        if unknown := [c for c in classes if c not in names.values()]:
            raise ValueError(f"{url}: params.classes {unknown} are not categories of the file: "
                             f"{list(names.values())}")
        painted = {cid for cid, name in names.items() if name in classes}

        by_number: dict[int, tuple[int, int, int]] = {}     # file number -> image id, height, width
        ids: set[int] = set()
        for image in doc["images"]:
            name = image.get("file_name")
            if not isinstance(name, str):
                raise ValueError(f"{url}: an image without a file_name string: {image!r}")
            number, iid = _file_number(name), _int(image.get("id"), f"the id of image {name!r}", url)
            ny, nx = (_int(image.get(k), f"the {k} of image {name!r}", url) for k in ("height", "width"))
            if number in by_number or iid in ids or ny < 1 or nx < 1:
                raise ValueError(f"{url}: image {name!r} repeats a file number or an id, or has no pixels")
            by_number[number] = (iid, ny, nx)
            ids.add(iid)
        chosen = _select(by_number, params, "image listed in the JSON")
        ny, nx = by_number[chosen[0]][1:]
        if other := [n for n in chosen if by_number[n][1:] != (ny, nx)]:
            raise ValueError(f"{url}: image number {other[0]} is {by_number[other[0]][2]}x{by_number[other[0]][1]}, "
                             f"number {chosen[0]} is {nx}x{ny} ({len(other)} of the {len(chosen)} images chosen "
                             "differ); the images of one array must agree, choose each shape's with params.numbers")

        plane = {by_number[n][0]: k for k, n in enumerate(chosen)}      # image id -> z
        self._planes: list[list[Box]] = [[] for _ in chosen]            # per z, in annotations[] order
        for ann in doc["annotations"]:
            iid, cid = ann.get("image_id"), ann.get("category_id")
            if iid not in ids or cid not in names:
                raise ValueError(f"{url}: annotation {ann.get('id')!r} names image {iid!r} / category {cid!r}, "
                                 "which the file does not list")
            if iid in plane and cid in painted:
                self._planes[plane[iid]].append((cid, *_corners(
                    ann.get("bbox"), form, nx, ny, f"{url}: annotation {ann.get('id')!r}")))
        if self._mode == "instance" and max(map(len, self._planes)) > 65535:
            raise ValueError(f"{url}: more than 65535 boxes in one image exceed uint16 instance ids")
        self._last: tuple[int, np.ndarray] | None = None
        self.axes, self.shape, self.dtype, self.voxel_size_nm = "zyx", (len(chosen), ny, nx), np.dtype("uint16"), None

    def _file(self, k: int) -> np.ndarray:
        """Boxes filled largest first, so the smaller of two overlapping boxes is on top and, of two of one size,
        the later in annotations[]. instance: 1-based number among the image's painted boxes, in annotations[]
        order; semantic: the category id. 0 is background."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        self._last = None
        boxes = self._planes[k]
        out = np.zeros(self.shape[1:], self.dtype)
        for i in sorted(range(len(boxes)), key=lambda i: -(boxes[i][3] - boxes[i][1]) * (boxes[i][4] - boxes[i][2])):
            cid, x0, y0, x1, y1 = boxes[i]
            out[y0:y1, x0:x1] = i + 1 if self._mode == "instance" else cid
        self._last = (k, out)
        return out


def _file_number(path: str) -> int:
    """The last run of digits in a file's name, before its extension. The name is the last part of the path that
    ends in .png, else the last part: `.../files/350.png/content` and `dir/350.png?download=1` are both 350."""
    parts = [p for p in re.split(r"[?#]", path.rpartition("::")[2])[0].split("/") if p]
    name = next((p for p in reversed(parts) if p.lower().endswith(".png")), parts[-1] if parts else "")
    digits = re.findall(r"\d+", name.rpartition(".")[0] or name)
    if not digits:
        raise ValueError(f"{path}: no number in the file name {name!r} to order and choose it by")
    return int(digits[-1])


def _numbers(value: object) -> set[int]:
    """A list of file numbers and inclusive runs (`7`, `"60-61"`) as the set of numbers."""
    out: set[int] = set()
    if not isinstance(value, list) or not value:
        raise ValueError(f"params.numbers must be a list of file numbers and 'a-b' runs, got {value!r}")
    for item in value:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(item)) if isinstance(item, (int, str)) else None
        if m is None or isinstance(item, bool) or (m[2] is not None and int(m[2]) < int(m[1])):
            raise ValueError(f"params.numbers: {item!r} is not a file number or an 'a-b' run")
        run = range(int(m[1]), int(m[2] or m[1]) + 1)
        if not out.isdisjoint(run):
            raise ValueError(f"params.numbers: {item!r} repeats a number")
        out.update(run)
    return out


def _select(by_number: dict[int, object], params: dict, what: str) -> list[int]:
    """The file numbers of the array's planes, ascending: those `numbers` names, else all. Every number named
    must exist, so a missing file is not passed over, and `count` must be the number of planes."""
    numbers, count = params.get("numbers"), params.get("count")
    chosen = sorted(by_number)
    if numbers is not None:
        chosen = sorted(_numbers(numbers))
        if missing := [n for n in chosen if n not in by_number]:
            raise ValueError(f"params.numbers: no {what} has the number(s) {missing[:10]}"
                             f"{' ...' if len(missing) > 10 else ''}")
    if not chosen:
        raise ValueError(f"no {what} to read")
    if count is not None and (isinstance(count, bool) or count != len(chosen)):
        raise ValueError(f"params.count is {count!r}, but the urls and params.numbers choose {len(chosen)} images")
    return chosen


def open_array(src: Source, io: IO) -> Array:
    params = src.params
    urls = [u for url in src.urls for u in io.expand(url)]
    if not urls or len(set(urls)) != len(urls):
        raise ValueError("the urls match no file, or (after glob expansion) name the same file twice")
    try:
        with io.open(urls[0]) as f:
            lead = f.read(16)
    except NeedsFetch:
        lead = io.fetch(urls[0]).read_bytes()[:16]
    if lead[:8] != SIGNATURE:
        if lead.removeprefix(b"\xef\xbb\xbf").lstrip()[:1] != b"{":
            raise ValueError(f"{urls[0]}: neither a PNG file nor a JSON object: header={lead[:8]!r}")
        if len(urls) != 1 or any(params.get(k) is not None for k in IMAGE_PARAMS):
            raise ValueError(f"{urls[0]} is a JSON annotation file: it must be the array's only url, and "
                             f"params {IMAGE_PARAMS} apply to images only")
        return _Boxes(urls[0], _bytes(io, urls[0]), params)
    if any(params.get(k) is not None for k in BOX_PARAMS):
        raise ValueError(f"{urls[0]} is a PNG image: params {BOX_PARAMS} apply to a JSON annotation file only")
    samples = params.get("samples")
    if samples is not None and not (isinstance(samples, list) and samples and len(set(samples)) == len(samples)
                                    and all(isinstance(s, int) and not isinstance(s, bool) and s >= 0
                                            for s in samples)):
        raise ValueError(f"params.samples must be a list of distinct sample indices from 0, got {samples!r}")
    by_number: dict[int, str] = {}
    for url in urls:
        if by_number.setdefault(_file_number(url), url) != url:
            raise ValueError(f"{url} and {by_number[_file_number(url)]} have the same file number")
    return _Images(io, [by_number[n] for n in _select(by_number, params, "url")], samples)
