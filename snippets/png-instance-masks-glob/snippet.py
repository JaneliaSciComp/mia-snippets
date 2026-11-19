"""One binary PNG per object, `<stem><mask><k>.<ext>`, beside the image it annotates, `<stem><image>`, in an
HTTP directory or a zip, chosen by glob (or listed one by one): the masks of one image are merged into one
instance-id plane (pixel = k where mask k is non-zero, 0 elsewhere, uint16) and the images are stacked along z.

The urls name the mask files and the image files. The images are not read: they say which planes there are and
in which order (the order the image urls expand in, as `image-tiles-glob` stacks the same urls). An image
without any mask file is an all-zero plane, and the masks of an image that is not among the urls are left out.
Where masks overlap the highest k is kept.
"""
import re

import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
DTYPE = np.dtype(np.uint16)
Plane = tuple[str, list[tuple[int, str]]]      # (image url, its masks as (k, mask url), ascending in k)


def _bytes(io: IO, url: str) -> bytes:
    try:
        with io.open(url) as fh:
            return fh.read()
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def _size(data: bytes, url: str) -> tuple[int, int]:
    """(Y, X) of a PNG from its IHDR, which follows the 8-byte signature: width at byte 16, height at 20. Told
    by the first bytes, not by the file's extension."""
    if data[:8] != PNG or len(data) < 26 or data[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={data[:8]!r}; this snippet reads PNG masks only (a "
                         "JPEG mask is lossy and has no exact zero)")
    return int.from_bytes(data[20:24], "big"), int.from_bytes(data[16:20], "big")


def _mask(data: bytes, url: str, shape: tuple[int, int]) -> np.ndarray:
    """A mask file as a (Y, X) boolean plane, True where the stored value is not 0. A mask saved with samples
    (RGB, RGBA, grey + alpha, palette) is read when it is a grey mask in disguise: the colour samples of every
    pixel equal and the alpha, if any, fully opaque everywhere."""
    if (size := _size(data, url)) != shape:
        raise ValueError(f"{url}: {size} differs from the array's {shape} (y, x); every mask must be on the "
                         "grid of the first one")
    a = np.asarray(imagecodecs.png_decode(data))
    if a.ndim == 3:
        if a.shape[2] in (2, 4):        # grey + alpha, RGBA (also what a tRNS chunk decodes to)
            if a[:, :, -1].min() != np.iinfo(a.dtype).max:
                raise ValueError(f"{url}: a mask with transparent pixels; whether those are background is not "
                                 "this snippet's to decide")
            a = a[:, :, :-1]
        if not np.array_equal(a, np.broadcast_to(a[:, :, :1], a.shape)):
            raise ValueError(f"{url}: a colour mask whose samples differ, so it has no single value to test "
                             "against 0")
        a = a[:, :, 0]
    if a.shape != shape:
        raise ValueError(f"{url}: decoded to shape {a.shape}, not {shape}")
    return a != 0


class _Planes:
    def __init__(self, io: IO, planes: list[Plane]):
        self._io, self._planes = io, planes
        url = next(masks[0][1] for _, masks in planes if masks)        # the only file read at open
        self._plane_shape = _size(_bytes(io, url), url)
        self._last: tuple[int, np.ndarray] | None = None                # the last plane merged
        self.axes = "zyx" if len(planes) > 1 else "yx"
        self.shape = ((len(planes),) if len(planes) > 1 else ()) + self._plane_shape
        self.dtype = DTYPE
        self.access = "plane"           # a plane is all the masks of one image, whatever the region asked for
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
        """Image z as (Y, X) ids: its masks painted in ascending k, so the highest k wins where they overlap;
        all zero for an image without masks."""
        if self._last is not None and self._last[0] == z:
            return self._last[1]
        out = np.zeros(self._plane_shape, self.dtype)
        for k, url in self._planes[z][1]:
            out[_mask(_bytes(self._io, url), url, self._plane_shape)] = k
        self._last = (z, out)
        return out


def _stem(url: str, ending: str) -> str:
    """`url` without `ending`; what is left must still name a file (`.../<stem>` or `<zip>::...<stem>`)."""
    stem = url[:len(url) - len(ending)]
    if not stem or stem.endswith(("/", "::")):
        raise ValueError(f"{url}: the file name is {ending!r} alone, with no stem before it to tell which image "
                         "it is")
    return stem


def _planes(urls: tuple[str, ...], io: IO, mask: str, image: str) -> list[Plane]:
    """The files the urls stand for, told apart by name: `<stem><mask><k>.<ext>` is mask k of the image
    `<stem><image>` (same folder, same zip). One plane per image, in the order the image urls expand in (binding
    order across urls, sorted by name within a glob); the masks of an image that is not among the urls are left
    out, so one mask glob serves each of the arrays a set of images is split into."""
    files = [u for url in urls for u in io.expand(url)]
    if len(set(files)) != len(files):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    name = re.compile(r"(?P<stem>.*)" + re.escape(mask) + r"(?P<k>[0-9]+)\.[A-Za-z0-9]+", re.DOTALL)
    images: dict[str, str] = {}                 # stem -> image url, in the urls' order
    masks: dict[str, dict[int, str]] = {}       # stem -> k -> mask url
    for f in files:
        if (m := name.fullmatch(f)) is not None:
            stem, k = _stem(f, f[m.end("stem"):]), int(m["k"])
            if not 1 <= k <= np.iinfo(DTYPE).max:
                raise ValueError(f"{f}: mask number {k} is not an instance id (1 to {np.iinfo(DTYPE).max}; 0 is "
                                 "the background)")
            if k in (found := masks.setdefault(stem, {})):
                raise ValueError(f"{f} and {found[k]} are both mask {k} of one image")
            found[k] = f
        elif f.endswith(image):
            images[_stem(f, image)] = f
        else:
            raise ValueError(f"{f}: the name is neither a mask '<stem>{mask}<k>.<ext>' (params.mask, k a decimal "
                             f"number) nor an image '<stem>{image}' (params.image); the urls must name these "
                             "two kinds of file only")
    if not images:
        raise ValueError(f"the urls name no image '<stem>{image}' (params.image); the images say which planes "
                         "there are and in which order, so their urls belong beside the masks'")
    if not masks:
        raise ValueError(f"the urls name no mask '<stem>{mask}<k>.<ext>' (params.mask)")
    if not any(stem in images for stem in masks):
        example = next(iter(next(iter(masks.values())).values()))
        raise ValueError(f"none of the {len(masks)} images the masks belong to is among the urls as "
                         f"'<stem>{image}', e.g. the image of {example}; check params.image and the image urls")
    return [(url, sorted(masks.get(stem, {}).items())) for stem, url in images.items()]


def open_array(src: Source, io: IO) -> Array:
    mask, image = src.params.get("mask"), src.params.get("image")
    for key, value in (("mask", mask), ("image", image)):
        if not isinstance(value, str) or not value or "/" in value or "::" in value:
            raise ValueError(f"params.{key} must be a non-empty part of a file name (no '/'), got {value!r}")
    return _Planes(io, _planes(src.urls, io, mask, image))
