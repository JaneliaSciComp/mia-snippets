"""The traced images of a NeuronJ folder in a zip, and their traces on the same grid.

The urls name JPEG images (zip members, by glob or one by one). NeuronJ exports each tracing of `<image>.jpg` as
`<image>.N<k>.txt` beside it, one `x<TAB>y` pixel coordinate per row along the neurite. Only images with at
least one such file are part of the array, in url order, one image per z plane; so the same urls give the
images (no `traces` param) and their traces (`traces: instance`) plane for plane.

`traces: instance` draws each tracing as a one pixel wide polyline with the value k of its file name, on the
grid the image's JPEG header declares; 0 is background. `open_array` reads the zip's directory and the first
image's header only.
"""
import re
import zipfile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

TRACES = ("instance",)
SAMPLES = ("grey",)
JPEG = b"\xff\xd8\xff"
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}     # frame headers; C4, C8 and CC are tables
TRACE = re.compile(r"(.+)\.N(\d+)\.txt")                    # <image stem>.N<k>.txt
MAX_ID = np.iinfo(np.uint16).max


def _split(url: str) -> tuple[str, str]:
    """(zip url, member) of `<zip url>::<member>`; the `::` is looked for in the path, as the harness does."""
    path = url.find("/", url.find("://") + 3)
    i = url.find("::", path) if path >= 0 else -1
    if i < 0:
        raise ValueError(f"{url}: not a zip member (<zip url>::<member>); this snippet finds an image's trace "
                         "files among the members of its zip")
    return url[:i], url[i + 2:]


def _stem(member: str) -> str:
    """The member without its extension: what NeuronJ puts `.N<k>.txt` after."""
    head, dot, ext = member.rpartition(".")
    return head if dot and "/" not in ext and not head.endswith("/") else member


def _jpeg_size(data: bytes, url: str) -> tuple[int, int, int]:
    """(height, width, samples) from the frame header of an 8-bit JPEG, without decoding it."""
    if data[:3] != JPEG:
        raise ValueError(f"{url}: not a JPEG file: header={data[:8]!r}")
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:                                   # a fill byte before the marker
            pos += 1
        elif marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:   # markers without a length
            pos += 2
        elif marker in SOF and pos + 10 <= len(data):
            height, width = (int.from_bytes(data[pos + i:pos + i + 2], "big") for i in (5, 7))
            if data[pos + 4] != 8 or not height or not width:
                raise ValueError(f"{url}: a {data[pos + 4]}-bit JPEG of {height} x {width}; this snippet reads "
                                 "8-bit JPEGs whose frame header states the size")
            return height, width, data[pos + 9]
        elif marker == 0xDA:                                 # scan data: no frame header came before it
            break
        else:
            pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    raise ValueError(f"{url}: no JPEG frame header found")


def _vertices(data: bytes, url: str) -> np.ndarray:
    """The rows of one tracing file as an (n, 2) integer array of x, y."""
    try:
        rows = [[int(v) for v in line.split()] for line in data.decode("ascii").splitlines() if line.strip()]
    except ValueError as e:
        raise ValueError(f"{url}: not rows of integer pixel coordinates: {e}") from None
    if not rows or any(len(r) != 2 for r in rows):
        raise ValueError(f"{url}: expected one or more rows of x<TAB>y, got {len(rows)} rows of "
                         f"{sorted({len(r) for r in rows})} fields")
    return np.array(rows, dtype=np.int64)


