"""The images a CVAT for images 1.1 XML names (8-bit JPEG or PNG beside the XML), or the polygons it holds for
them filled on each image's grid, one image per z plane.

The urls are annotation files, not images: `<annotations>` with one `<image name=... width=... height=...>` per
picture and `<polygon label=... points="x,y;x,y;...">` children in that picture's pixel coordinates. The XML is
the index of a folder that cannot be listed: each image's url is the XML's url with its last segment replaced by
the image's `name`. The planes are the `<image>` elements in url order, then document order; `params.width` and
`params.height` keep those of one size when the sizes differ. Without `params.polygons` the array is the images
(an RGB file is three `c` planes; an opaque alpha is left out). With it the array is the label: every polygon
filled with its 1-based number in its image (`instance`) or the 1-based position of its label in
`params.classes` (`semantic`). Both forms of one binding entry pair plane for plane because they come from the
same elements. `open_array` reads the XML files (and, for images, the first image's header); the label form
never reads an image.
"""
import math
from collections import Counter
from typing import BinaryIO, NamedTuple
from xml.etree import ElementTree

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("instance", "semantic")
PNG = b"\x89PNG\r\n\x1a\n"
HEAD = 1 << 16      # bytes of an image read for its header; the whole file when a JPEG's frame header is further in
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
PNG_SAMPLES = {0: 1, 4: 1, 2: 3, 6: 3}      # colour samples by PNG colour type: grey, grey+alpha, RGB, RGBA
UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")


class _Polygon(NamedTuple):
    label: str
    xy: np.ndarray      # (n, 2) vertices as (x, y) in pixels of the image; pixel (i, j) covers x [j, j+1), y [i, i+1)
    z_order: int


class _Image(NamedTuple):
    url: str                    # the image file
    where: str                  # `<xml url>: <image name=...>`, for messages
    shape: tuple[int, int]      # (height, width) as the <image> element says
    polygons: list[_Polygon]    # in document order
    others: list[str]           # tags of the children that are not <polygon>


def _quote(name: str) -> str:
    """`name` as a url path: every byte but letters, digits and `-._~/` percent-encoded."""
    return "".join(chr(b) if b in UNRESERVED else f"%{b:02X}" for b in name.encode("utf-8"))


def _folder(xml: str) -> tuple[str, bool]:
    """(the url of the folder of the annotation file `xml`, whether names below it are percent-encoded): a plain
    url's folder, or a zip member's inside its zip, where member names are spelled as they are."""
    path = xml.find("/", xml.find("://") + 3)
    i = xml.find("::", path) if path >= 0 else -1
    if i >= 0:
        member = xml[i + 2:]
        return xml[:i + 2] + member[:member.rfind("/") + 1], False
    if "?" in xml or "#" in xml:
        raise ValueError(f"{xml}: a url with a query or fragment has no folder to find the images in")
    return xml[:xml.rfind("/") + 1], True


def _size(node: ElementTree.Element, key: str, where: str) -> int:
    try:
        value = int(node.get(key, ""))
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{where}: {key} {node.get(key)!r} is not a positive integer")
    return value


def _polygon(node: ElementTree.Element, where: str) -> _Polygon:
    label = node.get("label")
    if not label:
        raise ValueError(f"{where}: a <polygon> without a label")
    try:
        xy = np.array([[float(v) for v in point.split(",")] for point in (node.get("points") or "").split(";")],
                      np.float64)
        z_order = int(node.get("z_order", "0"))
    except ValueError:
        xy, z_order = np.zeros((0, 2)), 0
    if xy.ndim != 2 or xy.shape[1:] != (2,) or len(xy) < 3 or not np.isfinite(xy).all():
        raise ValueError(f"{where}: the points {(node.get('points') or '')[:60]!r} of a <polygon> are not "
                         "`x,y;x,y;...` with 3 vertices or more (and a whole-number z_order)")
    return _Polygon(label, xy, z_order)


