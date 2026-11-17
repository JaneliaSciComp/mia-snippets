"""One binary mask PNG per object, `<image>_<k>.png`, as zip members chosen by glob: the masks of one image are
merged into one instance-id plane (pixel = k where mask k is non-zero, 0 elsewhere) and the images are stacked
along z. With `params.images: true` the urls are the images the masks belong to instead, one grey or RGB PNG
per plane, returned as stored (the raw form).

The masks of one image are the members that share everything before `_<k>.<ext>`. Where masks overlap the
highest k is kept. Both forms read the zip opened once with `io.open`, a plane's members in one range request,
and wait and ask again when the server answers `429 Too Many Requests`, which the harness does not retry.
"""
import re
import time
from typing import Callable, TypeVar
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

NAME = re.compile(r"(?P<image>.+)_(?P<k>[0-9]+)(?P<ext>\.[A-Za-z0-9]+)")   # <image>_<k>.<ext>, k decimal
PNG = b"\x89PNG\r\n\x1a\n"
DTYPE = np.dtype(np.uint16)     # of the label form
ATTEMPTS = 6                    # tries per step that makes requests, while the answer is 429
WAIT = (1.0, 60.0, 120.0)       # seconds before the next try: at least, without a Retry-After, at most
SPAN = 4 << 20                  # a plane's members no further apart than this are asked for in one read
T = TypeVar("T")


def _wait(e: Exception) -> float | None:
    """Seconds to wait before asking again when `e` is the HTTP client's 429 answer (its Retry-After when that
    is a number of seconds), else None. Told by class name, since a snippet may not import the client."""
    response = getattr(e, "response", None)
    if "HTTPStatusError" not in {c.__name__ for c in type(e).__mro__} or getattr(response, "status_code", None) != 429:
        return None
    try:
        seconds = float(response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):     # no header, or an HTTP date
        seconds = WAIT[1]
    return min(max(seconds, WAIT[0]), WAIT[2]) if seconds == seconds else WAIT[1]    # nan: the default


def _patient(step: Callable[[], T]) -> T:
    """`step()`, tried up to ATTEMPTS times while the server answers 429; any other error is raised at once.
    The blocks a failed try did get stay in io's cache, so the next try asks for the rest only."""
    for attempt in range(ATTEMPTS):
        try:
            return step()
        except Exception as e:      # BudgetExceeded, HostNotAllowed, a 404, a bad zip: final
            wait = _wait(e)
            if wait is None or attempt == ATTEMPTS - 1:
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


def _decode(data: bytes, url: str, image: bool) -> np.ndarray:
    """A mask as a (Y, X) boolean plane, True where the stored grey value is not 0; or, with `image`, a grey or
    RGB PNG as stored, (Y, X) or (C, Y, X)."""
    # IHDR follows the signature: byte 24 is the bit depth, byte 25 the colour type (0 = grey, 2 = RGB,
    # 3 = palette, 4 = grey + alpha, 6 = RGB + alpha)
    if data[:8] != PNG or len(data) < 26:
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    depth, colour = data[24], data[25]
    if not image and colour != 0:
        raise ValueError(f"{url}: PNG colour type {colour}; the label form reads grey masks only (a palette, "
                         "colour or alpha mask has no single value to test against 0)")
    if image and (colour not in (0, 2) or depth < 8):
        raise ValueError(f"{url}: PNG colour type {colour}, bit depth {depth}; the raw form reads 8 and 16-bit "
                         "grey or RGB only (the decoder would replace palette indices by colours, rescale 1, 2 "
                         "and 4-bit values, and alpha is not a channel of the image)")
    a = np.asarray(imagecodecs.png_decode(data))
    if a.shape[2:] != ((3,) if colour == 2 else ()):    # a tRNS chunk decodes with an alpha sample
        raise ValueError(f"{url}: decoded to shape {a.shape}, not {'(Y, X, 3)' if colour == 2 else '(Y, X)'}")
    if not image:
        return a != 0
    return np.ascontiguousarray(np.moveaxis(a, 2, 0)) if colour == 2 else a


class _Zip:
    """The zip at `url`, opened once per array and read by range through io's 1 MiB block cache."""

    def __init__(self, io: IO, url: str):
        self._io, self.url = io, url
        self._zip: ZipFile | None = None

    def read(self, members: list[str]) -> list[bytes]:
        """The members' bytes (inflated and CRC-checked by zipfile), in the order given."""
        if self._zip is None:       # a HEAD, then the directory; blocks another array of the job read are reused
            self._zip = _patient(lambda: ZipFile(self._io.open(self.url)))
        infos = [self._zip.getinfo(m) for m in members]     # KeyError for a member the directory does not have
        start = min(i.header_offset for i in infos)
        end = max(i.header_offset + 30 + len(i.filename.encode()) + len(i.extra) + i.compress_size for i in infos)
        if end - start <= SPAN:
            # one read over all of them: io asks for the missing blocks in one request, where zipfile's own
            # reads (a header, then its data, member by member) would ask again at each block boundary
            fp = self._zip.fp       # zipfile seeks before each of its reads, so moving fp here is safe
            _patient(lambda: (fp.seek(start), fp.read(end - start)))
        return [_patient(lambda: self._zip.read(i)) for i in infos]