def _draw(plane: np.ndarray, pts: np.ndarray, value: int, url: str) -> None:
    """The polyline through `pts` (x, y) set to `value`: every vertex, and between two vertices the pixel
    nearest to the straight line at each step along its longer axis (8-connected, one pixel wide)."""
    (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
    if x0 < 0 or y0 < 0 or x1 >= plane.shape[1] or y1 >= plane.shape[0]:
        raise ValueError(f"{url}: coordinates x {x0}..{x1}, y {y0}..{y1} leave the image grid of "
                         f"{plane.shape[0]} x {plane.shape[1]} (y, x)")
    plane[pts[:, 1], pts[:, 0]] = value
    for (xa, ya), (xb, yb) in zip(pts[:-1].tolist(), pts[1:].tolist()):
        n = max(abs(xb - xa), abs(yb - ya))
        if n > 1:
            t = np.arange(1, n)
            plane[ya + (2 * t * (yb - ya) + n) // (2 * n), xa + (2 * t * (xb - xa) + n) // (2 * n)] = value


class _Traced:
    def __init__(self, io: IO, images: list[str], traces: list[dict[int, str]], mode: str | None,
                 samples: str | None):
        self._io, self._images, self._traces, self._mode, self._samples = io, images, traces, mode, samples
        self._zips: dict[str, zipfile.ZipFile | None] = {}
        self._last: tuple[int, np.ndarray] | None = None    # the last plane made: crops and c are read one by one
        self._size = _jpeg_size(self._bytes(images[0]), images[0])
        height, width, count = self._size
        if count not in (1, 3):
            raise ValueError(f"{images[0]}: a JPEG with {count} samples; this snippet reads grey and RGB")
        colour = mode is None and count == 3 and samples != "grey"
        self.axes = ("z" if len(images) > 1 else "") + ("cyx" if colour else "yx")
        self.shape = ((len(images),) if len(images) > 1 else ()) + ((3,) if colour else ()) + (height, width)
        self.dtype = np.dtype(np.uint8 if mode is None else np.uint16)
        self.access = "plane"           # one image, or the trace files of one image, per z plane
        self.voxel_size_nm = None       # neither a JPEG header nor a NeuronJ export states a pixel size

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes if k != "z")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._images))[sel])[idx]   # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._images))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self.shape[1:], self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        if self._last is None or self._last[0] != k:
            data = self._bytes(self._images[k])
            if _jpeg_size(data, self._images[k]) != self._size:
                raise ValueError(f"{self._images[k]}: (height, width, samples) {_jpeg_size(data, self._images[k])} "
                                 f"differs from the first image's {self._size}; all images of one array must "
                                 "agree")
            self._last = (k, self._image(data, self._images[k]) if self._mode is None else self._drawn(k))
        return self._last[1]

    def _image(self, data: bytes, url: str) -> np.ndarray:
        """The decoded JPEG as (Y, X) or (C, Y, X)."""
        a = np.asarray(imagecodecs.jpeg_decode(data))
        height, width, count = self._size
        if a.dtype != np.uint8 or a.shape != ((height, width) if count == 1 else (height, width, count)):
            raise ValueError(f"{url}: decoded to {a.dtype} {a.shape}, its header says {self._size}")
        if a.ndim == 2:
            return a
        if self._samples == "grey":
            if not np.array_equal(a, np.broadcast_to(a[:, :, :1], a.shape)):
                raise ValueError(f"{url}: samples differ, so this is not a grey image stored as RGB; drop "
                                 "params.samples to read the samples as a c axis")
            return np.ascontiguousarray(a[:, :, 0])
        return np.ascontiguousarray(np.moveaxis(a, 2, 0))

    def _drawn(self, k: int) -> np.ndarray:
        """The tracings of image k on its grid, each with the number of its file name; a later one is on top."""
        plane = np.zeros(self._size[:2], np.uint16)
        zip_url = _split(self._images[k])[0]
        for number, member in sorted(self._traces[k].items()):
            url = f"{zip_url}::{member}"
            _draw(plane, _vertices(self._member(zip_url, member), url), number, url)
        return plane

    def _bytes(self, url: str) -> bytes:
        try:
            with self._io.open(url) as fh:
                return fh.read()
        except NeedsFetch:      # a deflated member: stage it whole
            return self._io.fetch(url).read_bytes()

    def _member(self, zip_url: str, member: str) -> bytes:
        """A trace file's bytes, read from the zip by range: the trace files of one image lie side by side in
        it and are small, so they come in a block or two rather than in two requests per file (a record host
        that limits the request rate refuses the latter). A zip that cannot be read by range is read member by
        member with `io.fetch`."""
        if zip_url not in self._zips:
            try:
                self._zips[zip_url] = zipfile.ZipFile(self._io.open(zip_url))
            except NeedsFetch:
                self._zips[zip_url] = None
        zf = self._zips[zip_url]
        return zf.read(member) if zf is not None else self._io.fetch(f"{zip_url}::{member}").read_bytes()


def _trace_index(names: list[str], zip_url: str) -> dict[str, dict[int, str]]:
    """image stem -> {k: member} over the `<stem>.N<k>.txt` members of one zip."""
    index: dict[str, dict[int, str]] = {}
    for name in names:
        m = TRACE.fullmatch(name)
        if m is None:
            continue
        number, of_image = int(m.group(2)), index.setdefault(m.group(1), {})
        if not 1 <= number <= MAX_ID or number in of_image:
            raise ValueError(f"{zip_url}::{name}: tracing number {number} is outside 1..{MAX_ID} or is also that "
                             f"of {of_image.get(number)!r}")
        of_image[number] = name
    return index


def open_array(src: Source, io: IO) -> Array:
    mode, samples = src.params.get("traces"), src.params.get("samples")
    if mode is not None and mode not in TRACES:
        raise ValueError(f"params.traces must be one of {TRACES}, got {mode!r}")
    if samples is not None and (samples not in SAMPLES or mode is not None):
        raise ValueError(f"params.samples must be one of {SAMPLES} and is for the images only (no "
                         f"params.traces), got {samples!r} with traces {mode!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    indexes: dict[str, dict[str, dict[int, str]]] = {}
    images, traces = [], []
    for url in urls:
        zip_url, member = _split(url)
        if zip_url not in indexes:
            indexes[zip_url] = _trace_index(io.list(zip_url), zip_url)
        of_image = indexes[zip_url].get(_stem(member))
        if of_image:                    # an image without trace files is in neither the images nor the traces
            images.append(url)
            traces.append(of_image)
    if not images:
        raise ValueError(f"none of the {len(urls)} files has a <stem>.N<k>.txt trace file beside it")
    return _Traced(io, images, traces, mode, samples)