def _images(io: IO, xml: str) -> list[_Image]:
    """The <image> elements of one CVAT for images XML, in document order."""
    folder, encoded = _folder(xml)
    data = io.fetch(xml).read_bytes()
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"{xml}: XML with a DOCTYPE or entity declaration is refused")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ValueError(f"{xml}: not an XML file: {e}") from None
    if root.tag != "annotations":
        raise ValueError(f"{xml}: root element <{root.tag}>, not the <annotations> of a CVAT XML")
    nodes = root.findall("image")
    if not nodes:
        raise ValueError(f"{xml}: no <image> element under <annotations>; this snippet reads CVAT for images "
                         "(a CVAT for video file holds <track> elements)")
    out: list[_Image] = []
    for node in nodes:
        name = node.get("name") or ""
        where = f"{xml}: <image name={name!r}>"
        if (not name or "\\" in name or "::" in name or any(p in ("", ".", "..") for p in name.split("/"))
                or any(ord(ch) < 32 for ch in name)):
            raise ValueError(f"{where}: the name is not a path below the XML's folder")
        polygons = [_polygon(child, where) for child in node if child.tag == "polygon"]
        if len(polygons) > 65535:
            raise ValueError(f"{where}: {len(polygons)} polygons exceed uint16 ids")
        shape = _size(node, "height", where), _size(node, "width", where)
        out.append(_Image(folder + (_quote(name) if encoded else name), where, shape, polygons,
                          [child.tag for child in node if child.tag != "polygon"]))
    return out


def _file(io: IO, url: str) -> BinaryIO:
    try:
        return io.open(url)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).open("rb")


def _bytes(io: IO, url: str, n: int = -1) -> bytes:
    with _file(io, url) as f:
        return f.read(n)


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


def _header(data: bytes, url: str) -> tuple[tuple[int, int], int, int | None] | None:
    """((height, width), colour samples 1 or 3, Exif orientation or None) of a JPEG or PNG from its first bytes,
    nothing decoded; None when `data` ends before a JPEG's frame header."""
    if data[:8] == PNG:
        if len(data) < 29 or data[12:16] != b"IHDR":
            raise ValueError(f"{url}: a PNG file without an IHDR chunk")
        nx, ny, bits, colour = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), data[24],
                                data[25])
        if bits != 8 or colour not in PNG_SAMPLES:
            raise ValueError(f"{url}: a PNG of colour type {colour} and bit depth {bits}; this snippet reads 8-bit "
                             "grey or RGB PNGs (with or without alpha) only")
        return (ny, nx), PNG_SAMPLES[colour], None
    if data[:3] != b"\xff\xd8\xff":
        raise ValueError(f"{url}: neither a JPEG nor a PNG file: header={data[:4]!r}")
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
        if pos + 2 + length > len(data):
            return None
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00" and orientation is None:
            orientation = _orientation(data[pos + 10:pos + 2 + length])
        if marker in SOF and length >= 8:
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8 or ns not in (1, 3):
                raise ValueError(f"{url}: a {bits}-bit JPEG with {ns} components; this snippet reads 8-bit grey "
                                 "or 3-component JPEGs only")
            return (ny, nx), ns, orientation
        if marker == 0xDA:                              # start of scan: no frame header came before it
            break
        pos += 2 + length
    return None


def _decode(data: bytes, url: str) -> np.ndarray:
    """A whole JPEG or PNG as (Y, X) or (Y, X, 3) uint8; a PNG's alpha sample is left out when it is 255
    everywhere and raises otherwise."""
    if data[:8] == PNG:
        a = np.asarray(imagecodecs.png_decode(data))
        if a.ndim == 3 and a.shape[2] in (2, 4):        # the last sample is alpha (stored, or made from tRNS)
            if (a[..., -1] != 255).any():
                raise ValueError(f"{url}: a PNG with an alpha sample that is not 255 everywhere; this snippet "
                                 "leaves out an opaque alpha only")
            a = a[..., 0] if a.shape[2] == 2 else a[..., :3]
    else:
        a = np.asarray(imagecodecs.jpeg_decode(data))
    if a.dtype != np.uint8 or not (a.ndim == 2 or (a.ndim == 3 and a.shape[2] == 3)):
        raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, not 8-bit grey or RGB")
    return a


