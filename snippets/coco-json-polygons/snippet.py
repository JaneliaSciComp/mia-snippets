"""COCO segmentation JSON as label planes: the polygons a COCO file holds for each image the urls name, rasterised
on the grid that file states for the image, one image per z plane.

The urls are the annotated IMAGES (plain urls, zip members or globs), the same urls and order as the raw array
they label, so plane k of both is the k-th image. No image is read. Each image's annotations come from the COCO
file `params.annotations` names relative to the image's folder (default `_annotations.coco.json` beside it, the
Roboflow export layout): its `images` entry whose `file_name` ends in the image's name gives the grid (`height`,
`width`), and the `annotations` with that entry's `image_id` give the polygons (`segmentation`: a list of rings
`[x0, y0, x1, y1, ...]` in pixels of the image). Pixel (y, x) holds, for the annotation enclosing its centre, its
1-based number among the image's annotations (ids: instance) or its COCO category id (ids: semantic), 0 where
none does. `open_array` reads the COCO files; `read` only draws.
"""
import json
import math

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

IDS = ("instance", "semantic")
ANNOTATIONS = "_annotations.coco.json"

Ring = tuple[tuple[float, ...], tuple[float, ...]]      # xs, ys
Shape = tuple[int, ...]                                 # one object: the rings of one annotation
Plane = tuple[str, tuple[int, int], tuple[tuple[int, Shape], ...]]      # url, (Y, X), (value, shape) in file order


def _bytes(io: IO, url: str) -> bytes:
    try:
        with io.open(url) as f:
            return f.read()
    except NeedsFetch:          # a deflated member, or a server without Range: stage the whole file
        return io.fetch(url).read_bytes()


def _split(url: str) -> tuple[str, list[str]]:
    """(root, path steps) of a file url: the root is `<zip url>::` for a member, `<scheme>://<host>/` otherwise."""
    root, sep, path = url.rpartition("::")
    if sep:
        return root + sep, path.split("/")
    scheme, sep, rest = url.partition("://")
    host, _, path = rest.partition("/")
    return f"{scheme}{sep}{host}/", path.split("/")


def _beside(url: str, relative: str) -> str:
    """The url of `relative` (a path with `..` steps allowed) seen from the folder of the file `url`."""
    root, steps = _split(url)
    steps = [s for s in steps[:-1] if s]
    for step in relative.split("/"):
        if step == "..":
            if not steps:
                raise ValueError(f"params.annotations {relative!r} leaves the top folder of {url}")
            steps.pop()
        elif step not in ("", "."):
            steps.append(step)
    return root + "/".join(steps)


def _number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _rings(annotation: dict, where: str) -> Shape:
    """The rings of one annotation's `segmentation`."""
    seg = annotation.get("segmentation")
    if isinstance(seg, dict):
        raise ValueError(f"{where}: the segmentation is a run-length mask (RLE), not polygons; this snippet "
                         "reads polygon segmentations only")
    if not isinstance(seg, list) or not seg:
        raise ValueError(f"{where}: no polygon in `segmentation` ({seg!r}); a box-only annotation is not a "
                         "segmentation and this snippet does not fill boxes")
    out = []
    for ring in seg:
        if not (isinstance(ring, list) and len(ring) >= 6 and len(ring) % 2 == 0 and all(map(_number, ring))):
            raise ValueError(f"{where}: a segmentation ring must be [x0, y0, x1, y1, ...] with 3 or more points "
                             f"of finite numbers, got {str(ring)[:80]}")
        out.append((tuple(float(v) for v in ring[0::2]), tuple(float(v) for v in ring[1::2])))
    return tuple(out)


