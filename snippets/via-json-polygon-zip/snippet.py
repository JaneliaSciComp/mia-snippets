"""VGG Image Annotator (VIA) polygon annotations and the JPEG images they annotate, as zip members: either the
images themselves (`read: image`) or their polygon regions filled onto the image grid (`read: instance` or
`semantic`), one z plane per image in url order.

The urls name one VIA JSON and the JPEG images (member urls or member globs). The planes are the images, sorted
by url; with `annotated_only` only those whose VIA entry has at least one region. A raw and a label bound with
the same urls and `annotated_only` therefore have the same planes in the same order. A label pixel is filled
when its centre lies inside a polygon: with the region's 1-based number in its image (`instance`) or with the
1-based index of its `attribute` value in `classes` (`semantic`).

Every file is read whole with `io.fetch`, once: `open_array` fetches the JSON and the first image (for the JPEG
frame header: grid and sample count; nothing is decoded), an image `read` fetches and decodes the images it is
asked for, and a label `read` fetches nothing.
"""
import json
import time
from pathlib import Path
from typing import Callable, TypeVar

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

READS = ("image", "instance", "semantic")
HEAD = 1 << 16      # bytes of a JPEG searched first for its frame header; the whole file only when that is not enough
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

TRIES = 8           # times an io call is made when the server keeps answering 429 Too Many Requests
T = TypeVar("T")

Polygon = tuple[np.ndarray, np.ndarray, str | None]     # x and y of the vertices, the region's class or None


def _patient(call: Callable[[], T]) -> T:
    """`call()`, made again when the server answers 429 Too Many Requests, after the wait its Retry-After header
    asks for (1 to 60 s, 60 s when it names none), up to TRIES times in all. The harness's io retries dropped
    connections and 5xx but treats a 429 as final, and a host's request quota is shared by every job behind one
    address. Any other error is raised as it is; io keeps nothing of a failed call, so the call is simply repeated."""
    for left in range(TRIES - 1, -1, -1):
        try:
            return call()
        except Exception as e:
            response = getattr(e, "response", None)     # an HTTP status error carries the response
            if not left or getattr(response, "status_code", None) != 429:
                raise
            try:
                wait = float(response.headers.get("retry-after"))
            except (AttributeError, TypeError, ValueError):
                wait = 60.0
            time.sleep(min(max(wait, 1.0), 60.0) + 1.0)
    raise AssertionError("unreachable")


def _jpeg_frame(data: bytes) -> tuple[int, int, int, int] | None:
    """(Y, X, samples, bits per sample) from the start-of-frame segment, or None when `data` ends before it;
    nothing is decoded."""
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
        elif marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:      # markers without a length
            pos += 2
        elif marker in SOF:
            if pos + 10 > len(data):
                break
            return (int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 7:pos + 9], "big"),
                    data[pos + 9], data[pos + 4])
        elif marker == 0xDA:                            # start of scan: no frame header came before it
            break
        else:
            pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    return None


def _frame(path: Path, url: str) -> tuple[int, int, int]:
    """(Y, X, samples) of the 8-bit JPEG staged at `path`, from its header."""
    with path.open("rb") as f:
        head = f.read(HEAD)
        if head[:3] != b"\xff\xd8\xff":
            raise ValueError(f"{url} is not a JPEG (starts {head[:4]!r}); the images of a VIA JSON are read as JPEG")
        frame = _jpeg_frame(head) or _jpeg_frame(head + f.read())
    if frame is None or min(frame[:2]) <= 0:
        raise ValueError(f"{url}: no JPEG start-of-frame segment with an image size found")
    if frame[2] not in (1, 3) or frame[3] != 8:
        raise ValueError(f"{url}: a JPEG of {frame[2]} samples of {frame[3]} bits; this snippet reads 8-bit grey "
                         "and three-sample JPEGs only")
    return frame[:3]


def _entries(doc: object, url: str) -> dict[str, list[dict]]:
    """VIA file name -> its regions, from an annotation export (`{<name><size>: entry}`) or a project file
    (the same mapping under `_via_img_metadata`); VIA 1 stores an entry's regions as a mapping, VIA 2 as a list."""
    if isinstance(doc, dict) and isinstance(doc.get("_via_img_metadata"), dict):
        doc = doc["_via_img_metadata"]
    if not isinstance(doc, dict) or not doc:
        raise ValueError(f"{url}: not a VIA JSON (a mapping of image entries)")
    out: dict[str, list[dict]] = {}
    for key, e in doc.items():
        name = e.get("filename") if isinstance(e, dict) else None
        regions = e.get("regions") if isinstance(e, dict) else None
        if isinstance(regions, dict):
            regions = list(regions.values())
        if not isinstance(name, str) or not isinstance(regions, list):
            raise ValueError(f"{url}: entry {key!r} is not a VIA image entry with 'filename' and 'regions'")
        if name in out:
            raise ValueError(f"{url}: two entries for file name {name!r}")
        out[name] = regions
    return out