def _inside(xy: np.ndarray, ny: int, nx: int) -> tuple[int, int, np.ndarray]:
    """(y0, x0, mask): the pixels whose centres lie inside the closed polygon `xy` by the even-odd rule, as a mask
    over the polygon's bounding rows and columns (cut to the image), whose first are y0 and x0."""
    y0, y1 = max(math.floor(xy[:, 1].min()), 0), min(math.ceil(xy[:, 1].max()), ny)
    x0, x1 = max(math.floor(xy[:, 0].min()), 0), min(math.ceil(xy[:, 0].max()), nx)
    if y1 <= y0 or x1 <= x0:
        return y0, x0, np.zeros((0, 0), bool)
    a, b = xy, np.roll(xy, -1, axis=0)
    yc = np.arange(y0, y1)[:, None] + 0.5                               # pixel-centre rows
    ri, ei = np.nonzero((a[:, 1] <= yc) != (b[:, 1] <= yc))             # the edges crossing each row
    xc = a[ei, 0] + (yc[ri, 0] - a[ei, 1]) * (b[ei, 0] - a[ei, 0]) / (b[ei, 1] - a[ei, 1])
    first = np.clip(np.ceil(xc - 0.5) - x0, 0, x1 - x0).astype(np.int64)    # first pixel centre right of the edge
    count = np.zeros((y1 - y0, x1 - x0 + 1), np.int32)
    np.add.at(count, (ri, first), 1)
    return y0, x0, (np.cumsum(count, axis=1)[:, :-1] & 1).astype(bool)


def _paint(image: _Image, ids: list[int]) -> np.ndarray:
    """The image's polygons filled with `ids` (one per polygon) on its grid, 0 elsewhere. Where polygons overlap
    the higher z_order is on top, as CVAT draws them; of equal z_order the smaller one, then the later one."""
    ny, nx = image.shape
    out = np.zeros(image.shape, np.uint16)
    masks = [_inside(p.xy, ny, nx) for p in image.polygons]
    for i in sorted(range(len(masks)), key=lambda i: (image.polygons[i].z_order, -int(masks[i][2].sum()))):
        y0, x0, mask = masks[i]
        out[y0:y0 + mask.shape[0], x0:x0 + mask.shape[1]][mask] = ids[i]
    return out


