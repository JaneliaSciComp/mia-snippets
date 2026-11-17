"""One image per file (PNG or single-page TIFF, told apart by the file's first bytes), from zip members chosen by
glob (or plain URLs), stacked along z. Two things set it apart from `image-tiles-glob`:

- `ids: rg` reads a colour PNG whose instance id is spread over two samples, id = R + 256 * G, as one uint16 id
  plane; `category_id` then keeps only the instances that the file's class JSON gives that category.
- A zip whose server states no size (no Content-Length on HEAD) or ignores Range cannot be read member by
  member; it is then staged whole with `io.fetch` and its members are read from the local copy.

Without `ids` a file must have no sample axis and its values are returned as decoded. Files are opened lazily:
`open_array` decodes the first file only; `read` loads the files it needs, one file per z plane.
"""
import json
from fnmatch import fnmatchcase
from io import BytesIO
from zipfile import ZipFile

import imagecodecs
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

IDS = ("rg",)
GLOB_CHARS = "*?["
PNG = b"\x89PNG\r\n\x1a\n"
TIFF = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def _split(url: str) -> tuple[str, str | None]:
    """(file url, zip member or None): the first '::' after the host, as the harness splits it."""
    path = url.find("/", url.find("://") + 3)
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


class _Store:
    """The bytes behind urls. A zip that `io` cannot read by member is staged whole, once, and read locally."""

    def __init__(self, io: IO):
        self._io = io
        self._zips: dict[str, ZipFile] = {}     # zip url -> the staged whole zip

    def expand(self, url: str) -> list[str]:
        """A glob url as the file urls it matches, sorted by name; any other url as itself."""
        try:
            return self._io.expand(url)
        except NeedsFetch:      # the zip's directory cannot be read by range
            zip_url, glob = _split(url)
            if glob is None or zip_url.endswith("/"):
                raise
            names = sorted(n for n in self._zip(zip_url).namelist() if not n.endswith("/") and fnmatchcase(n, glob))
            if not names:
                raise ValueError(f"no member of {zip_url} matches {glob!r}") from None
            return [f"{zip_url}::{n}" for n in names]

    def read(self, url: str) -> bytes:
        """The whole file: by range when it can be, else staged (a deflated member), else from the staged zip."""
        zip_url, member = _split(url)
        if member is not None and zip_url in self._zips:
            return self._member(zip_url, member)
        try:
            with self._io.open(url) as fh:
                return fh.read()
        except NeedsFetch:
            pass
        try:
            return self._io.fetch(url).read_bytes()
        except NeedsFetch:      # not even one member can be fetched: no size on HEAD, or Range ignored
            if member is None:
                raise
            return self._member(zip_url, member)

    def _zip(self, zip_url: str) -> ZipFile:
        if zip_url not in self._zips:
            self._zips[zip_url] = ZipFile(self._io.fetch(zip_url))
        return self._zips[zip_url]

    def _member(self, zip_url: str, member: str) -> bytes:
        try:
            return self._zip(zip_url).read(member)      # zipfile inflates the member and checks its CRC
        except KeyError:
            raise KeyError(f"no member {member!r} in {zip_url}") from None


def _decode(data: bytes, url: str) -> np.ndarray:
    """The file's one image as (Y, X) or (Y, X, S), by its magic bytes (a zip member's name is not trusted)."""
    if data[:8] == PNG:
        # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type (3 = palette)
        if len(data) < 26 or data[25] == 3 or data[24] < 8:
            raise ValueError(f"{url}: a palette or 1/2/4-bit PNG; the decoder would replace the stored indices "
                             "by palette colours or rescale them to 0..255, so this snippet does not read it")
        a = imagecodecs.png_decode(data)
    elif data[:4] in TIFF:
        with tifffile.TiffFile(BytesIO(data)) as tif:
            s = tif.series[0]
            if s.axes not in ("YX", "YXS") or len(tif.series) != 1:
                raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)}; this snippet reads "
                                 "single-page YX or YXS tiles only, see tiff-zip-member for stacks")
            a = s.asarray()
    else:
        raise ValueError(f"{url}: not a PNG or TIFF file: header={data[:8]!r}")
    a = np.asarray(a)
    if a.ndim not in (2, 3):
        raise ValueError(f"{url}: decoded to shape {a.shape}, not (Y, X) or (Y, X, S)")
    return a


