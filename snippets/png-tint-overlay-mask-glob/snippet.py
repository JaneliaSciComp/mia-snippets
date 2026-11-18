"""Binary masks from tint overlays: 8-bit RGB PNGs that show a grey image with its annotated pixels tinted in
one colour (for example blue blended over the nanowires of a TEM image), chosen by glob (zip members, an HTTP
directory) or listed one by one, stacked along z, one file per plane.

A pixel of a grey image saved as RGB has three equal samples; a tinted pixel does not. The mask is 1 where a
pixel's samples are not all equal and 0 elsewhere. A tint blended over whole pixels shifts the samples by the
same (G - R, B - R) wherever it is drawn, and by one more, larger shift of the same colour where annotation
shapes overlap and it is blended again. So the tinted pixels of one file must carry at most MAX_LAYERS
different shifts, all of one colour; a file where they do not (a real colour image, an anti-aliased or lossy
overlay, two classes in two colours) raises instead of giving a mask.

An overlay belongs to an image, `<stem><suffix>.png` to `<stem>.<image_ext>` in the same folder, and the files a
glob stands for are ordered by the name of that image, not by their own: `a_2.jpg` sorts before `a_23.jpg`, but
`a_23_label.png` before `a_2_label.png`. The label array is then plane for plane the raw array bound with the
image glob. `open_array` reads the first file's header only; `read` decodes the files it needs.
"""
import imagecodecs
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PNG = b"\x89PNG\r\n\x1a\n"
HEAD = 33               # the signature (8 bytes) and the IHDR chunk (4 length, 4 type, 13 data, 4 CRC)
RGB8 = (8, 2)           # IHDR bit depth and colour type of an 8-bit RGB PNG
MAX_LAYERS = 8          # the most different tint shifts one file may carry: one per depth of overlapping shapes
SPAN = 511              # the values a difference of two uint8 samples can take, -255..255


def _bytes(io: IO, url: str, limit: int = -1) -> bytes:
    """The file's first `limit` bytes, or all of it."""
    try:
        with io.open(url) as f:
            return f.read(limit)
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        with io.fetch(url).open("rb") as f:
            return f.read(limit)


def _size(head: bytes, url: str) -> tuple[int, int]:
    """(Y, X) from the IHDR chunk of an 8-bit RGB PNG; any other file raises. Nothing is decoded."""
    if len(head) < HEAD or head[:8] != PNG or head[12:16] != b"IHDR":
        raise ValueError(f"{url}: not a PNG file: header={head[:8]!r}")
    nx, ny = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if (head[24], head[25]) != RGB8:
        raise ValueError(f"{url}: PNG colour type {head[25]}, bit depth {head[24]}; this snippet reads 8-bit RGB "
                         "PNGs (colour type 2) only: a tint overlay needs three samples to show its tint")
    return ny, nx