class _Planes:
    """One <image> element per z plane. Images: (Y, X) or (3, Y, X) per file, axes z[c]yx, uint8. Labels (`mode`
    set): (Y, X), axes zyx, uint16, from the XML alone."""

    def __init__(self, io: IO, images: list[_Image], mode: str | None, classes: list[str]):
        self._io, self._images, self._mode, self._classes = io, images, mode, classes
        self._last: tuple[int, np.ndarray] | None = None    # the last plane made: its c planes are read in turn
        self._samples = 1
        if mode is None:
            first = images[0]
            head = _bytes(io, first.url, HEAD)
            found = _header(head, first.url) or _header(_bytes(io, first.url), first.url)
            if found is None:
                raise ValueError(f"{first.url}: no JPEG start-of-frame segment found")
            self._check(first, found)
            self._samples = found[1]
        c = (3,) if self._samples == 3 else ()
        self.axes = "z" + ("c" if c else "") + "yx"
        self.shape: tuple[int, ...] = (len(images),) + c + images[0].shape
        self.dtype = np.dtype("uint8" if mode is None else "uint16")
        self.access = "plane"           # images: a region costs its file; labels: a plane is painted whole
        self.voxel_size_nm = None       # JPEG and PNG densities describe print size, and the XML has none

    def _check(self, image: _Image, header: tuple[tuple[int, int], int, int | None]) -> None:
        shape, _, orientation = header
        if shape != image.shape:
            raise ValueError(f"{image.url}: the file is {shape} (height, width), {image.where} says {image.shape}")
        if orientation not in (None, 1):
            raise ValueError(f"{image.url}: Exif orientation {orientation}, so the stored pixels are turned "
                             "against the picture a viewer shows; this snippet cannot tell which of the two the "
                             "polygons of the XML were drawn on")

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = tuple(region.get(a, slice(None)) for a in self.axes[1:])
        z = region.get("z", slice(None))
        if isinstance(z, slice):
            planes = [self._plane(k)[inner] for k in range(len(self._images))[z]]
            return np.stack(planes) if planes else np.zeros((0,) + np.zeros(self.shape[1:], bool)[inner].shape,
                                                            self.dtype)
        return self._plane(range(len(self._images))[z])[inner]     # IndexError when out of range, like numpy

    def _plane(self, k: int) -> np.ndarray:
        """Plane k as (Y, X) or (3, Y, X)."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        image = self._images[k]
        if self._mode == "instance":
            a = _paint(image, list(range(1, len(image.polygons) + 1)))
        elif self._mode == "semantic":
            a = _paint(image, [self._classes.index(p.label) + 1 for p in image.polygons])
        else:
            data = _bytes(self._io, image.url)
            header = _header(data, image.url)
            if header is None:
                raise ValueError(f"{image.url}: no JPEG start-of-frame segment found")
            self._check(image, header)          # refused before decoding when it is another shape
            a = _decode(data, image.url)
            if a.shape[:2] != image.shape or (3 if a.ndim == 3 else 1) != self._samples:
                raise ValueError(f"{image.url}: decoded as {a.shape}; the images of this array are "
                                 f"{image.shape + ((3,) if self._samples == 3 else ())} like the first")
            a = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        self._last = (k, a)
        return a


def _pick(images: list[_Image], width: int | None, height: int | None) -> list[_Image]:
    """The images of the one size `width` x `height`, or all of them when they have one size and none is asked."""
    sizes = Counter(image.shape for image in images)
    counts = ", ".join(f"{w}x{h} ({n})" for (h, w), n in sizes.most_common(8))
    if width is None:
        if len(sizes) > 1:
            raise ValueError(f"the images are of {len(sizes)} sizes; set params.width and params.height to bind "
                             f"one array per size. width x height (images): {counts}")
        return images
    picked = [image for image in images if image.shape == (height, width)]
    if not picked:
        raise ValueError(f"no <image> with width {width} and height {height}; width x height (images): {counts}")
    return picked


def open_array(src: Source, io: IO) -> Array:
    mode, classes = src.params.get("polygons"), src.params.get("classes")
    width, height = src.params.get("width"), src.params.get("height")
    if (width is None) != (height is None):
        raise ValueError("params.width and params.height select the images of one size and are set together")
    for key, value in (("width", width), ("height", height)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"params.{key} must be a positive integer, got {value!r}")
    if mode is None:
        if classes is not None:
            raise ValueError("params.classes belongs to the label form; set params.polygons "
                             f"({' or '.join(MODES)}) with it, or drop it for the images")
    else:
        if mode not in MODES:
            raise ValueError(f"params.polygons must be one of {MODES}, got {mode!r}")
        if (not isinstance(classes, list) or not classes or len(classes) > 65535 or len(set(map(str, classes)))
                != len(classes) or not all(isinstance(c, str) and c for c in classes)):
            raise ValueError("params.classes must list every label the polygons use, as distinct strings in "
                             f"class id order, got {classes!r}")
    if not src.urls:
        raise ValueError("no urls: give the CVAT annotation files")
    if len(set(src.urls)) != len(src.urls):
        raise ValueError("the urls name the same annotation file twice")
    images = [image for url in src.urls for image in _images(io, url)]
    twice = [url for url, n in Counter(image.url for image in images).items() if n > 1]
    if twice:
        raise ValueError(f"{len(twice)} image(s) named by more than one <image> element, e.g. {twice[0]}")
    images = _pick(images, width, height)
    if mode is not None:
        for image in images:
            if image.others:
                raise ValueError(f"{image.where}: holds <{image.others[0]}>; this snippet fills <polygon> "
                                 "elements only and does not leave other annotations out silently")
            unknown = sorted({p.label for p in image.polygons} - set(classes))
            if unknown:
                raise ValueError(f"{image.where}: polygon label(s) {unknown} are not in params.classes")
    return _Planes(io, images, mode, classes or [])
