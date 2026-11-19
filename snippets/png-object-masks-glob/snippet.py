"""One PNG per object (a cell drawn on black, each file the size of the whole image), chosen by glob over zip
members or an HTTP directory (or listed one by one), merged into one `yx` plane of ids.

An object's pixels are those where the file is not zero (any sample of an RGB file). By default every file gets
its own id, 1, 2, ... in the order of the expanded urls (instance ids); with `ids: url` a file gets the 1-based
position of the binding url it came from (class ids, when the binding has one glob per class folder). Where two
files overlap, the pixel keeps the id of the later file. 0 is where no file has an object.

A zip is opened once and its members are read from it by range, 1 MiB blocks at a time, so the hundreds of small
files of one image cost a few requests when they lie together in the zip, not two requests each. `open_array`
reads the zip's directory and the first file (the 33-byte header of a plain url); the first `read` decodes every
file and keeps the merged plane, so later reads cost nothing.
"""
import struct
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

IDS = ("file", "url")
SIGNATURE = b"\x89PNG\r\n\x1a\n"
HEADER = 33                         # signature (8) + IHDR chunk: length (4), type (4), 13 bytes of data, CRC (4)
SAMPLES = {0: 1, 2: 3}              # IHDR colour type -> samples per pixel: grey, RGB (no palette, no alpha)
DEPTHS = (8, 16)


class _Merged:
    def __init__(self, files: "_Files", urls: list[str], ids: list[int]):
        self._files, self._urls, self._ids = files, urls, ids
        self.shape: tuple[int, int] = _header(files.data(urls[0], HEADER), urls[0])[0]
        self.dtype = np.dtype("uint16" if max(ids) <= np.iinfo(np.uint16).max else "uint32")
        self.axes = "yx"
        self.voxel_size_nm = None       # pHYs, when present, is a print resolution, not a pixel size
        self.access = "file"            # any pixel may belong to any file's object: a region costs all the files
        self._plane: np.ndarray | None = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:
            self._plane = self._merge()
        return self._plane[region.get("y", slice(None)), region.get("x", slice(None))]

    def _merge(self) -> np.ndarray:
        """Every file's object painted with its id, in url order: the later file keeps a pixel two files share."""
        plane = np.zeros(self.shape, self.dtype)
        for url, k in zip(self._urls, self._ids):
            plane[self._object(url)] = k
        return plane

    def _object(self, url: str) -> np.ndarray:
        """The file's nonzero pixels as a (Y, X) bool array, checked against the first file's size."""
        data = self._files.data(url)
        shape, samples, dtype = _header(data, url)
        if shape != self.shape:
            raise ValueError(f"{url}: {shape} differs from the first file's {self.shape} (y, x); every mask of "
                             "one image must be drawn on the same grid")
        a = np.asarray(imagecodecs.png_decode(data))
        if a.shape != shape + (() if samples == 1 else (samples,)) or a.dtype != dtype:
            # e.g. a tRNS chunk decoded as an alpha sample: transparent black would count as an object
            raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, its header says {dtype} {shape} with "
                             f"{samples} sample(s)")
        return a != 0 if a.ndim == 2 else a.any(axis=2)


class _Files:
    """The bytes of a file by url. A zip is opened once, by range when the server allows (else staged whole), and
    its members are read from it; zipfile inflates a member and checks its CRC."""

    def __init__(self, io: IO):
        self._io = io
        self._zips: dict[str, ZipFile] = {}

    def data(self, url: str, n: int = -1) -> bytes:
        """The first `n` bytes of the file, or all of it."""
        path = url.find("/", url.find("://") + 3)       # "::" in the path, not in an IPv6 host
        i = url.find("::", path) if path >= 0 else -1
        if i >= 0:
            data = self._zip(url[:i]).read(url[i + 2:])
            return data if n < 0 else data[:n]
        try:
            with self._io.open(url) as fh:
                return fh.read(n)
        except NeedsFetch:      # a server without Range
            data = self._io.fetch(url).read_bytes()
            return data if n < 0 else data[:n]

    def _zip(self, url: str) -> ZipFile:
        if url not in self._zips:
            try:
                self._zips[url] = ZipFile(self._io.open(url))
            except NeedsFetch:
                self._zips[url] = ZipFile(self._io.fetch(url))
        return self._zips[url]


def _header(data: bytes, url: str) -> tuple[tuple[int, int], int, np.dtype]:
    """(height, width), samples per pixel and dtype from the signature and IHDR chunk."""
    if len(data) < HEADER or data[:8] != SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    if colour not in SAMPLES or depth not in DEPTHS:
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; this snippet reads 8- or 16-bit "
                         "grey (0) or RGB (2) masks. A palette's index 0 need not be black and an alpha sample "
                         "is not part of the drawing, so neither says which pixels are the object")
    return (height, width), SAMPLES[colour], np.dtype("uint8" if depth == 8 else "uint16")


def open_array(src: Source, io: IO) -> Array:
    mode = src.params.get("ids", "file")
    if mode not in IDS:
        raise ValueError(f"params.ids must be one of {IDS}, got {mode!r}")
    urls: list[str] = []
    ids: list[int] = []
    for n, url in enumerate(src.urls, start=1):
        files = io.expand(url)
        if not files:
            raise ValueError(f"{url}: matches no file")
        urls += files
        ids += [n] * len(files) if mode == "url" else range(len(urls) - len(files) + 1, len(urls) + 1)
    if not urls:
        raise ValueError("png-object-masks-glob needs at least one url")
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return _Merged(_Files(io), urls, ids)