class _Coco:
    """One parsed COCO file: its categories, its images by file name, and its annotations by image id."""

    def __init__(self, data: bytes, url: str):
        self.url = url
        try:
            doc = json.loads(data.decode("utf-8-sig"))
        except ValueError as e:
            raise ValueError(f"{url}: not JSON: {e}") from None
        if not (isinstance(doc, dict) and all(isinstance(doc.get(k), list) and all(isinstance(e, dict) for e in doc[k])
                                              for k in ("images", "annotations", "categories"))):
            raise ValueError(f"{url}: not a COCO file: it needs the lists `images`, `annotations` and `categories`")
        self.categories: dict[int, str] = {}
        for c in doc["categories"]:
            if not (isinstance(c.get("id"), int) and isinstance(c.get("name"), str)) or c["id"] in self.categories:
                raise ValueError(f"{url}: a category needs an integer `id` of its own and a `name`, got {c!r}")
            self.categories[c["id"]] = c["name"]
        self.images: dict[str, list[dict]] = {}
        for im in doc["images"]:
            if not isinstance(im.get("file_name"), str):
                raise ValueError(f"{url}: an `images` entry has no `file_name`: {str(im)[:120]}")
            self.images.setdefault(im["file_name"].rpartition("/")[2], []).append(im)
        self.annotations: dict[object, list[dict]] = {}
        for a in doc["annotations"]:
            self.annotations.setdefault(a.get("image_id"), []).append(a)

    def chosen(self, names: list[str] | None) -> set[int]:
        """The ids of the categories named (all of them without names); a name must be exactly one category's."""
        if names is None:
            return set(self.categories)
        ids = set()
        for name in names:
            found = [i for i, n in self.categories.items() if n == name]
            if len(found) != 1:
                raise ValueError(f"{self.url}: params.categories names {name!r}, which is the name of "
                                 f"{len(found)} categories; the file has {sorted(self.categories.items())}")
            ids.add(found[0])
        return ids

    def plane(self, url: str, names: list[str] | None, ids: str) -> Plane:
        """The grid and the (value, rings) of every annotation of a chosen category of the image `url`."""
        name = _split(url)[1][-1]
        entries = self.images.get(name, [])
        if len(entries) != 1:
            raise ValueError(f"{url}: {len(entries)} `images` entries of {self.url} have the file name {name!r} "
                             f"(it lists {sum(map(len, self.images.values()))} images); a plane needs exactly one")
        im = entries[0]
        if not all(isinstance(im.get(k), int) and not isinstance(im[k], bool) and im[k] > 0
                   for k in ("height", "width")):
            raise ValueError(f"{self.url}: the entry of {name!r} states no `height` and `width` in pixels")
        chosen, shapes = self.chosen(names), []
        for a in self.annotations.get(im.get("id"), []):
            category = a.get("category_id")
            if category not in self.categories:
                raise ValueError(f"{self.url}: annotation {a.get('id')!r} of {name!r} has category_id {category!r}, "
                                 f"which is none of the file's categories {sorted(self.categories.items())}")
            if category not in chosen:
                continue
            value = len(shapes) + 1 if ids == "instance" else category
            if not 1 <= value <= 65535:
                raise ValueError(f"{self.url}: {name!r} needs the id {value} "
                                 f"({'its annotation number' if ids == 'instance' else 'a COCO category id'}), "
                                 "outside 1..65535: 0 is background and the array is uint16")
            where = f"{self.url}: annotation {a.get('id')!r} of {name!r}"
            shape = _rings(a, where)
            xs, ys = [x for ring in shape for x in ring[0]], [y for ring in shape for y in ring[1]]
            if max(xs) < 0 or min(xs) > im["width"] or max(ys) < 0 or min(ys) > im["height"]:
                raise ValueError(f"{where}: it spans x {min(xs):g}..{max(xs):g}, y {min(ys):g}..{max(ys):g}, wholly "
                                 f"outside the {im['width']}x{im['height']} image the file states; is this the "
                                 "image the polygons were drawn on?")
            shapes.append((value, shape))
        return url, (im["height"], im["width"]), tuple(shapes)


def _fill(shape: Shape, grid: tuple[int, int]) -> tuple[int, int, np.ndarray]:
    """(y0, x0, mask) of one annotation inside the grid: mask[j, i] is True when the centre (x0 + i + 0.5,
    y0 + j + 0.5) of pixel (y0 + j, x0 + i) is inside one of its rings, each closed and filled by the even-odd
    rule. The mask spans the rings' bounding box, cut to the grid."""
    xs, ys = [x for ring in shape for x in ring[0]], [y for ring in shape for y in ring[1]]
    x0, x1 = max(0, math.floor(min(xs))), min(grid[1], math.ceil(max(xs)))
    y0, y1 = max(0, math.floor(min(ys))), min(grid[0], math.ceil(max(ys)))
    mask = np.zeros((max(0, y1 - y0), max(0, x1 - x0)), bool)
    cx = np.arange(x0, x0 + mask.shape[1]) + 0.5
    for xs, ys in shape:
        inside = np.zeros_like(mask)
        for xa, ya, xb, yb in zip(xs, ys, xs[1:] + xs[:1], ys[1:] + ys[:1]):
            # the rows whose centre the edge spans, of those in the mask
            r0, r1 = max(y0, math.ceil(min(ya, yb) - 0.5)), min(y1, math.ceil(max(ya, yb) - 0.5))
            if r0 < r1:
                cross = xa + (xb - xa) * (np.arange(r0, r1) + 0.5 - ya) / (yb - ya)
                inside[r0 - y0:r1 - y0] ^= cx[None, :] < cross[:, None]
        mask |= inside
    return y0, x0, mask


