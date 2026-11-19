"""One PNG or JPEG image at a plain URL, or the sperm table that annotates it, painted as discs on its grid.

Image form (one url): the file's pixels as stored, `yx`, or `cyx` when it has samples (an RGB file is three `c`
planes). Label form (two urls, the table and its image, in any order, told apart by their leading bytes): the
table is headerless text with one row per sperm, six numbers separated by `;`:

    tail X; tail Y; head X; head Y; minor-axis length of the head; maximum halo thickness

all in pixels of the image (X is the column, Y the row). The file names are not trusted to pair the two (the
dataset is served under opaque object ids), so the binding names both. Each row becomes a disc centred on its
head point, filled with the row's 1-based number in the file: `draw: head` gives the disc whose diameter is the
minor-axis length, `draw: halo` the one that reaches the maximum halo thickness further out. The grid is the
image's, read from its header; no pixel of the image is decoded for the label.
"""
import math

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

DRAW = ("head", "halo")
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
HEAD = 1 << 20              # the header is looked for in the first block `io` caches; a JPEG may need more
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
PNG_SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}      # colour type: grey, RGB, grey + alpha, RGBA (3, palette, is refused)

Header = tuple[tuple[int, ...], np.dtype, int | None]   # (Y, X) or (Y, X, S), dtype, Exif orientation or None
Row = tuple[float, float, float, float, float, float]   # tail x, tail y, head x, head y, minor axis, halo


def _read(io: IO, url: str, n: int = -1) -> bytes:
    """The first n bytes of a file, or all of it."""
    try:
        with io.open(url) as f:
            return f.read(n)
    except NeedsFetch:          # a server without Range: stage the whole file
        data = io.fetch(url).read_bytes()
        return data if n < 0 else data[:n]


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


def _jpeg_header(data: bytes, url: str) -> Header | None:
    """From the segments before the scan; nothing is decoded. None when `data` ends before the frame header."""
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
        if marker == 0xDA:                              # start of scan: no frame header came before it
            raise ValueError(f"{url}: no JPEG start-of-frame segment before the scan")
        if pos + 2 + length > len(data):
            return None
        if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00" and orientation is None:
            orientation = _orientation(data[pos + 10:pos + 2 + length])
        if marker in SOF:
            bits, ny, nx, ns = (data[pos + 4], int.from_bytes(data[pos + 5:pos + 7], "big"),
                                int.from_bytes(data[pos + 7:pos + 9], "big"), data[pos + 9])
            if bits != 8 or ns not in (1, 3):
                raise ValueError(f"{url}: a {bits}-bit JPEG with {ns} components; this snippet reads 8-bit grey "
                                 "or 3-component JPEGs only")
            return ((ny, nx) if ns == 1 else (ny, nx, ns)), np.dtype("uint8"), orientation
        pos += 2 + length
    if pos + 4 > len(data):
        return None
    raise ValueError(f"{url}: not a JPEG segment at byte {pos}")


def _png_header(data: bytes, url: str) -> Header:
    """From IHDR, the chunk that follows the signature."""
    if len(data) < 26 or data[12:16] != b"IHDR":
        raise ValueError(f"{url}: a PNG without an IHDR chunk after the signature")
    nx, ny, bits, colour = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), data[24], data[25]
    if colour not in PNG_SAMPLES or bits not in (8, 16):
        raise ValueError(f"{url}: a PNG of colour type {colour} with {bits} bits; a palette or 1/2/4-bit PNG is "
                         "not read, since the decoder would replace or rescale the stored values")
    ns = PNG_SAMPLES[colour]
    return ((ny, nx) if ns == 1 else (ny, nx, ns)), np.dtype("uint8" if bits == 8 else "uint16"), None


def _header(io: IO, url: str) -> Header:
    """The image's shape, dtype and Exif orientation from its leading bytes; no pixel is decoded."""
    head = _read(io, url, HEAD)
    if head[:8] == PNG:
        return _png_header(head, url)
    if head[:3] != JPEG:
        raise ValueError(f"{url}: not a PNG or JPEG file: header={head[:8]!r}")
    found = _jpeg_header(head, url)
    if found is None and len(head) == HEAD:             # metadata longer than the first block
        found = _jpeg_header(_read(io, url), url)
    if found is None:
        raise ValueError(f"{url}: the JPEG ends before its start-of-frame segment")
    return found


def _rows(data: bytes, url: str) -> list[Row]:
    """The table's rows in file order; blank lines are passed over and anything else must be six numbers."""
    try:
        text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{url}: not a text table (not UTF-8)") from None
    rows: list[Row] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            v = [float(field) for field in line.split(";")]
        except ValueError:
            v = []
        if len(v) != 6 or not all(math.isfinite(x) for x in v):
            raise ValueError(f"{url} line {n}: expected six numbers separated by ';' (tail x; tail y; head x; "
                             f"head y; minor axis; halo thickness), got {line[:80]!r}")
        if v[4] < 0 or v[5] < 0:
            raise ValueError(f"{url} line {n}: negative minor-axis length or halo thickness: {line[:80]!r}")
        rows.append((v[0], v[1], v[2], v[3], v[4], v[5]))
    if len(rows) > 65535:
        raise ValueError(f"{url}: {len(rows)} rows exceed uint16 ids")
    return rows


