"""TrakEM2 area lists as binary masks on the grid of the image they were painted on, one project per z plane.

Each url is a TrakEM2 project (`.xml.gz` or `.xml`; a zip member, a plain URL or a glob) with one layer and one
image patch. The `t2_area_list` objects whose titles match `params.titles` are filled (even-odd over all the
`t2_path`s of an object, as TrakEM2 writes outlines and holes) into a yx uint8 mask, 1 inside and 0 elsewhere, in
the pixel frame of the project's patch: area-list coordinates go through the area list's transform and the inverse
of the patch's. Urls that are not projects are the images of the same names; no byte of them is read, they only
give the order of the planes, so that plane k is the project of the raw array's file k.
`open_array` reads the first project only; `read` fetches the project of each plane it is asked for.
"""
import gzip
import io as stdio
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from xml.etree import ElementTree

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

PROJECT_EXT = (".xml.gz", ".xml")
DEFAULT_TITLE = "area_list"         # what TrakEM2 calls an area list nobody named
XML_CAP = 256 * 2**20               # bytes of XML one project may inflate to
_MATRIX = re.compile(r"\s*matrix\(([^()]*)\)\s*")
_ROOT = re.compile(r"<trakem2[\s>]")
_PATH_CHARS = frozenset("MLz0123456789.- \t\r\n")


@dataclass(frozen=True)
class _Object:
    name: str                       # the title, or the project tree's type for an untitled area list
    to_image: np.ndarray            # 3x3: area-list coordinates -> pixels of the patch
    paths: tuple[str, ...]          # the d of every t2_path on the layer


@dataclass(frozen=True)
class _Project:
    frame: tuple[int, int]          # (height, width) of the patch's image
    objects: tuple[_Object, ...]


def _matrix(text: str | None, what: str) -> np.ndarray:
    """An SVG `matrix(a,b,c,d,e,f)` as the 3x3 affine [[a, c, e], [b, d, f], [0, 0, 1]]."""
    m = _MATRIX.fullmatch(text or "")
    values = [float(v) for v in m.group(1).split(",")] if m else []
    if len(values) != 6 or not np.isfinite(values).all():
        raise ValueError(f"{what}: transform {text!r} is not matrix(a,b,c,d,e,f)")
    a, b, c, d, e, f = values
    return np.array([[a, c, e], [b, d, f], [0.0, 0.0, 1.0]])


def _norm(text: str) -> str:
    """Titles and patterns compare without case and with runs of blanks as one blank."""
    return " ".join(text.split()).casefold()


def _parse(data: bytes, url: str) -> _Project:
    if data[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=stdio.BytesIO(data)) as fh:
            data = fh.read(XML_CAP + 1)
        if len(data) > XML_CAP:
            raise ValueError(f"{url}: inflates to more than {XML_CAP} bytes")
    declared = re.search(rb'encoding\s*=\s*["\']([A-Za-z0-9._-]+)["\']', data[:200])
    text = data.decode(declared.group(1).decode("ascii") if declared else "utf-8")
    start = _ROOT.search(text)      # parsed from the root element on: the DTD before it is never read
    if start is None:
        raise ValueError(f"{url}: no <trakem2> element; not a TrakEM2 project (header={data[:4]!r})")
    root = ElementTree.fromstring(text[start.start():])

    sets, layers = root.findall(".//t2_layer_set"), root.findall(".//t2_layer")
    if len(sets) != 1 or len(layers) != 1 or root.find("t2_layer_set/t2_layer") is None:
        raise ValueError(f"{url}: {len(sets)} layer sets and {len(layers)} layers; this snippet reads projects "
                         "with one layer set holding one layer (one 2D image)")
    layer = layers[0]
    patches = layer.findall("t2_patch")
    frames = {(p.get("transform"), p.get("o_width"), p.get("o_height")) for p in patches}
    if len(frames) != 1:
        raise ValueError(f"{url}: {len(patches)} image patches in {len(frames)} placements; this snippet needs "
                         "one image (or copies of it in one place) to be the pixel frame")
    if any(len(p) for p in patches):
        raise ValueError(f"{url}: the patch has a non-affine coordinate transform; not supported")
    transform, o_width, o_height = next(iter(frames))
    frame = (int(o_height or 0), int(o_width or 0))
    if min(frame) < 1:
        raise ValueError(f"{url}: patch size o_width={o_width!r} o_height={o_height!r}")
    to_patch = np.linalg.inv(_matrix(transform, f"{url}: patch"))

    project = root.find("project")
    kinds = {c.get("oid"): parent.tag for parent in (project.iter() if project is not None else ())
             for c in parent if c.get("oid")}
    objects = []
    for al in sets[0].findall("t2_area_list"):
        title = al.get("title") or DEFAULT_TITLE
        name = kinds.get(al.get("oid"), title) if title == DEFAULT_TITLE else title
        paths = []
        for area in al.findall("t2_area"):
            if area.get("layer_id") != layer.get("oid"):
                raise ValueError(f"{url}: area list {title!r} paints layer {area.get('layer_id')!r}, "
                                 f"which is not the project's one layer")
            paths += [p.get("d") or "" for p in area.findall("t2_path")]
        to_image = to_patch @ _matrix(al.get("transform"), f"{url}: area list {title!r}")
        objects.append(_Object(name, to_image, tuple(paths)))
    return _Project(frame, tuple(objects))


