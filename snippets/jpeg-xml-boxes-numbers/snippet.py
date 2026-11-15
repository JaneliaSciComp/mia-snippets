"""Numbered JPEG images, or the XML box annotations beside them, chosen by glob (zip members, an HTTP directory)
or listed one by one, stacked along z, one file per plane. `numbers` / `skip` pick the files of one array by
file number, for a set whose images come in several shapes that the names do not tell apart.

An image keeps its samples: an RGB JPEG is three `c` planes. An annotation file is VOC-like XML without
`<size>`: `<annotation>` holding `<object>`s with a class in `<label>` (or `<name>`) and a `<bndbox>`. It becomes
a label plane on the grid of the JPEG of the same name, read from that JPEG's header, with every box filled: by a
per-file object id (`boxes: instance`) or by the 1-based index of its class in `classes` (`boxes: semantic`). A
file without `<object>` is an all-background plane. The kind is taken from the first file's leading bytes.
`open_array` reads the first file (and, for annotations, its JPEG) without decoding pixels; `read` gets the rest.
"""
import math
import re
from xml.etree import ElementTree

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

BOX_MODES = ("instance", "semantic")
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Box = tuple[str, int, int, int, int]        # class, xmin, ymin, xmax, ymax


def _bytes(io: IO, url: str) -> bytes:
    try:
        with io.open(url) as f:
            return f.read()
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def _orientation(exif: bytes) -> int | None:
    """The Orientation tag (0x0112) of a TIFF-structured Exif block's first IFD, or None."""
    order = {b"II": "little", b"MM": "big"}.get(exif[:2])
    if order is None or len(exif) < 8:
        return None
    ifd = int.from_bytes(exif[4:8], order)
    for k in range(int.from_bytes(exif[ifd:ifd + 2], order)):
        entry = exif[ifd + 2 + 12 * k:ifd + 14 + 12 * k]
        if len(entry) == 12 and int.from_bytes(entry[:2], order) == 0x0112:
            return int.from_bytes(entry[8:10], order)
    return None


def _jpeg_header(data: bytes, url: str) -> tuple[tuple[int, ...], int | None]:
    """((Y, X) or (Y, X, 3), Exif orientation or None) from the segments before the scan; nothing is decoded."""
    if data[:3] != b"\xff\xd8\xff":
        raise ValueError(f"{url}: not a JPEG file: header={data[:4]!r}")
    pos, orientation = 2, None
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:    # markers without a length
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00" and orientation is None:
            orientation = _orientation(data[pos + 10:pos + 2 + length])
        if marker in SOF and pos + 10 <= len(data):
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8 or ns not in (1, 3):
                raise ValueError(f"{url}: a {bits}-bit JPEG with {ns} components; this snippet reads 8-bit grey "
                                 "or 3-component JPEGs only")
            return ((ny, nx) if ns == 1 else (ny, nx, ns)), orientation
        if marker == 0xDA:                              # start of scan: no frame header came before it
            break
        pos += 2 + length
    raise ValueError(f"{url}: no JPEG start-of-frame segment found")


def _corners(bb: ElementTree.Element | None, url: str) -> tuple[int, int, int, int]:
    """xmin, ymin, xmax, ymax as whole pixels: a fractional corner is moved outwards (min down, max up)."""
    out = []
    for tag, whole in (("xmin", math.floor), ("ymin", math.floor), ("xmax", math.ceil), ("ymax", math.ceil)):
        text = bb.findtext(tag) if bb is not None else None
        try:
            value = float(text)
            out.append(whole(value))
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{url}: <bndbox><{tag}> is missing or not a number: {text!r}") from None
    return out[0], out[1], out[2], out[3]