def _tints(shift: np.ndarray, url: str) -> None:
    """Raise unless the (N, 2) shifts (G - R, B - R) of a file's tinted pixels are one tint: at most MAX_LAYERS
    different ones, all of one colour (each a positive multiple of the largest, give or take the rounding)."""
    codes = (shift[:, 0].astype(np.int32) + 255) * SPAN + (shift[:, 1] + 255)
    found = np.flatnonzero(np.bincount(codes, minlength=SPAN * SPAN))
    tints = sorted(((int(c) // SPAN - 255, int(c) % SPAN - 255) for c in found), key=lambda t: abs(t[0]) + abs(t[1]))
    if len(tints) > MAX_LAYERS:
        raise ValueError(f"{url}: the pixels whose samples differ carry {len(tints)} different (G - R, B - R), "
                         f"e.g. {list(tints[0])} and {list(tints[-1])}; a tint drawn on whole pixels gives one, "
                         f"and one more for each depth of overlapping shapes (at most {MAX_LAYERS} are read). "
                         "This is a colour image or an anti-aliased or lossy overlay, so its coloured pixels are "
                         "not a mask")
    top = tints[-1] if tints else (0, 0)
    for t in tints:
        slack = abs(t[0]) + abs(t[1]) + abs(top[0]) + abs(top[1])   # each of the four samples may round by one
        if abs(t[0] * top[1] - t[1] * top[0]) > slack or t[0] * top[0] + t[1] * top[1] <= 0:
            raise ValueError(f"{url}: the pixels whose samples differ carry (G - R, B - R) = {list(t)} and "
                             f"{list(top)}, two colours; this snippet reads one tint as one binary mask and "
                             "does not tell classes apart")


def _mask(data: bytes, url: str) -> np.ndarray:
    """(Y, X) uint8: 1 where the pixel's samples are not all equal (tinted), 0 where they are (grey)."""
    shape = _size(data[:HEAD], url) + (3,)
    a = np.asarray(imagecodecs.png_decode(data))
    if a.shape != shape or a.dtype != np.uint8:
        raise ValueError(f"{url}: decoded as {a.dtype} {a.shape}, not uint8 {shape} as its header says (a tRNS "
                         "chunk adds an alpha sample)")
    shift = a[:, :, 1:].astype(np.int16) - a[:, :, :1]      # (G - R, B - R) of every pixel
    tinted = shift.any(axis=2)
    _tints(shift[tinted], url)
    return tinted.astype(np.uint8)


class _Masks:
    """One overlay per z plane, each read as the (Y, X) mask of its tinted pixels."""

    def __init__(self, io: IO, urls: list[str]):
        self._io, self._urls = io, urls
        self._last: tuple[int, np.ndarray] | None = None    # the last mask made: a crop reads its plane twice
        self._plane_shape = _size(_bytes(io, urls[0], HEAD), urls[0])
        self.axes = "zyx" if len(urls) > 1 else "yx"        # as image-tiles-glob does for the images
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + self._plane_shape
        self.dtype = np.dtype(np.uint8)
        self.access = "plane"           # one file per z plane: any region of it costs the whole file, read by
        self.voxel_size_nm = None       # range (stored member, plain url) or staged (deflated member)

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in "yx")
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
        """File k as its (Y, X) mask, checked against file 0's size before it is decoded."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url = self._urls[k]
        data = _bytes(self._io, url)
        if (shape := _size(data[:HEAD], url)) != self._plane_shape:
            raise ValueError(f"{url}: {shape} differs from the first file's {self._plane_shape} (y, x); all "
                             "files of one array must agree")
        self._last = (k, _mask(data, url))
        return self._last[1]


def _split(url: str) -> tuple[str, str | None]:
    """(zip or directory url, member or glob after `::`), or (url, None) for a plain url."""
    path = url.find("/", url.find("://") + 3)       # "::" in the path, not in an IPv6 host
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


def _image_url(url: str, suffix: str, ext: str) -> str:
    """The image an overlay was drawn on: `<stem><suffix>.png` -> `<stem>.<ext>` in the same folder."""
    container, member = _split(url)
    head, slash, name = (url if member is None else member).rpartition("/")     # head is "" at the top of a zip
    stem, dot, _ = name.rpartition(".")
    if not dot or not stem.endswith(suffix) or len(stem) == len(suffix):
        raise ValueError(f"{url}: the file name is not '<stem>{suffix}.<extension>', so the image it was drawn "
                         f"on cannot be named (params.suffix is {suffix!r})")
    image = f"{head}{slash}{stem[:len(stem) - len(suffix)]}.{ext}"
    return image if member is None else f"{container}::{image}"


def _expand(urls: tuple[str, ...], io: IO, suffix: str, ext: str) -> list[str]:
    """The overlay files. A url after `::` (a zip member, a member glob, a directory glob) stands for its files
    ordered by the name of the image each was drawn on, as a glob over those images sorts them, and each image
    must be in the same listing. Plain urls pass through. Binding order is kept across urls."""
    out: list[str] = []
    for url in urls:
        files = io.expand(url)
        images = [_image_url(f, suffix, ext) for f in files]
        container, member = _split(url)
        if member is not None:
            listed = set(io.list(container))        # the listing the glob was expanded from: no more bytes
            names = [im.rpartition("/")[2] if container.endswith("/") else _split(im)[1] for im in images]
            if missing := [n for n in names if n not in listed]:
                raise ValueError(f"{url}: {len(missing)} of {len(files)} overlays have no image "
                                 f"'<stem>.{ext}' beside them, e.g. {missing[0]!r} is not in {container}; "
                                 "check params.suffix and params.image_ext")
        if len(set(images)) != len(images):
            raise ValueError(f"{url}: two overlays name the same image")
        out += [f for _, f in sorted(zip(images, files))]
    if not out:
        raise ValueError("the urls name no file")
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    suffix, ext = src.params.get("suffix", "_label"), src.params.get("image_ext", "jpg")
    if not isinstance(suffix, str) or "/" in suffix or "." in suffix:
        raise ValueError(f"params.suffix must be the text between an image's stem and its overlay's extension "
                         f"(no '/' or '.'), got {suffix!r}")
    if not (isinstance(ext, str) and ext and ext.isalnum()):
        raise ValueError(f"params.image_ext must be a file extension without the dot, got {ext!r}")
    return _Masks(io, _expand(src.urls, io, suffix, ext))
