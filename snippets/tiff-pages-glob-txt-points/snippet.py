"""A set of multi-page TIFFs whose pages are channels (tifffile writes them as a 'shaped' series with axes QYX,
one YX page per channel), one file per z, chosen by glob over a zip's members; or the points that headerless
text files beside them list for one channel, marked on that channel's grid.

Without `params.points` the array is the images: every page of a file is a channel (`[z]cyx`), or
`params.channel` picks one page (`[z]yx`). A single-page file (a mask) is `[z]yx`.

With `params.points`, a member name template such as `set/annotations/{name}_C{c}.txt`, the array is a label on
the planes of `params.channel` of the same files: each image has one text file of `a,b` lines, one point per
line, and plane z holds the points of file z, each as its 1-based line number at its pixel or in a disc of
`params.radius` pixels. `params.columns` says whether a line is `x,y` or `y,x`. Every image of a points array
must have its text file: a file without one was not annotated and cannot be told from a field without points,
so the binding lists the annotated images instead.

`open_array` expands the urls and reads the first file's header; `read` opens the files and text files it needs.
"""
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

# series axes of a file whose pages may be read as channels: Q and I are tifffile's letters for an axis the file
# does not name. A file that names its pages Z or T is not read
PAGE_AXES = ("YX", "QYX", "IYX", "CYX")
COLUMNS = ("xy", "yx")
MAX_RADIUS = 255            # keeps a squared distance inside uint16
MAX_POINTS = 65535          # ids are uint16


class _Files:
    """The TIFF files of one array. One is open at a time, and the last page decoded is kept."""

    def __init__(self, io: IO, urls: list[str]):
        self._io, self.urls = io, urls
        self._open: tuple[int, tifffile.TiffFile, list[tifffile.TiffPage]] | None = None
        self._last: tuple[int, int, np.ndarray] | None = None
        self.pages = 0                                          # pages per file; 0 until file 0 is read
        first = self._pages(0)
        self.pages, self.page_shape = len(first), tuple(first[0].shape)
        self.dtype = np.dtype(first[0].dtype)

    def __del__(self) -> None:      # the harness never closes arrays; the open file's handle goes with the array
        if getattr(self, "_open", None) is not None:
            self._open[1].close()

    def page(self, k: int, i: int) -> np.ndarray:
        """Page i of file k as (Y, X)."""
        if self._last is None or self._last[:2] != (k, i):
            a = np.asarray(self._pages(k)[i].asarray())
            if a.shape != self.page_shape or a.dtype != self.dtype:
                raise ValueError(f"{self.urls[k]}: page {i} decoded as {a.dtype} {a.shape}, not {self.dtype} "
                                 f"{self.page_shape}")
            self._last = (k, i, a)
        return self._last[2]

    def _pages(self, k: int) -> list[tifffile.TiffPage]:
        """The pages of file k, checked against file 0. The file stays open until another is asked for."""
        if self._open is not None and self._open[0] == k:
            return self._open[2]
        if self._open is not None:
            self._open[1].close()
            self._open = None
        url = self.urls[k]
        try:
            tif = tifffile.TiffFile(self._io.open(url))
        except NeedsFetch:      # a deflated zip member, or a server without Range: stage the whole file
            tif = tifffile.TiffFile(self._io.fetch(url))
        try:
            pages = self._checked(tif, url)
        except BaseException:
            tif.close()
            raise
        self._open = (k, tif, pages)
        return pages

    def _checked(self, tif: tifffile.TiffFile, url: str) -> list[tifffile.TiffPage]:
        s = tif.series[0]
        pages = list(s.pages)
        if len(tif.series) != 1 or s.axes not in PAGE_AXES:
            raise ValueError(f"{url}: {len(tif.series)} series, the first with tifffile axes {s.axes!r} "
                             f"{tuple(s.shape)}; this snippet reads files of one series whose pages the file "
                             f"does not name as z or time ({', '.join(PAGE_AXES)}), see tiff-zip-member-3 for "
                             "stacks and tiff-channels-glob for QPTIFF")
        want = s.shape[0] if len(s.shape) == 3 else 1
        if len(pages) != want or any(p is None or tuple(p.shape) != tuple(s.shape[-2:]) for p in pages):
            raise ValueError(f"{url}: series {s.axes} {tuple(s.shape)} is not one YX page per channel")
        if self.pages and (len(pages), tuple(pages[0].shape), np.dtype(pages[0].dtype)) != (
                self.pages, self.page_shape, self.dtype):
            raise ValueError(f"{url}: {len(pages)} pages of {np.dtype(pages[0].dtype)} {tuple(pages[0].shape)} "
                             f"differ from the first file's {self.pages} pages of {self.dtype} {self.page_shape} "
                             f"({self.urls[0]}); all files of one array must agree, so bind files of another "
                             "page count or shape as their own array (a narrower glob)")
        return pages