def _polygons(d: str, what: str) -> list[np.ndarray]:
    """The closed subpaths of one `t2_path` d ("M x y L x y ... z") as (n, 2) arrays of x, y."""
    if not _PATH_CHARS.issuperset(d) or d.split("M", 1)[0].strip():
        raise ValueError(f"{what}: path {d[:60]!r} holds more than M, L, z and plain numbers")
    out = []
    for sub in d.split("M")[1:]:
        numbers = np.array(sub.replace("L", " ").replace("z", " ").split(), dtype=np.float64)
        if numbers.size % 2:
            raise ValueError(f"{what}: path {sub[:60]!r} has an odd number of coordinates")
        if numbers.size >= 6:       # fewer than three points enclose nothing
            out.append(numbers.reshape(-1, 2))
    return out


def _paint(out: np.ndarray, polygons: list[np.ndarray]) -> None:
    """Set `out` (bool, yx) where a pixel's centre is inside the polygons by the even-odd rule. Pixel (r, c)
    covers x in [c, c+1), y in [r, r+1), its centre is (c + 0.5, r + 0.5); an outline along pixel edges, as
    TrakEM2 writes one, fills exactly the pixels it encloses."""
    if not polygons:
        return
    h, w = out.shape
    start = np.concatenate(polygons)
    end = np.concatenate([np.roll(p, -1, axis=0) for p in polygons])
    x0, y0, x1, y1 = start[:, 0], start[:, 1], end[:, 0], end[:, 1]
    first = np.clip(np.ceil(np.minimum(y0, y1) - 0.5), 0, h).astype(np.int64)   # rows whose centre line the edge
    stop = np.clip(np.ceil(np.maximum(y0, y1) - 0.5), 0, h).astype(np.int64)    # crosses: first <= r < stop
    n = stop - first
    if not n.any():
        return
    edge = np.repeat(np.arange(n.size), n)
    rows = np.arange(edge.size) - np.repeat(np.cumsum(n) - n, n) + first[edge]
    x = x0[edge] + (rows + 0.5 - y0[edge]) * (x1[edge] - x0[edge]) / (y1[edge] - y0[edge])
    cols = np.clip(np.ceil(x - 0.5), 0, w).astype(np.int64)      # the first pixel whose centre is right of x
    top, bottom, left, right = int(rows.min()), int(rows.max()) + 1, int(cols.min()), int(cols.max())
    width = right - left + 1
    crossings = np.bincount((rows - top) * width + (cols - left), minlength=(bottom - top) * width)
    inside = np.cumsum(crossings.reshape(bottom - top, width), axis=1)[:, :-1] & 1
    out[top:bottom, left:right] |= inside.astype(bool)