def _polygons(regions: list[dict], attribute: str | None, classes: list[str] | None, where: str) -> list[Polygon]:
    """The regions of one image as polygons, in file order, each checked: a polygon of 3 or more finite vertices
    whose `attribute` value (when one is asked for) is in `classes`."""
    out: list[Polygon] = []
    for i, r in enumerate(regions, 1):
        shape = r.get("shape_attributes") if isinstance(r, dict) else None
        if not isinstance(shape, dict) or shape.get("name") != "polygon":
            kind = shape.get("name") if isinstance(shape, dict) else None
            raise ValueError(f"{where} region {i}: shape {kind!r}; this snippet fills polygons only")
        try:
            x = np.asarray(shape.get("all_points_x"), dtype=np.float64)
            y = np.asarray(shape.get("all_points_y"), dtype=np.float64)
        except (TypeError, ValueError):
            x = y = np.empty(0)
        if x.ndim != 1 or x.shape != y.shape or len(x) < 3 or not (np.isfinite(x).all() and np.isfinite(y).all()):
            raise ValueError(f"{where} region {i}: all_points_x / all_points_y are not the 3 or more vertices "
                             "of a polygon")
        cls = None
        if attribute is not None:
            attrs = r.get("region_attributes")
            cls = attrs.get(attribute) if isinstance(attrs, dict) else None
            if not isinstance(cls, str) or cls not in classes:
                raise ValueError(f"{where} region {i}: region attribute {attribute!r} is {cls!r}, which is not in "
                                 f"params.classes {classes}")
        out.append((x, y, cls))
    return out


def _inside(x: np.ndarray, y: np.ndarray, ny: int, nx: int) -> tuple[int, int, np.ndarray]:
    """(row, column, mask) of the pixels of an (ny, nx) grid whose centres lie inside the closed polygon (even-odd
    rule; pixel (i, j) has its centre at x = j + 0.5, y = i + 0.5). The part outside the grid is cut off."""
    r0, r1 = (int(np.clip(v, 0, ny)) for v in (np.floor(y.min()), np.ceil(y.max())))
    c0, c1 = (int(np.clip(v, 0, nx)) for v in (np.floor(x.min()), np.ceil(x.max())))
    if r1 <= r0 or c1 <= c0:
        return r0, c0, np.zeros((0, 0), bool)
    xa, ya, xb, yb = x, y, np.roll(x, -1), np.roll(y, -1)       # edge k runs from vertex k to vertex k + 1
    yc = np.arange(r0, r1)[:, None] + 0.5                       # pixel-centre rows
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = xa + (yc - ya) * (xb - xa) / (yb - ya)             # where each edge meets each row
    rows, edges = np.nonzero((ya <= yc) != (yb <= yc))          # the edges that cross the row (never horizontal)
    first = np.clip(np.ceil(xc[rows, edges] - 0.5), c0, c1).astype(np.int64) - c0   # first centre at or right of it
    flips = np.zeros((r1 - r0, c1 - c0 + 1), np.int32)
    np.add.at(flips, (rows, first), 1)
    return r0, c0, (np.cumsum(flips[:, :-1], axis=1) % 2).astype(bool)   # inside: an odd number of edges to the left


class _Stack:
    """One plane per image, axes [z] + the plane's own ([c]yx for an image, yx for a label); a plane is made when
    it is read and the last one is kept, so the c planes and the crops of one image share it."""

    def __init__(self, n: int, plane_axes: str, plane_shape: tuple[int, ...], dtype: str,
                 plane: Callable[[int], np.ndarray]):
        self._n, self._plane_axes, self._plane_shape, self._make = n, plane_axes, plane_shape, plane
        self._last: tuple[int, np.ndarray] | None = None
        self.axes = ("z" if n > 1 else "") + plane_axes
        self.shape = ((n,) if n > 1 else ()) + plane_shape
        self.dtype = np.dtype(dtype)
        self.voxel_size_nm = None       # neither a VIA file nor a JPEG (its JFIF density is a print size) has one
        self.access = "plane"           # one file per plane: any y/x region costs that whole image or its polygons

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        cut = tuple(region.get(ax, slice(None)) for ax in self._plane_axes)
        if "z" not in self.axes:
            return self._plane(0)[cut]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(self._n)[z])[cut]      # IndexError when out of range, like numpy
        planes = [self._plane(k)[cut] for k in range(self._n)[z]]
        if planes:
            return np.stack(planes)
        return np.empty((0, *np.empty(self._plane_shape, self.dtype)[cut].shape), self.dtype)   # an empty z range

    def _plane(self, k: int) -> np.ndarray:
        if self._last is None or self._last[0] != k:
            self._last = (k, self._make(k))
        return self._last[1]