class _Planes:
    """Axes [z][c]yx over files and pages. Subclasses give `_plane(k, c)`: page c of file k as (Y, X)."""

    def __init__(self, files: _Files, channel: int | None, dtype: np.dtype):
        self._files, self._channel = files, channel
        n = len(files.urls)
        channels = files.pages if channel is None and files.pages > 1 else 0
        self.axes = ("z" if n > 1 else "") + ("c" if channels else "") + "yx"
        self.shape: tuple[int, ...] = ((n,) if n > 1 else ()) + ((channels,) if channels else ()) + files.page_shape
        self.dtype = dtype
        self.voxel_size_nm = None       # these files carry no pixel size (see README)
        # one file per z and one page per channel: a y/x region costs its page when the file is read by range
        # (a stored member) and its file when the member is deflated and staged whole
        self.access = "plane"

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        sel = {a: region.get(a, slice(None)) for a in self.axes}
        # numpy's answer for the shape (and its IndexError for an index out of range); nothing is allocated
        shape = np.broadcast_to(np.zeros((), self.dtype), self.shape)[tuple(sel.values())].shape
        zs, cs = self._picks("z", sel), self._picks("c", sel)
        yx = (sel["y"], sel["x"])
        tail = np.broadcast_to(np.zeros((), self.dtype), self._files.page_shape)[yx].shape
        out = np.empty((len(zs), len(cs)) + tail, self.dtype)
        for i, k in enumerate(zs):
            for j, c in enumerate(cs):
                out[i, j] = self._plane(k, c)[yx]
        return out.reshape(shape)

    def _picks(self, axis: str, sel: dict[str, int | slice]) -> list[int]:
        """The indices a region asks for along z (files) or c (pages); the one file or page when the array has
        no such axis."""
        if axis not in sel:
            return [0 if axis == "z" else self._channel or 0]
        n = self.shape[self.axes.index(axis)]
        return list(range(n)[sel[axis]]) if isinstance(sel[axis], slice) else [range(n)[sel[axis]]]

    def _plane(self, k: int, c: int) -> np.ndarray:
        raise NotImplementedError


class _Images(_Planes):
    def __init__(self, files: _Files, channel: int | None):
        super().__init__(files, channel, files.dtype)

    def _plane(self, k: int, c: int) -> np.ndarray:
        return self._files.page(k, c)


class _Points(_Planes):
    """Plane z holds the points of file z's text file: 0 background, a point's 1-based line number at its pixel
    or in its disc."""

    def __init__(self, files: _Files, channel: int, io: IO, texts: list[str], columns: str, radius: int):
        super().__init__(files, channel, np.dtype("uint16"))
        self._io, self._texts, self._columns, self._radius = io, texts, columns, radius
        d = np.arange(-radius, radius + 1, dtype=np.int32) ** 2
        self._d2 = (d[:, None] + d[None, :]).astype(np.uint16)
        self._painted: tuple[int, np.ndarray] | None = None     # the last plane made: its regions are read in turn

    def _plane(self, k: int, c: int) -> np.ndarray:
        if self._painted is None or self._painted[0] != k:
            self._painted = (k, self._paint(self._rows(k)))
        return self._painted[1]

    def _rows(self, k: int) -> list[tuple[int, int]]:
        """(y, x) of every point of file k's text file, in line order."""
        url = self._texts[k]
        ny, nx = self._files.page_shape
        out: list[tuple[int, int]] = []
        for line, text in enumerate(self._io.fetch(url).read_text(encoding="utf-8-sig").splitlines(), start=1):
            if not text.strip():
                continue
            cells = text.split(",")
            if len(cells) != 2:
                raise ValueError(f"{url} line {line}: {text[:60]!r} is not two comma-separated numbers")
            a, b = (_whole(cell, url, line) for cell in cells)
            y, x = (b, a) if self._columns == "xy" else (a, b)
            if not (0 <= y < ny and 0 <= x < nx):
                raise ValueError(f"{url} line {line}: point x={x} y={y} is outside the {nx}x{ny} image "
                                 f"{self._files.urls[k]}")
            out.append((y, x))
        if len(out) > MAX_POINTS:
            raise ValueError(f"{url}: {len(out)} points exceed uint16 ids")
        return out

    def _paint(self, rows: list[tuple[int, int]]) -> np.ndarray:
        """Every point's disc (squared distance <= radius**2) filled with the point's number. A pixel inside
        several discs takes the nearest point's number; the earlier line wins a tie."""
        ny, nx = self._files.page_shape
        r = self._radius
        out = np.zeros((ny, nx), np.uint16)
        best = np.full((ny, nx), r * r + 1, np.uint16)          # squared distance to the nearest point so far
        for number, (y, x) in enumerate(rows, start=1):
            y0, y1, x0, x1 = max(y - r, 0), min(y + r + 1, ny), max(x - r, 0), min(x + r + 1, nx)
            d2 = self._d2[y0 - y + r:y1 - y + r, x0 - x + r:x1 - x + r]
            near = d2 < best[y0:y1, x0:x1]
            out[y0:y1, x0:x1][near] = number
            best[y0:y1, x0:x1][near] = d2[near]
        return out


