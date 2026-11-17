"""One image (JPEG, or a single-page TIFF) at a plain URL, or a tab-separated points table painted on that
image's grid.

The table is the `terms.txt` of an eHistology (Kaufman Atlas) DataShare item: a header line
`Plate, Image, MK #, EMAP:ID, X, Y, ...`, a rule of `=`, then one row per flag. A raw is bound with the image's
url alone. A label is bound with two urls, the table and the image it annotates (told apart by their first
bytes): the label is `yx` uint16 on the image's grid, 0 where there is no flag and the flag's `MK #` (the
annotation number printed on the atlas plate) on the pixels the flag covers. `scale` says how many image pixels
one table pixel is.

`open_array` reads the image's header (and, for a label, the table); pixels are decoded at the first `read`.
"""
from typing import BinaryIO

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

JPEG = b"\xff\xd8\xff"
TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
# JPEG start-of-frame markers: SOF0..SOF15 without DHT (C4), JPG (C8) and DAC (CC)
SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
COLUMNS = ["Plate", "Image", "MK #", "EMAP:ID", "X", "Y"]     # the table's first six columns, as its header names them

Flag = tuple[int, int, int]     # MK #, X, Y as the table gives them


def _open(io: IO, url: str) -> BinaryIO:
    try:
        return io.open(url)
    except NeedsFetch:          # a server without Range or Content-Length: stage the whole file
        return io.fetch(url).open("rb")


def _jpeg_shape(fh: BinaryIO, url: str) -> tuple[int, ...]:
    """(Y, X) or (Y, X, 3) from the frame header, walking the segments before it; nothing is decoded."""
    pos = 2
    while True:
        fh.seek(pos)
        head = fh.read(4)
        if len(head) < 4 or head[0] != 0xFF:
            raise ValueError(f"{url}: no JPEG frame header before byte {pos}")
        marker, length = head[1], int.from_bytes(head[2:4], "big")
        if marker == 0xFF:                              # a fill byte before the marker
            pos += 1
        elif marker in SOF:
            frame = fh.read(6)
            if len(frame) < 6:
                raise ValueError(f"{url}: the JPEG frame header at byte {pos} is cut short")
            bits, y, x, n = frame[0], int.from_bytes(frame[1:3], "big"), int.from_bytes(frame[3:5], "big"), frame[5]
            if bits != 8 or n not in (1, 3) or not (y and x):
                raise ValueError(f"{url}: a {bits}-bit JPEG of {n} components, {x} x {y}; this snippet reads "
                                 "8-bit grey or 3-component JPEGs")
            return (y, x) if n == 1 else (y, x, 3)
        elif marker == 0xDA or length < 2:              # the scan began without a frame header
            raise ValueError(f"{url}: no JPEG frame header before byte {pos}")
        else:
            pos += 2 + length


def _image_shape(io: IO, url: str) -> tuple[str, tuple[int, ...], np.dtype]:
    """(format, (Y, X) or (Y, X, S), dtype) of the image at url, from its header."""
    with _open(io, url) as fh:
        magic = fh.read(4)
        if magic[:3] == JPEG:
            return "jpeg", _jpeg_shape(fh, url), np.dtype(np.uint8)
        if magic not in TIFF:
            raise ValueError(f"{url}: not a JPEG or TIFF file: header={magic!r}")
        fh.seek(0)
        with tifffile.TiffFile(fh) as tif:
            s = tif.series[0]
            if len(tif.series) != 1 or s.axes not in ("YX", "YXS"):
                raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)} in {len(tif.series)} series; "
                                 "this snippet reads one single-page YX or YXS (RGB) image")
            return "tiff", tuple(s.shape), np.dtype(s.dtype)


def _is_image(io: IO, url: str) -> bool:
    with _open(io, url) as fh:
        magic = fh.read(4)
    return magic[:3] == JPEG or magic in TIFF


class _Image:
    """The image as (Y, X) or (C, Y, X); the file is fetched and decoded whole at the first read, and kept."""

    def __init__(self, io: IO, url: str):
        self._io, self._url = io, url
        self._format, self._stored, self.dtype = _image_shape(io, url)      # stored (Y, X[, S])
        self.axes = "cyx" if len(self._stored) == 3 else "yx"
        self.shape = self._stored[2:] + self._stored[:2]
        self.voxel_size_nm = None       # JFIF density and TIFF resolution describe print size
        self.access = "plane"           # any region costs the whole file
        self._pixels: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._pixels is None:
            self._pixels = self._decode()
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if self.axes == "yx":
            return self._pixels[y, x]
        c = region.get("c", slice(None))
        out = self._pixels[y, x, c]                     # stored order: samples last
        return np.moveaxis(out, -1, 0) if isinstance(c, slice) else out

    def _decode(self) -> np.ndarray:
        with _open(self._io, self._url) as fh:
            if self._format == "tiff":
                with tifffile.TiffFile(fh) as tif:
                    a = tif.series[0].asarray()
            else:
                data = fh.read()
                # libjpeg decodes a file cut short without an error, grey below the cut
                if data.rstrip(b"\x00")[-2:] != b"\xff\xd9":
                    raise ValueError(f"{self._url}: the {len(data)} bytes read do not end with the JPEG "
                                     "end-of-image marker; the file is cut short")
                a = imagecodecs.jpeg_decode(data)
        a = np.asarray(a)
        if a.shape != self._stored or a.dtype != self.dtype:
            raise ValueError(f"{self._url}: decoded to {a.dtype} {a.shape}, the header says "
                             f"{self.dtype} {self._stored}")
        return a