def _rg_ids(a: np.ndarray, url: str) -> np.ndarray:
    """(Y, X, S >= 3) uint8 as the uint16 plane R + 256 * G. Blue must be 0: it would be the id's third byte."""
    if a.ndim != 3 or a.shape[2] < 3 or a.dtype != np.uint8:
        raise ValueError(f"{url}: {a.dtype} {a.shape}; params.ids 'rg' needs an 8-bit file with at least "
                         "3 samples (R, G, B[, A])")
    if a[:, :, 2].any():
        raise ValueError(f"{url}: the blue sample is not 0 everywhere, so the id is not R + 256 * G")
    return a[:, :, 0].astype(np.uint16) | (a[:, :, 1].astype(np.uint16) << 8)


def _classes_url(url: str) -> str:
    """`.../masks/<stem>.<ext>` -> `.../classes/<stem>.json`, the class JSON beside a mask."""
    zip_url, member = _split(url)
    folder, _, name = (url if member is None else member).rpartition("/")
    parent, _, last = folder.rpartition("/")
    stem, dot, _ = name.rpartition(".")
    if last != "masks" or not dot:
        raise ValueError(f"{url}: params.category_id reads the class JSON beside a mask, "
                         "<dir>/masks/<stem>.<ext> -> <dir>/classes/<stem>.json, and this file is not in a "
                         "'masks' folder")
    sibling = f"{parent}/classes/{stem}.json" if parent else f"classes/{stem}.json"
    return sibling if member is None else f"{zip_url}::{sibling}"


def _categories(data: bytes, url: str) -> dict[int, int]:
    """{"annotations": [{"id": <instance id>, "category_id": <class>}, ...]} as instance id -> class."""
    doc = json.loads(data)
    rows = doc.get("annotations") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{url}: no 'annotations' list")
    out: dict[int, int] = {}
    for row in rows:
        ok = isinstance(row, dict) and all(type(row.get(k)) is int for k in ("id", "category_id"))
        if not ok or not 0 < row["id"] <= 0xFFFF or out.setdefault(row["id"], row["category_id"]) != row["category_id"]:
            raise ValueError(f"{url}: annotation {row!r} is not one {{'id': 1..65535, 'category_id': int}} "
                             "per instance")
    return out


class _Tiles:
    def __init__(self, store: _Store, urls: list[str], ids: str | None, category_id: int | None):
        self._store, self._urls, self._ids, self._category = store, urls, ids, category_id
        self._last: tuple[int, np.ndarray] | None = None    # the last file decoded
        first = self._load(0)                               # (Y, X); every file must match
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self.axes = ("z" if len(urls) > 1 else "") + "yx"
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + first.shape
        self.access = "plane"           # one file per z plane: any region of it costs the whole file
        self.voxel_size_nm = None       # files of one array need not share a calibration; see the README
        self._last = (0, first)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = (region.get("y", slice(None)), region.get("x", slice(None)))
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._urls))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._urls))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X), checked against file 0."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        a = self._load(k)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._urls[k]}: {a.dtype} {a.shape} differs from the first file's "
                             f"{self.dtype} {self._plane_shape}; all files of one array must agree")
        self._last = (k, a)
        return a

    def _load(self, k: int) -> np.ndarray:
        url = self._urls[k]
        a = _decode(self._store.read(url), url)
        if self._ids is None:
            if a.ndim != 2:
                raise ValueError(f"{url}: {a.shape} has a sample axis; this snippet reads grey files, or ids "
                                 "from samples with params.ids. image-tiles-glob reads colour as a c axis")
            return a
        a = _rg_ids(a, url)
        if self._category is None:
            return a
        json_url = _classes_url(url)
        classes = _categories(self._store.read(json_url), json_url)
        unknown = np.setdiff1d(np.unique(a), [0, *classes])
        if unknown.size:
            raise ValueError(f"{url}: ids {unknown[:10].tolist()} have no annotation in {json_url}, so their "
                             "class is unknown")
        keep = np.zeros(0x10000, np.uint16)                 # id -> id when it is of the wanted class, else 0
        wanted = [i for i, c in classes.items() if c == self._category]
        keep[wanted] = wanted
        return keep[a]


def open_array(src: Source, io: IO) -> Array:
    ids, category_id = src.params.get("ids"), src.params.get("category_id")
    if ids is not None and ids not in IDS:
        raise ValueError(f"params.ids must be one of {IDS}, got {ids!r}")
    if category_id is not None and (type(category_id) is not int or ids is None):
        raise ValueError(f"params.category_id must be an int and needs params.ids, got {category_id!r} "
                         f"with ids {ids!r}")
    store = _Store(io)
    urls = [u for url in src.urls for u in store.expand(url)]
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return _Tiles(store, urls, ids, category_id)