class _Stack:
    """Planes stacked along z. `load(z)` is plane z as (Y, X) or (C, Y, X); all must agree with plane 0, which
    `open_array` reads for the shape and dtype."""

    def __init__(self, count: int, load: Callable[[int], np.ndarray], name: Callable[[int], str]):
        self._count, self._load, self._name = count, load, name
        first = load(0)
        self._last: tuple[int, np.ndarray] = (0, first)     # the last plane read: c planes are asked one by one
        self._plane_shape, self.dtype = first.shape, np.dtype(first.dtype)
        self._plane_axes = "cyx" if first.ndim == 3 else "yx"
        self.axes = ("z" if count > 1 else "") + self._plane_axes
        self.shape = ((count,) if count > 1 else ()) + first.shape
        self.access = "plane"           # a plane is the whole image, or all its masks, whatever the region
        self.voxel_size_nm = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in self._plane_axes)
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(self._count)[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(z)[idx] for z in range(self._count)[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, z: int) -> np.ndarray:
        if self._last[0] == z:
            return self._last[1]
        a = self._load(z)
        if a.shape != self._plane_shape or a.dtype != self.dtype:
            raise ValueError(f"{self._name(z)}: {a.dtype} {a.shape} differs from the first plane's {self.dtype} "
                             f"{self._plane_shape} ({', '.join(self._plane_axes)}); all planes of one array "
                             "must agree")
        self._last = (z, a)
        return a


def _members(urls: tuple[str, ...], io: IO) -> tuple[str, list[str]]:
    """The one zip the urls name and the members they stand for (globs expanded), in the urls' order."""
    files = [u for url in urls for u in _patient(lambda: io.expand(url))]   # a glob reads the zip's directory
    if len(set(files)) != len(files):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    zips, members = set(), []
    for f in files:
        zip_url, sep, member = f.rpartition("::")
        if not sep:
            raise ValueError(f"{f}: not a zip member; this snippet reads `<zip url>::<member or glob>` only")
        zips.add(zip_url)
        members.append(member)
    if len(zips) != 1:
        raise ValueError(f"the urls name {len(zips)} zips ({sorted(zips)}); one array reads one zip")
    return zips.pop(), members


def _groups(url: str, members: list[str]) -> list[list[tuple[int, str]]]:
    """The masks grouped per image: the planes in the order `<image><ext>` sorts (the order of images named like
    the masks without `_<k>`), each plane's masks ascending in k."""
    groups: dict[str, dict[int, str]] = {}
    for member in members:
        m = NAME.fullmatch(member)
        if m is None or int(m["k"]) < 1:
            raise ValueError(f"{url}::{member}: the name is not <image>_<k>.<ext> with k >= 1, so it has no "
                             "instance id (params.images: true reads the images themselves)")
        k, masks = int(m["k"]), groups.setdefault(m["image"] + m["ext"], {})
        if k > np.iinfo(DTYPE).max:
            raise ValueError(f"{url}::{member}: id {k} does not fit {DTYPE}")
        if k in masks:
            raise ValueError(f"{url}::{member} and {masks[k]} are both mask {k} of one image")
        masks[k] = member
    return [sorted(groups[g].items()) for g in sorted(groups)]


def open_array(src: Source, io: IO) -> Array:
    images = src.params.get("images", False)
    if not isinstance(images, bool):
        raise ValueError(f"params.images must be true or false, got {images!r}")
    url, members = _members(src.urls, io)
    files = _Zip(io, url)
    if images:
        members = sorted(members)       # by member name, whatever the order of the urls: the label form's order

        def image(z: int) -> np.ndarray:
            return _decode(files.read([members[z]])[0], f"{url}::{members[z]}", True)

        return _Stack(len(members), image, lambda z: f"{url}::{members[z]}")
    planes = _groups(url, members)

    def merged(z: int) -> np.ndarray:
        """Image z as (Y, X) ids: its masks painted in ascending k, so the highest k wins where they overlap."""
        names = [member for _, member in planes[z]]
        out = None
        for (k, member), data in zip(planes[z], files.read(names)):
            mask = _decode(data, f"{url}::{member}", False)
            if out is None:
                out = np.zeros(mask.shape, DTYPE)
            elif mask.shape != out.shape:
                raise ValueError(f"{url}::{member}: {mask.shape} differs from {out.shape} of {names[0]} (y, x); "
                                 "the masks of one image must agree")
            out[mask] = k
        return out

    return _Stack(len(planes), merged, lambda z: f"{url}::{planes[z][0][1]}")