def _paint(shape: tuple[int, int], rows: list[Row], draw: str) -> np.ndarray:
    """One disc per row, centred on the head point (x, y) with pixel (row, column) at (column, row): the pixels
    whose centre is within the radius, and always the pixel nearest the centre. Radius: half the minor axis
    (`head`), plus the halo thickness (`halo`). The value is the row's 1-based number in the file; 0 is
    background. Largest disc first, so the smaller of two overlapping discs is on top (file order breaks ties).
    A disc is cut at the image border; the head points are inside it (checked at open)."""
    ny, nx = shape
    out = np.zeros(shape, np.uint16)
    radius = [minor / 2 + (halo if draw == "halo" else 0.0) for _, _, _, _, minor, halo in rows]
    for i in sorted(range(len(rows)), key=lambda k: -radius[k]):
        hx, hy, r = rows[i][2], rows[i][3], radius[i]
        y0, y1 = max(math.ceil(hy - r), 0), min(math.floor(hy + r), ny - 1) + 1
        x0, x1 = max(math.ceil(hx - r), 0), min(math.floor(hx + r), nx - 1) + 1
        yy, xx = np.ogrid[y0:y1, x0:x1]
        out[y0:y1, x0:x1][(yy - hy) ** 2 + (xx - hx) ** 2 <= r * r] = i + 1
        out[min(math.floor(hy + 0.5), ny - 1), min(math.floor(hx + 0.5), nx - 1)] = i + 1
    return out


class _Image:
    """The image as stored: (Y, X) as yx, (Y, X, S) as cyx."""

    def __init__(self, io: IO, url: str):
        self._io, self._url = io, url
        self._file_shape, self.dtype, _ = _header(io, url)
        c = self._file_shape[2:]
        self.axes = "cyx" if c else "yx"
        self.shape = c + self._file_shape[:2]
        self.access = "plane"           # any region costs the whole file: it is decoded in one piece
        self.voxel_size_nm = None       # JPEG and PNG densities describe print size
        self._pixels: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._pixels is None:
            data = _read(self._io, self._url)
            a = np.asarray(imagecodecs.png_decode(data) if data[:8] == PNG else imagecodecs.jpeg_decode(data))
            if a.shape != self._file_shape or a.dtype != self.dtype:
                raise ValueError(f"{self._url}: decoded as {a.dtype} {a.shape}, its header says {self.dtype} "
                                 f"{self._file_shape}")
            self._pixels = np.moveaxis(a, 2, 0) if a.ndim == 3 else a
        return self._pixels[tuple(region.get(k, slice(None)) for k in self.axes)]


class _Discs:
    """The table's rows as discs on the image's grid: yx, uint16 ids."""

    def __init__(self, io: IO, table: str, image: str, draw: str):
        self._draw = draw
        file_shape, _, orientation = _header(io, image)
        self._rows = _rows(_read(io, table), table)
        if self._rows and orientation not in (None, 1):
            raise ValueError(f"{table}: {image} has Exif orientation {orientation}, so its stored pixels are "
                             "turned against the picture the points were marked on; this snippet cannot tell "
                             "which of the two the coordinates mean")
        self.axes = "yx"
        self.shape = file_shape[:2]
        ny, nx = self.shape
        for n, (_, _, hx, hy, _, _) in enumerate(self._rows, 1):
            if not (0 <= hx < nx and 0 <= hy < ny):     # the table of another image, or another frame
                raise ValueError(f"{table} row {n}: head point x {hx} y {hy} is outside the {nx}x{ny} image "
                                 f"{image}")
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # the whole plane is painted from the table at the first read
        self.voxel_size_nm = None
        self._plane: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:
            self._plane = _paint(self.shape, self._rows, self._draw)
        return self._plane[tuple(region.get(k, slice(None)) for k in self.axes)]


def open_array(src: Source, io: IO) -> Array:
    draw = src.params.get("draw")
    urls = list(src.urls)
    if draw is None:
        if len(urls) != 1:
            raise ValueError(f"the image form reads one image url, got {len(urls)}; for the table and its image "
                             f"(two urls) set params.draw ({' or '.join(DRAW)})")
        return _Image(io, urls[0])
    if draw not in DRAW:
        raise ValueError(f"params.draw must be one of {DRAW}, got {draw!r}")
    if len(urls) != 2:
        raise ValueError(f"params.draw is for a table and the image it annotates (two urls), got {len(urls)}")
    images = [u for u in urls if (magic := _read(io, u, 8))[:8] == PNG or magic[:3] == JPEG]
    if len(images) != 1:
        raise ValueError(f"params.draw needs one table and one PNG or JPEG image; {len(images)} of the two urls "
                         "are images")
    return _Discs(io, next(u for u in urls if u != images[0]), images[0], draw)