def _whole(text: str, name: str, line: int) -> int:
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not value.is_integer():      # nan and inf are not
        raise ValueError(f"{name} line {line}: coordinate {text.strip()[:30]!r} is not a whole number of pixels")
    return int(value)


def _member(url: str) -> tuple[str, str | None]:
    """(zip url, member) of `<zip url>::<member>`; (url, None) for a plain url. The first `::` of the path."""
    path = url.find("/", url.find("://") + 3)
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


def _texts(urls: list[str], template: str, channel: int, io: IO) -> list[str]:
    """The text file url of every image: `template` with {name} as the image's file name without its extension
    and {c} as the channel, a member of the image's zip. Every one must exist."""
    out, missing = [], []
    members: dict[str, set[str]] = {}
    for url in urls:
        zip_url, member = _member(url)
        if member is None:
            raise ValueError(f"{url}: params.points looks the text files up among the members of the image's "
                             "zip, so the images must be zip members")
        if zip_url not in members:
            members[zip_url] = set(io.list(zip_url))
        name = member.rpartition("/")[2].rpartition(".")[0] or member.rpartition("/")[2]
        text = template.replace("{name}", name).replace("{c}", str(channel))
        out.append(f"{zip_url}::{text}")
        if text not in members[zip_url]:
            missing.append(text)
    if missing:
        raise ValueError(f"{len(missing)} of {len(urls)} images have no text file, e.g. {missing[:3]}: an image "
                         "without one was not annotated in this channel (or params.points is wrong). Name the "
                         "annotated images as plain urls in this array and in the raw it pairs with")
    return out


def _int(value: object, what: str, most: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (most is not None and value > most):
        raise ValueError(f"params.{what} must be a whole number from 0{'' if most is None else f' to {most}'}, "
                         f"got {value!r}")
    return value


def open_array(src: Source, io: IO) -> Array:
    p = src.params
    channel, template = p.get("channel"), p.get("points")
    if channel is not None:
        _int(channel, "channel", None)
    if template is None:
        extra = [k for k in ("columns", "radius") if p.get(k) is not None]
        if extra:
            raise ValueError(f"params {extra} apply to points only: set params.points, or drop them to read the "
                             "images")
    else:
        if not isinstance(template, str) or "{name}" not in template:
            raise ValueError("params.points must be a member name template with {name} (the image's file name "
                             f"without its extension), such as 'set/annotations/{{name}}_C{{c}}.txt', got "
                             f"{template!r}")
        if p.get("columns") not in COLUMNS:
            raise ValueError(f"params.columns must be one of {COLUMNS} when params.points is set, got "
                             f"{p.get('columns')!r}")
        radius = _int(p.get("radius", 0), "radius", MAX_RADIUS)
    urls = [u for url in src.urls for u in io.expand(url)]
    if not urls:
        raise ValueError("tiff-pages-glob-txt-points needs at least one url")
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    files = _Files(io, urls)
    if channel is not None and channel >= files.pages:
        raise ValueError(f"params.channel {channel} is out of range: {urls[0]} has {files.pages} page(s)")
    if template is None:
        return _Images(files, channel)
    if channel is None:
        if files.pages > 1 or "{c}" in template:
            raise ValueError(f"params.points needs params.channel: the files have {files.pages} page(s), and a "
                             "points array is the label of one of them")
        channel = 0
    return _Points(files, channel, io, _texts(urls, template, channel, io), p["columns"], radius)
