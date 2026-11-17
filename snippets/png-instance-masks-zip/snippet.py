"""One binary mask PNG per object, `<image>_<k>.png`, as zip members chosen by glob: the masks of one image are
merged into one instance-id plane (pixel = k where mask k is non-zero, 0 elsewhere) and the images are stacked
along z.

The masks of one image are the members that share everything before `_<k>.<ext>`. Where masks overlap the
highest k is kept. `open_array` reads the zip directory and one mask (for the plane's shape); `read` reads the
masks of the planes it is asked for, through the zip opened once with `io.open`, so a plane of 100 masks costs
the 1 MiB blocks that hold them and not 200 requests.
"""
import re
from zipfile import ZipFile

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

NAME = re.compile(r"(?P<image>.+)_(?P<k>[0-9]+)(?P<ext>\.[A-Za-z0-9]+)")   # <image>_<k>.<ext>, k decimal
PNG = b"\x89PNG\r\n\x1a\n"
DTYPE = np.dtype(np.uint16)


def _decode(data: bytes, url: str) -> np.ndarray:
    """The mask as a (Y, X) boolean plane: True where the stored grey value is not 0."""
    # IHDR follows the signature: byte 25 is the colour type (0 = grey; 2, 6 colour; 3 palette; 4 grey + alpha)
    if data[:8] != PNG or len(data) < 26:
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}")
    if data[25] != 0:
        raise ValueError(f"{url}: PNG colour type {data[25]}; this snippet reads grey masks only (a palette, "
                         "colour or alpha mask has no single value to test against 0)")
    a = np.asarray(imagecodecs.png_decode(data))
    if a.ndim != 2:     # a grey PNG with a tRNS chunk decodes with an alpha sample
        raise ValueError(f"{url}: decoded to shape {a.shape}, not (Y, X)")
    return a != 0


class _Masks:
    """`planes[z]` is the masks of one image as (k, member) ascending in k; all planes are in the zip `url`."""

    def __init__(self, io: IO, url: str, planes: list[list[tuple[int, str]]]):
        self._io, self._url, self._planes = io, url, planes
        self._zip: ZipFile | None = None
        self._last: tuple[int, np.ndarray] | None = None        # the last plane merged
        self._plane_shape = self._mask(planes[0][0][1]).shape   # every mask of the array must match
        self.dtype = DTYPE
        self.axes = ("z" if len(planes) > 1 else "") + "yx"
        self.shape = ((len(planes),) if len(planes) > 1 else ()) + self._plane_shape
        self.access = "plane"           # one plane is all the masks of one image, whatever the region
        self.voxel_size_nm = None

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in "yx")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._planes))[sel])[idx]     # IndexError when out of range, like numpy
        planes = [self._plane(z)[idx] for z in range(len(self._planes))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._plane_shape, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, z: int) -> np.ndarray:
        """Image z as (Y, X) ids: its masks painted in ascending k, so the highest k wins where they overlap."""
        if self._last is not None and self._last[0] == z:
            return self._last[1]
        out = np.zeros(self._plane_shape, self.dtype)
        for k, member in self._planes[z]:
            mask = self._mask(member)
            if mask.shape != self._plane_shape:
                raise ValueError(f"{self._url}::{member}: {mask.shape} differs from the first mask's "
                                 f"{self._plane_shape} (y, x); all masks of one array must agree")
            out[mask] = k
        self._last = (z, out)
        return out

    def _mask(self, member: str) -> np.ndarray:
        if self._zip is None:
            self._zip = ZipFile(self._io.open(self._url))   # range-backed: the directory, then blocks as read
        return _decode(self._zip.read(member), f"{self._url}::{member}")   # zipfile inflates and checks the CRC


def _planes(urls: tuple[str, ...], io: IO) -> tuple[str, list[list[tuple[int, str]]]]:
    """The zip the urls name and its masks grouped per image: the planes in the order `<image><ext>` sorts (the
    order of images named like the masks without `_<k>`), each plane's masks ascending in k."""
    files = [u for url in urls for u in io.expand(url)]     # a glob becomes its matching members
    if len(set(files)) != len(files):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    zips: set[str] = set()
    groups: dict[str, dict[int, str]] = {}
    for f in files:
        zip_url, sep, member = f.rpartition("::")
        if not sep:
            raise ValueError(f"{f}: not a zip member; this snippet reads `<zip url>::<member or glob>` only")
        zips.add(zip_url)
        m = NAME.fullmatch(member)
        if m is None or int(m["k"]) < 1:
            raise ValueError(f"{f}: the name is not <image>_<k>.<ext> with k >= 1, so it has no instance id")
        k, masks = int(m["k"]), groups.setdefault(m["image"] + m["ext"], {})
        if k > np.iinfo(DTYPE).max:
            raise ValueError(f"{f}: id {k} does not fit {DTYPE}")
        if k in masks:
            raise ValueError(f"{f} and {masks[k]} are both mask {k} of one image")
        masks[k] = member
    if len(zips) != 1:
        raise ValueError(f"the urls name {len(zips)} zips ({sorted(zips)}); one array reads one zip")
    return zips.pop(), [sorted(groups[g].items()) for g in sorted(groups)]


def open_array(src: Source, io: IO) -> Array:
    url, planes = _planes(src.urls, io)
    return _Masks(io, url, planes)