def _boxes(data: bytes, url: str) -> list[Box]:
    """The <object> boxes of an annotation file, in file order; [] for a file without <object>."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"{url}: XML with a DOCTYPE or entity declaration is refused")
    root = ElementTree.fromstring(data)
    if root.tag != "annotation":
        raise ValueError(f"{url}: root element <{root.tag}>, not <annotation>")
    boxes = []
    for obj in root.findall("object"):
        label = obj.findtext("label")
        name = (label if label is not None else obj.findtext("name") or "").strip()
        boxes.append((name, *_corners(obj.find("bndbox"), url)))
    return boxes


def _paint(shape: tuple[int, int], boxes: list[Box], mode: str, classes: list[str], url: str) -> np.ndarray:
    """Boxes filled over pixels [ymin, ymax) x [xmin, xmax), largest first, so the smaller of two overlapping
    boxes is on top (file order breaks ties). instance: 1-based object number in the file; semantic: 1-based
    index of the object's class in `classes`. 0 is background."""
    if len(boxes) > 65535:
        raise ValueError(f"{url}: {len(boxes)} objects exceed uint16 ids")
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    order = sorted(range(len(boxes)), key=lambda i: -(boxes[i][3] - boxes[i][1]) * (boxes[i][4] - boxes[i][2]))
    for i in order:
        name, x0, y0, x1, y1 = boxes[i]
        if name not in classes:
            raise ValueError(f"{url}: object class {name!r} is not in params.classes {classes}")
        if not (0 <= x0 < x1 <= nx and 0 <= y0 < y1 <= ny):
            raise ValueError(f"{url}: box {name!r} x {x0}..{x1} y {y0}..{y1} is empty or outside the "
                             f"{nx}x{ny} image")
        out[y0:y1, x0:x1] = i + 1 if mode == "instance" else classes.index(name) + 1
    return out


class _Planes:
    """One file per z plane. Images: (Y, X) or (3, Y, X) per file, axes z[c]yx. Annotations: (Y, X), axes zyx."""

    def __init__(self, io: IO, urls: list[str], boxes: str | None, classes: list[str] | None, image_ext: str):
        self._io, self._urls, self._boxes, self._classes, self._ext = io, urls, boxes, classes, image_ext
        self._last: tuple[int, np.ndarray] | None = None    # the last decoded file: its c planes are read in turn
        head = _bytes(io, urls[0])
        self._xml = head.removeprefix(b"\xef\xbb\xbf").lstrip()[:1] == b"<"
        if self._xml:
            if boxes not in BOX_MODES or not classes:
                raise ValueError(f"{urls[0]} is an XML annotation: set params.boxes ({' or '.join(BOX_MODES)}) "
                                 "and params.classes (the class strings, in class id order)")
            _boxes(head, urls[0])                           # a file that is not an <annotation> fails at open
            self._file_shape = _jpeg_header(_bytes(io, self._image(urls[0])), self._image(urls[0]))[0][:2]
        else:
            if boxes is not None or classes is not None:
                raise ValueError(f"{urls[0]} is not XML: params.boxes and params.classes apply to annotations only")
            self._file_shape = _jpeg_header(head, urls[0])[0]
        c = self._file_shape[2:]                # () or (3,)
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape = (len(urls),) + c + self._file_shape[:2]
        self.dtype = np.dtype("uint16" if self._xml else "uint8")
        self.access = "plane"           # a y/x region costs its file: one member, staged whole when deflated
        self.voxel_size_nm = None       # JPEG densities describe print size, and the XML has none

    def _image(self, url: str) -> str:
        """The JPEG an annotation file belongs to: the same name with the image extension."""
        stem, dot, _ = url.rpartition(".")
        if not dot or "/" in url[len(stem):]:
            raise ValueError(f"{url}: an annotation file needs an extension to find its image by")
        return f"{stem}.{self._ext}"

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes if a != "z")
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._file(k)[inner] for k in range(len(self._urls))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self.shape[1:], bool)[inner].shape,
                                                            self.dtype)
        return self._file(range(len(self._urls))[z])[inner]     # IndexError when out of range, like numpy

    def _file(self, k: int) -> np.ndarray:
        """File k as (Y, X) or (3, Y, X), checked against the array's shape."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        data = _bytes(self._io, url)
        if self._xml:
            boxes = _boxes(data, url)
            image = self._image(url)
            shape, orientation = _jpeg_header(_bytes(self._io, image), image)
            shape = shape[:2]
            if boxes and orientation not in (None, 1):
                raise ValueError(f"{url}: {image} has Exif orientation {orientation}, so its stored pixels are "
                                 "turned against the picture the boxes were drawn on; this snippet cannot tell "
                                 "which of the two the box coordinates mean")
            a = _paint(shape, boxes, self._boxes, self._classes, url) if shape == self._file_shape else None
        else:
            shape, _ = _jpeg_header(data, url)              # refused before decoding when it is another shape
            a = np.asarray(imagecodecs.jpeg_decode(data)) if shape == self._file_shape else None
            if a is not None and (a.shape != shape or a.dtype != self.dtype):
                raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, its header says {shape}")
        if a is None:
            raise ValueError(f"{url}: {'the image of this file is ' if self._xml else ''}{shape}, not the first "
                             f"file's {self._file_shape}; all files of one array must agree, choose each "
                             "shape's files with params.numbers / params.skip")
        a = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _stem_key(url: str) -> tuple[str, str]:
    """A file's url without its extension, then the extension: `a-1.jpg`, `a-2.jpg` and `a-1.xml`, `a-2.xml` sort
    alike, so an image glob and an annotation glob over the same stems pair plane for plane."""
    head, sep, name = url.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    return (head + sep + stem, ext) if dot else (url, "")


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by stem; other urls
    pass through, in binding order."""
    out = [u for url in urls for u in sorted(io.expand(url), key=_stem_key)]
    if not out:
        raise ValueError("the urls match no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def _numbers(value: object, name: str) -> set[int]:
    """A list of file numbers and inclusive runs (`7`, `"60-61"`) as the set of numbers."""
    out: set[int] = set()
    if not isinstance(value, list) or not value:
        raise ValueError(f"params.{name} must be a list of file numbers and 'a-b' runs, got {value!r}")
    for item in value:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(item)) if isinstance(item, (int, str)) else None
        if m is None or isinstance(item, bool) or (m[2] is not None and int(m[2]) < int(m[1])):
            raise ValueError(f"params.{name}: {item!r} is not a file number or an 'a-b' run")
        run = range(int(m[1]), int(m[2] or m[1]) + 1)
        if not out.isdisjoint(run):
            raise ValueError(f"params.{name}: {item!r} repeats a number")
        out.update(run)
    return out


