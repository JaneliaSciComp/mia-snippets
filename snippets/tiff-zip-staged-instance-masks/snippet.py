"""2-D TIFF members of a zip that is staged whole with `io.fetch(<zip url>)` and read from the local file: one
member as it is, or (`instances: true`) every one-object binary mask the urls name merged into one instance-id
plane, optionally only the objects of one class (`classes`, `class_id`).

The other zip snippets read a member through `io.open` / `io.fetch` on the `<zip>::<member>` url, which needs
the zip's central directory by range and so the zip's size. A server that sends no Content-Length (DSpace
bitstreams, e.g. OPARA) fails both with NeedsFetch; only `io.fetch` of the zip itself works there."""
import re
import zipfile
from fnmatch import fnmatchcase
from io import BytesIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

GLOB_CHARS = "*?["
MAX_IDS = np.iinfo(np.uint16).max


def _split(url: str) -> tuple[str, str]:
    """(zip url, member or glob) of `<zip url>::<member>`; the "::" is looked for in the path, as the harness does."""
    path = url.find("/", url.find("://") + 3)
    i = url.find("::", path) if path >= 0 else -1
    if i < 0 or not url[i + 2:]:
        raise ValueError(f"expected <zip url>::<member or glob>, got {url!r}")
    return url[:i], url[i + 2:]


def _natural(name: str) -> list[int | str]:
    """Sort key under which digit runs compare as numbers: masks/0/2.tif before masks/0/10.tif."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _members(names: list[str], patterns: list[str]) -> list[str]:
    """The zip's file members the patterns name (a plain name, or an fnmatch glob whose `*` crosses `/`), once
    each, in natural order."""
    files = [n for n in names if not n.endswith("/")]
    found: set[str] = set()
    for pattern in patterns:
        if any(ch in pattern for ch in GLOB_CHARS):
            matched = [n for n in files if fnmatchcase(n, pattern)]
        else:
            matched = [pattern] if pattern in files else []
        if not matched:
            raise KeyError(f"no member of the zip matches {pattern!r}")
        found.update(matched)
    return sorted(found, key=_natural)


def _layout(tif: tifffile.TiffFile, name: str) -> tuple[tuple[int, int], np.dtype]:
    """(shape, dtype) of a single-plane grey TIFF; anything else raises."""
    s = tif.series[0]
    axes = "".join(a for a, n in zip(s.axes, s.shape) if n > 1 or a in "YX")
    if len(tif.series) != 1 or axes != "YX":
        raise ValueError(f"{name!r}: {len(tif.series)} series, axes {s.axes!r} {tuple(s.shape)}; this snippet "
                         "reads one 2-D grey (YX) plane per member")
    sizes = dict(zip(s.axes, s.shape))
    return (int(sizes["Y"]), int(sizes["X"])), np.dtype(s.dtype)


class _Plane:
    """One yx plane held by a local zip: a member's own pixels, or instance ids merged from one mask per member.
    Decoded on the first `read` and kept, so every later crop of it costs no inflate."""

    axes = "yx"
    access = "file"            # the zip was staged whole at open
    voxel_size_nm = None       # plain TIFF resolution tags describe print size, not pixels

    def __init__(self, archive: zipfile.ZipFile, members: list[str], instances: bool, keep: np.ndarray | None):
        self._zip, self._members, self._instances, self._keep = archive, members, instances, keep
        self._plane: np.ndarray | None = None
        with archive.open(members[0]) as fh, tifffile.TiffFile(fh) as tif:   # the header only: no pixels yet
            self.shape, self._dtype = _layout(tif, members[0])
        if instances and self._dtype.kind not in "biu":
            raise ValueError(f"{members[0]!r} is {self._dtype.name}; a mask is boolean or integer")
        self.dtype = np.dtype("uint16") if instances else self._dtype

    def __del__(self):          # the harness never closes arrays; the staged zip's handle goes with the array
        self._zip.close()

    def _pixels(self, name: str) -> np.ndarray:
        with tifffile.TiffFile(BytesIO(self._zip.read(name))) as tif:   # zipfile checks the member's CRC32
            shape, dtype = _layout(tif, name)
            if shape != self.shape or dtype != self._dtype:
                raise ValueError(f"{name!r} is {dtype.name} {shape}, not {self._dtype.name} {self.shape} as "
                                 f"{self._members[0]!r}")
            return tif.asarray(series=0).reshape(shape)

    def _load(self) -> np.ndarray:
        if not self._instances:
            return self._pixels(self._members[0])
        ids = np.zeros(self.shape, np.uint16)
        for k, name in enumerate(self._members):       # ascending: where masks overlap, the highest number stays
            if self._keep is None or self._keep[k]:
                ids[self._pixels(name) != 0] = k + 1
        return ids

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._plane is None:
            self._plane = self._load()
        return self._plane[region.get("y", slice(None)), region.get("x", slice(None))].copy()


def _class_ids(archive: zipfile.ZipFile, member: str, count: int) -> np.ndarray:
    """The 1-D integer .npy `member`: one class id per mask, in the masks' natural order."""
    if member not in archive.namelist():
        raise KeyError(f"params.classes: no member {member!r} in the zip")
    ids = np.load(BytesIO(archive.read(member)), allow_pickle=False)
    if ids.ndim != 1 or ids.dtype.kind not in "iu" or len(ids) != count:
        raise ValueError(f"params.classes {member!r} is {ids.dtype.name} {ids.shape}; expected one integer per "
                         f"mask, {count} of them")
    return ids


def open_array(src: Source, io: IO) -> Array:
    params = src.params
    instances, classes, class_id = params.get("instances", False), params.get("classes"), params.get("class_id")
    if not isinstance(instances, bool):
        raise ValueError(f"params.instances must be true or false, got {instances!r}")
    if (classes is None) != (class_id is None) or (classes is not None and not instances):
        raise ValueError("params.classes and params.class_id go together, and only with instances: true")
    if classes is not None and (not isinstance(classes, str) or isinstance(class_id, bool)
                                or not isinstance(class_id, int)):
        raise ValueError(f"params.classes must be a member name and params.class_id an integer, got {classes!r} "
                         f"and {class_id!r}")
    if not src.urls:
        raise ValueError("tiff-zip-staged-instance-masks needs at least one <zip url>::<member or glob> url")
    parts = [_split(u) for u in src.urls]
    zip_urls = sorted({z for z, _ in parts})
    if len(zip_urls) != 1:
        raise ValueError(f"all urls of one array must be members of one zip, got {zip_urls}")
    archive = zipfile.ZipFile(io.fetch(zip_urls[0]))       # the whole zip, staged once per job and cached
    members = _members(archive.namelist(), [m for _, m in parts])
    if not instances and len(members) != 1:
        raise ValueError(f"the urls name {len(members)} members; without instances: true this snippet reads "
                         f"exactly one (first: {members[0]!r})")
    if len(members) > MAX_IDS:
        raise ValueError(f"{len(members)} masks do not fit uint16 instance ids")
    keep = None if classes is None else _class_ids(archive, classes, len(members)) == class_id
    return _Plane(archive, members, instances, keep)