def _flags(text: str, url: str) -> list[Flag]:
    """(MK #, X, Y) of every row of the table, in file order."""
    lines = [(n, line) for n, line in enumerate(text.splitlines(), 1) if line.strip()]
    header = [f.strip() for f in lines[0][1].split("\t")] if lines else []
    if header[:len(COLUMNS)] != COLUMNS:
        raise ValueError(f"{url}: not a points table: its first line names the columns {header[:len(COLUMNS)]}, "
                         f"not {COLUMNS}")
    flags = []
    for n, line in lines[1:]:
        if set(line.strip()) == {"="}:                  # the rule under the header
            continue
        fields = [f.strip() for f in line.split("\t")]
        try:
            mk, x, y = int(fields[2]), int(fields[4]), int(fields[5])
        except (IndexError, ValueError):
            raise ValueError(f"{url}: line {n} has no whole numbers in its MK #, X and Y columns: "
                             f"{line[:120]!r}") from None
        if not 1 <= mk <= 65535:
            raise ValueError(f"{url}: line {n}: MK # {mk} is not a label id in 1..65535")
        flags.append((mk, x, y))
    return flags


class _Points:
    """The flags of a table on the (Y, X) grid of its image: table pixel (X, Y) is the scale x scale image
    pixels from (X * scale, Y * scale), holding the flag's MK #; where flags share a pixel the later row is kept."""

    def __init__(self, flags: list[Flag], shape: tuple[int, int], scale: int, url: str):
        self.axes, self.shape, self.dtype = "yx", shape, np.dtype(np.uint16)
        self.voxel_size_nm = None
        self.access = "plane"           # a plane costs the table, already read: the harness may search it whole
        self._scale = scale
        self._flags = [(mk, x * scale, y * scale) for mk, x, y in flags]
        for (mk, x, y), (_, tx, ty) in zip(self._flags, flags):
            if not (0 <= y and y + scale <= shape[0] and 0 <= x and x + scale <= shape[1]):
                raise ValueError(f"{url}: flag {mk} at X {tx}, Y {ty} (image pixel x {x}, y {y} with scale "
                                 f"{scale}) lies outside the image, {shape[1]} x {shape[0]}; is params.scale right?")

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        sel = [region.get(k, slice(None)) for k in "yx"]
        # the image rows and columns asked for, as ranges (an int is one row, dropped again below)
        rows, cols = (range(n)[s] if isinstance(s, slice) else range(range(n)[s], range(n)[s] + 1)
                      for s, n in zip(sel, self.shape))
        out = np.zeros((len(rows), len(cols)), self.dtype)
        for mk, x, y in self._flags:
            for yy in range(y, y + self._scale):
                for xx in range(x, x + self._scale):
                    if yy in rows and xx in cols:
                        out[rows.index(yy), cols.index(xx)] = mk
        return out[tuple(slice(None) if isinstance(s, slice) else 0 for s in sel)]


def open_array(src: Source, io: IO) -> Array:
    scale, count = src.params.get("scale", 1), src.params.get("count")
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
        raise ValueError(f"params.scale must be a whole number of image pixels per table pixel, 1 or more, "
                         f"got {scale!r}")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise ValueError(f"params.count must be a number of rows, got {count!r}")
    urls = list(src.urls)
    if any("::" in u for u in urls):
        raise ValueError("this snippet reads plain urls, not zip members or globs")
    images = [u for u in urls if _is_image(io, u)]
    tables = [u for u in urls if u not in images]
    if len(urls) == 1 and images:
        if src.params:
            raise ValueError(f"params {sorted(src.params)} are for a points table; the one url is an image")
        return _Image(io, urls[0])
    if len(urls) != 2 or len(images) != 1 or len(tables) != 1:
        raise ValueError(f"{len(images)} image(s) and {len(tables)} other file(s) in the urls; bind one image "
                         "url (a raw), or one points table and the one image it annotates (a label)")
    with _open(io, tables[0]) as fh:
        flags = _flags(fh.read().decode("utf-8", errors="replace"), tables[0])
    if count is not None and len(flags) != count:
        raise ValueError(f"{tables[0]}: {len(flags)} rows, params.count says {count}; the table changed or "
                         "was cut short")
    return _Points(flags, _image_shape(io, images[0])[1][:2], scale, tables[0])