def _image(io: IO, url: str, shape: tuple[int, ...]) -> np.ndarray:
    """The decoded JPEG at `url` as (Y, X) or (3, Y, X) uint8, checked against the first image's header."""
    data = _patient(lambda: io.fetch(url)).read_bytes()     # the member whole; io checks its size and CRC
    if data[:2] != b"\xff\xd8":
        raise ValueError(f"{url} is not a JPEG (starts {data[:4]!r})")
    a = np.asarray(imagecodecs.jpeg_decode(data))
    if a.ndim == 3:
        a = np.moveaxis(a, 2, 0)            # the samples become c
    if a.shape != shape or a.dtype != np.uint8:
        raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, not the uint8 {shape} of the first image's "
                         "header; all images of one array must agree")
    return a


def _label(regions: list[dict], grid: tuple[int, int], read: str, attribute: str | None, classes: list[str] | None,
           where: str) -> np.ndarray:
    """One image's polygons on its grid, largest first, so the smaller of two overlapping regions is on top (file
    order breaks ties). 0 is background."""
    polygons = _polygons(regions, attribute, classes, where)
    if len(polygons) > 65535:
        raise ValueError(f"{where}: {len(polygons)} regions exceed uint16 ids")
    masks = [_inside(px, py, *grid) for px, py, _ in polygons]
    out = np.zeros(grid, np.uint16)
    for i in sorted(range(len(masks)), key=lambda i: -int(masks[i][2].sum())):
        r0, c0, mask = masks[i]
        value = i + 1 if read == "instance" else classes.index(polygons[i][2]) + 1
        out[r0:r0 + mask.shape[0], c0:c0 + mask.shape[1]][mask] = value
    return out


def _file_name(url: str) -> str:
    """The file name of a member url or a plain url: what follows its last '/' (or the '::' of a top-level member)."""
    return url[max(url.rfind("/"), url.rfind("::") + 1) + 1:]


def open_array(src: Source, io: IO) -> Array:
    read, attribute, classes = src.params.get("read"), src.params.get("attribute"), src.params.get("classes")
    only = src.params.get("annotated_only", False)
    if read not in READS:
        raise ValueError(f"params.read must be one of {READS}, got {read!r}")
    if (attribute is None) != (classes is None) or (read == "semantic" and classes is None):
        raise ValueError("params.attribute and params.classes go together, and params.read 'semantic' needs both")
    if read == "image" and attribute is not None:
        raise ValueError("params.attribute and params.classes describe the regions; drop them for read 'image'")
    if attribute is not None and (not isinstance(attribute, str) or not attribute):
        raise ValueError(f"params.attribute must be a region attribute name, got {attribute!r}")
    if classes is not None and not (isinstance(classes, list) and classes and len(set(classes)) == len(classes)
                                    and all(isinstance(c, str) and c for c in classes)):
        raise ValueError(f"params.classes must be a list of distinct class names, got {classes!r}")
    if not isinstance(only, bool):
        raise ValueError(f"params.annotated_only must be true or false, got {only!r}")
    urls = sorted({u for url in src.urls for u in _patient(lambda: io.expand(url))})
    jsons = [u for u in urls if u.lower().endswith(".json")]
    images = [u for u in urls if u not in jsons]
    if len(jsons) != 1 or not images:
        raise ValueError(f"the urls name {len(jsons)} .json files and {len(images)} others; one array reads one "
                         "VIA JSON and the images it annotates")
    names = [_file_name(u) for u in images]
    if len(set(names)) != len(names):
        raise ValueError("two of the image urls have the same file name; a VIA entry names its image by file name")
    try:
        entries = _entries(json.loads(_patient(lambda: io.fetch(jsons[0])).read_bytes()), jsons[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{jsons[0]} is not JSON: {e}") from None
    unannotated = [n for n in names if n not in entries]
    if unannotated:
        raise ValueError(f"{jsons[0]}: {len(unannotated)} of the {len(names)} images have no entry: "
                         f"{unannotated[:3]}")
    if only:
        images = [u for u, n in zip(images, names) if entries[n]]
        names = [n for n in names if entries[n]]
        if not images:
            raise ValueError(f"{jsons[0]}: none of the images has a region, and params.annotated_only is set")
    y, x, samples = _frame(_patient(lambda: io.fetch(images[0])), images[0])
    if read == "image":
        shape = (y, x) if samples == 1 else (samples, y, x)
        return _Stack(len(images), "yx" if samples == 1 else "cyx", shape, "uint8",
                      lambda k: _image(io, images[k], shape))
    return _Stack(len(images), "yx", (y, x), "uint16",
                  lambda k: _label(entries[names[k]], (y, x), read, attribute, classes, names[k]))
