"""One PNG image per array: a zip member (stored or deflated) or a plain URL, read as it is stored.

A greyscale PNG is `yx`, 1-, 2- and 4-bit ones included: their stored values (0/1 for a 1-bit mask) are
returned, not the 0..255 the decoder stretches them to. A colour PNG's samples are a `c` axis (`cyx`), or one
plane with `samples: grey` when they are identical copies. `open_array` reads the file's first 33 bytes (the
IHDR chunk: size, bit depth, colour type), for a deflated zip member by inflating just the start of it through
the zip; `read` gets and decodes the whole file.
"""
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MODES = ("grey",)
SIGNATURE = b"\x89PNG\r\n\x1a\n"
HEADER = 33                             # signature (8) + IHDR chunk (4 length, 4 type, 13 data, 4 crc)
SAMPLES = {0: 1, 2: 3, 4: 2, 6: 4}      # PNG colour type -> samples per pixel: grey, RGB, grey+alpha, RGBA
DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 4: (8, 16), 6: (8, 16)}
KEEP = 2                                # decoded files kept: a raw and its label, whatever the number of arrays

_decoded: list[tuple[IO, str, np.ndarray]] = []     # the last KEEP files read, shared by every array of the job


def _header(io: IO, url: str) -> bytes:
    """The first HEADER bytes of the file, without reading the rest of it when ranges allow."""
    try:
        with io.open(url) as fh:                    # a plain url or a stored member: one range read
            return fh.read(HEADER)
    except NeedsFetch:
        pass
    i = url.find("::", max(url.find("/", url.find("://") + 3), 0))     # "::" in the path, as io splits it
    if i >= 0:                                      # a deflated member: inflate its start through the zip
        zip_url, member = url[:i], url[i + 2:]
        try:
            zf = io.open(zip_url)
            try:
                with ZipFile(zf) as z, z.open(member) as fh:
                    return fh.read(HEADER)
            finally:
                zf.close()
        except NeedsFetch:
            pass
    with open(io.fetch(url), "rb") as fh:           # a server without Range: the whole file, staged
        return fh.read(HEADER)


class _Image:
    def __init__(self, io: IO, url: str, samples: str | None):
        self._io, self._url, self._samples = io, url, samples
        head = _header(io, url)
        if len(head) < HEADER or head[:8] != SIGNATURE or head[12:16] != b"IHDR":
            raise ValueError(f"{url}: not a PNG file: header={head[:8]!r}")
        width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        depth, colour = head[24], head[25]
        if colour == 3:
            raise ValueError(f"{url}: a palette PNG; the decoder would replace the stored indices by palette "
                             "colours, so this snippet does not read it")
        if colour not in SAMPLES or depth not in DEPTHS[colour] or not width or not height:
            raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}, {width}x{height} is not a "
                             "valid PNG header")
        n = SAMPLES[colour]
        self._file_shape = (height, width) + ((n,) if n > 1 else ())    # what the decoder must return
        self._top = (1 << depth) - 1 if depth < 8 else None             # largest stored value of a 1/2/4-bit file
        plane = n == 1 or samples == "grey"
        self.axes = "yx" if plane else "cyx"
        self.shape = (height, width) if plane else (n, height, width)
        self.dtype = np.dtype(np.uint16 if depth == 16 else np.uint8)
        self.voxel_size_nm = None       # a pHYs chunk is a print density more often than a pixel size
        self.access = "file"            # the one file is the whole array: any region costs fetching and decoding it

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self.axes)
        return np.array(self._image()[idx])         # a copy: the caller may write to it, the kept image stays

    def _image(self) -> np.ndarray:
        """The decoded file as the array presents it, (Y, X) or (C, Y, X)."""
        for io, url, a in _decoded:
            if io is self._io and url == self._url:
                return a
        try:
            with self._io.open(self._url) as fh:
                data = fh.read()
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            data = self._io.fetch(self._url).read_bytes()
        a = np.asarray(imagecodecs.png_decode(data))
        if a.shape != self._file_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._url}: decoded as {a.dtype} {a.shape}, its header says {self.dtype} "
                             f"{self._file_shape}; a tRNS chunk (transparency the decoder turns into an alpha "
                             "sample) does that, and this snippet does not read such a file")
        if self._top is not None and int(a.max()) > self._top:
            # libpng stretches 1/2/4-bit grey to 0..255 (stored * 255 / top); undo it, exactly or not at all
            scale = 255 // self._top
            if np.any(a % scale):
                raise ValueError(f"{self._url}: decoded values are not multiples of {scale}, so the stored "
                                 f"0..{self._top} values cannot be recovered")
            a = a // scale
        if a.ndim == 3:
            if self._samples == "grey":
                if not np.array_equal(a, np.broadcast_to(a[:, :, :1], a.shape)):
                    raise ValueError(f"{self._url}: samples differ, so this is not a grey image or mask stored "
                                     "as RGB; drop params.samples to read the samples as a c axis")
                a = np.ascontiguousarray(a[:, :, 0])
            else:
                a = np.ascontiguousarray(np.moveaxis(a, 2, 0))
        _decoded.append((self._io, self._url, a))
        del _decoded[:-KEEP]
        return a


def open_array(src: Source, io: IO) -> Array:
    samples = src.params.get("samples")
    if samples is not None and samples not in MODES:
        raise ValueError(f"params.samples must be one of {MODES}, got {samples!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(urls) != 1:
        raise ValueError(f"the urls name {len(urls)} files; this snippet reads one PNG per array, so bind each "
                         "image as its own raw or label entry (image-tiles-glob stacks files of one shape)")
    return _Image(io, urls[0], samples)