def _paint(plane: Plane) -> np.ndarray:
    """The plane's annotations painted with their values; the ones covering more pixels first, so where two
    overlap the smaller is on top (file order breaks ties). 0 is background."""
    _, grid, shapes = plane
    out = np.zeros(grid, np.uint16)
    masks = []
    for value, shape in shapes:
        y0, x0, mask = _fill(shape, grid)
        masks.append((int(mask.sum()), value, y0, x0, np.packbits(mask, axis=1), mask.shape[1]))
    for _, value, y0, x0, packed, nx in sorted(masks, key=lambda m: -m[0]):
        mask = np.unpackbits(packed, axis=1, count=nx).view(bool)
        out[y0:y0 + mask.shape[0], x0:x0 + nx][mask] = value
    return out


class _Planes:
    """One image per z plane (no z axis when there is one image), uint16 ids on the image's grid."""

    def __init__(self, planes: list[Plane]):
        self._planes = planes
        self._last: tuple[int, np.ndarray] | None = None
        self._grid = planes[0][1]
        self.axes = "zyx" if len(planes) > 1 else "yx"
        self.shape = ((len(planes),) if len(planes) > 1 else ()) + self._grid
        self.dtype = np.dtype("uint16")
        self.access = "plane"           # a y/x region costs the drawing of its whole image
        self.voxel_size_nm = None       # a COCO file states no pixel size

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        inner = (region.get("y", slice(None)), region.get("x", slice(None)))
        if "z" not in self.axes:
            return self._plane(0)[inner]
        z = region.get("z", slice(None))
        if not isinstance(z, slice):
            return self._plane(range(len(self._planes))[z])[inner]      # IndexError when out of range, like numpy
        planes = [self._plane(k)[inner] for k in range(len(self._planes))[z]]
        if not planes:
            return np.zeros((0,) + np.zeros(self._grid, bool)[inner].shape, self.dtype)
        return np.stack(planes)

    def _plane(self, k: int) -> np.ndarray:
        if self._last is None or self._last[0] != k:
            self._last = (k, _paint(self._planes[k]))
        return self._last[1]


def open_array(src: Source, io: IO) -> Array:
    annotations, names = src.params.get("annotations", ANNOTATIONS), src.params.get("categories")
    ids = src.params.get("ids", "instance")
    if ids not in IDS:
        raise ValueError(f"params.ids must be one of {IDS}, got {ids!r}")
    if not isinstance(annotations, str) or not annotations.rpartition("/")[2] or "::" in annotations \
            or annotations.startswith("/"):
        raise ValueError("params.annotations must be the path of the COCO file relative to the folder of each "
                         f"image, got {annotations!r}")
    if names is not None and not (isinstance(names, list) and names and len(set(names)) == len(names)
                                  and all(isinstance(n, str) and n for n in names)):
        raise ValueError(f"params.categories must be a list of distinct COCO category names, got {names!r}")
    urls = [u for url in src.urls for u in io.expand(url)]
    if not urls:
        raise ValueError("the urls match no file")
    if len(set(urls)) != len(urls):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    files: dict[str, _Coco] = {}
    planes = []
    for url in urls:
        where = _beside(url, annotations)
        if where == url:
            raise ValueError(f"{url} is the COCO file itself; the urls are the annotated images")
        if where not in files:
            files[where] = _Coco(_bytes(io, where), where)
        planes.append(files[where].plane(url, names, ids))
    if ids == "semantic":       # one value must mean one category in every plane
        meanings = {tuple(sorted((i, f.categories[i]) for i in f.chosen(names))) for f in files.values()}
        if len(meanings) != 1:
            raise ValueError(f"the COCO files give the categories different ids: {sorted(meanings)}; with ids: "
                             "semantic a value would mean different things in different planes")
    for url, grid, _ in planes:
        if grid != planes[0][1]:
            raise ValueError(f"{url}: its COCO entry states {grid} (y, x), the first image's states "
                             f"{planes[0][1]}; all images of one array must agree")
    return _Planes(planes)