class _Masks:
    def __init__(self, io: IO, urls: list[str], titles: list[str], others: list[str]):
        self._io, self._urls, self._titles, self._others = io, urls, titles, others
        self._cached: tuple[int, np.ndarray] | None = None
        first = self._project(0)
        self._select(first, urls[0])    # fail at open, not at the first crop, when the params don't fit
        self._frame = first.frame
        self.shape = (len(urls), *first.frame) if len(urls) > 1 else first.frame
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.dtype = np.dtype("uint8")
        self.access = "plane"           # one project per plane, rasterised whole
        self.voxel_size_nm = None       # see README: the projects' calibration has no usable unit

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0)[y, x]
        sel = region.get("z", slice(None))
        if isinstance(sel, slice):
            return np.stack([self._plane(k)[y, x] for k in range(self.shape[0])[sel]])
        return self._plane(range(self.shape[0])[sel])[y, x]     # IndexError when out of range, like numpy

    def _project(self, k: int) -> _Project:
        url = self._urls[k]
        try:
            with self._io.open(url) as fh:
                data = fh.read()
        except NeedsFetch:              # a deflated zip member, or a server without Range
            data = self._io.fetch(url).read_bytes()
        return _parse(data, url)

    def _select(self, project: _Project, url: str) -> list[_Object]:
        """The objects of this mask. Every other object must be one the binding names in `others`."""
        chosen = []
        for obj in project.objects:
            name = _norm(obj.name)
            mine = any(fnmatchcase(name, p) for p in self._titles)
            if mine and any(fnmatchcase(name, p) for p in self._others):
                raise ValueError(f"{url}: area list {obj.name!r} matches both params.titles and params.others")
            if mine:
                chosen.append(obj)
            elif not any(fnmatchcase(name, p) for p in self._others):
                raise ValueError(f"{url}: area list {obj.name!r} matches neither params.titles nor "
                                 "params.others; say which it is, so that no object is dropped unseen")
        return chosen

    def _plane(self, k: int) -> np.ndarray:
        if self._cached is not None and self._cached[0] == k:
            return self._cached[1]
        url = self._urls[k]
        project = self._project(k)
        if project.frame != self._frame:
            raise ValueError(f"{url}: image of {project.frame} (y, x) differs from the first project's "
                             f"{self._frame}; all planes of one array must agree")
        mask = np.zeros(self._frame, dtype=bool)
        for obj in self._select(project, url):
            polygons = []
            for d in obj.paths:
                for p in _polygons(d, f"{url}: area list {obj.name!r}"):
                    xy1 = np.column_stack([p, np.ones(len(p))]) @ obj.to_image.T
                    polygons.append(xy1[:, :2])
            _paint(mask, polygons)      # even-odd within one object, union between objects
        plane = mask.astype(np.uint8)
        self._cached = (k, plane)
        return plane


def _stem(url: str) -> tuple[str, bool]:
    """A url's file name without its extension, and whether it is a TrakEM2 project."""
    name = url.rsplit("::", 1)[-1].rsplit("/", 1)[-1]
    for ext in PROJECT_EXT:
        if name.lower().endswith(ext):
            return name[:-len(ext)], True
    return name.rsplit(".", 1)[0], False


def _planes(urls: list[str], image_suffix: str) -> list[str]:
    """The project urls in plane order: the order of the image urls when there are any, else as given."""
    projects, images = {}, []
    for url in urls:
        stem, is_project = _stem(url)
        if is_project:
            if stem in projects:
                raise ValueError(f"two projects are named {stem!r}: {projects[stem]} and {url}")
            projects[stem] = url
        else:
            if not stem.endswith(image_suffix):
                raise ValueError(f"{url}: the name does not end with params.image_suffix {image_suffix!r}")
            images.append(stem[:len(stem) - len(image_suffix)])
    if not projects:
        raise ValueError("no url is a TrakEM2 project (.xml.gz or .xml)")
    if not images:
        return list(projects.values())
    if len(set(images)) != len(images) or set(images) != set(projects):
        lone = sorted(set(images) ^ set(projects))
        raise ValueError(f"images and projects do not pair one to one by name; unpaired or repeated: {lone[:10]}")
    return [projects[stem] for stem in images]


def _patterns(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"params.{key} must be a list of title patterns, got {value!r}")
    return [_norm(v) for v in value]


def open_array(src: Source, io: IO) -> Array:
    titles = _patterns(src.params.get("titles"), "titles")
    others = _patterns(src.params.get("others", []), "others")
    if not titles:
        raise ValueError("params.titles is empty: name the area lists this mask holds")
    image_suffix = src.params.get("image_suffix", "")
    if not isinstance(image_suffix, str):
        raise ValueError(f"params.image_suffix must be a string, got {image_suffix!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return _Masks(io, _planes(urls, image_suffix), titles, others)