def _select(urls: list[str], params: dict) -> list[str]:
    """The files `numbers` names, or all but those `skip` names, in the urls' order. A file's number is the last
    run of digits in its name. Every number listed must be a file's, so a missing file is not passed over."""
    numbers, skip, count = params.get("numbers"), params.get("skip"), params.get("count")
    if numbers is not None and skip is not None:
        raise ValueError("params.numbers and params.skip are two ways to choose the files; set one")
    if numbers is not None or skip is not None:
        want = _numbers(numbers if numbers is not None else skip, "numbers" if numbers is not None else "skip")
        by_number: dict[int, str] = {}
        for url in urls:
            digits = re.findall(r"\d+", url.rpartition("::")[2].rpartition("/")[2])
            if not digits:
                raise ValueError(f"{url}: no number in the file name to choose it by")
            if by_number.setdefault(int(digits[-1]), url) != url:
                raise ValueError(f"{url} and {by_number[int(digits[-1])]} have the same file number")
        if missing := sorted(want - set(by_number)):
            raise ValueError(f"params.{'numbers' if numbers is not None else 'skip'}: no file has the number(s) "
                             f"{missing[:10]}{' ...' if len(missing) > 10 else ''}")
        urls = [u for n, u in by_number.items() if (n in want) == (numbers is not None)]
        if not urls:
            raise ValueError("params.skip leaves no file")
    if count is not None and (isinstance(count, bool) or count != len(urls)):
        raise ValueError(f"params.count is {count!r}, but the urls and numbers / skip choose {len(urls)} files")
    return urls


def open_array(src: Source, io: IO) -> Array:
    boxes, classes, ext = src.params.get("boxes"), src.params.get("classes"), src.params.get("image_ext", "jpg")
    if boxes is not None and boxes not in BOX_MODES:
        raise ValueError(f"params.boxes must be one of {BOX_MODES}, got {boxes!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class strings, got {classes!r}")
    if not (isinstance(ext, str) and re.fullmatch(r"\w+", ext)):
        raise ValueError(f"params.image_ext must be a file extension without the dot, got {ext!r}")
    return _Planes(io, _select(_expand(src.urls, io), src.params), boxes, classes, ext)
