"""One TIFF that is a member of a gzip-compressed tar archive, as its own axes (`yx` for a single greyscale page),
or, with `channels`, the TIFFs of one field that differ only in a channel token of their file name (e.g. `DAPI`
and `CY5`), stacked as `cyx` in the order the binding lists the tokens.

A tar.gz has no index and cannot be read by range, so a member is reached by inflating the archive from its first
byte: the compressed bytes up to the last wanted member's end come through `io.open` (range requests, cached in
blocks, counted against the job's cap) and nothing behind it is asked for. The archive is never fetched whole.
Each member is read from its own bytes, page by page: no other file is ever looked for, so a multi-file OME-TIFF
or its companion metadata file is not followed."""
import fnmatch
import io as stdio
import tarfile
from typing import BinaryIO, Iterator

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

READ = 8 << 20      # compressed bytes asked of io at a time: one range request
SHOWN = 8           # member names quoted when no member matches
AXIS = {"Y": "y", "X": "x", "S": "c"}   # tifffile page axis letter -> czyx
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form


def _is_tiff(name: str) -> bool:
    return name.lower().endswith((".tif", ".tiff"))


def _base(name: str) -> tuple[str, str]:
    """A member name as (directory with its trailing slash, file name)."""
    cut = name.rfind("/") + 1
    return name[:cut], name[cut:]


def _walk(io: IO, url: str, staged: bool) -> Iterator[tuple[str, BinaryIO]]:
    """(name, stream) of each regular file of the archive, in archive order. A stream is readable only until the
    next one is asked for. `staged`: from the archive fetched whole, not by range reads."""
    raw = open(io.fetch(url), "rb") if staged else io.open(url)
    with raw, tarfile.open(fileobj=raw, mode="r|gz", bufsize=READ) as tar:
        for info in tar:
            if info.isfile():
                yield info.name, tar.extractfile(info)


def _load(io: IO, url: str, member: str | None, tokens: list[str], staged: bool) -> list[tuple[str, bytes]]:
    """(name, bytes) of the members that form the array, in channel order: the first member that `member` and
    `tokens[0]` select, and with tokens its partners, the same name with each other token in place of the first."""
    names: list[str] | None = None
    seen: set[str] = set()
    shown: list[str] = []
    got: dict[str, bytes] = {}
    for name, f in _walk(io, url, staged):
        seen.add(name)
        if len(shown) < SHOWN and _is_tiff(name):
            shown.append(name)
        if names is None and (fnmatch.fnmatchcase(name, member) if member is not None else _is_tiff(name)) \
                and (not tokens or tokens[0] in _base(name)[1]):
            folder, file = _base(name)
            cut = file.rfind(tokens[0]) if tokens else 0
            names = [folder + file[:cut] + t + file[cut + len(tokens[0]):] for t in tokens] if tokens else [name]
        if names is not None:
            if name in names:
                got[name] = f.read()
            if seen.issuperset(names):
                break
    if names is None:
        what = ".tif/.tiff member" if member is None else f"member matching {member!r}"
        raise ValueError(f"{url} has no {what}" + (f" with {tokens[0]!r} in its file name" if tokens else "") +
                         f"; its first TIFF members: {shown}")
    absent = [n for n in names if n not in seen]
    if absent:
        raise ValueError(f"{url} has the member {names[0]!r} but not {absent[0]!r}, the same field with "
                         f"{tokens[names.index(absent[0])]!r} in place of {tokens[0]!r}")
    if len(got) < len(names):   # a partner lies before the first channel's member: a second pass, from io's cache
        for name, f in _walk(io, url, staged):
            if name in names and name not in got:
                got[name] = f.read()
                if len(got) == len(names):
                    break
    return [(n, got[n]) for n in names]


def _open(name: str, data: bytes) -> tifffile.TiffFile:
    try:
        return tifffile.TiffFile(stdio.BytesIO(data))
    except tifffile.TiffFileError as e:
        raise ValueError(f"member {name!r} is not a TIFF file: {e}") from None


def _layout(name: str, data: bytes, axes: str | None) -> tuple[str, tuple[int, ...], np.dtype]:
    """(axes, shape, dtype) of one member: its pages, all of one shape and dtype, stacked along z when there are
    several. Size-1 axes other than y and x are dropped."""
    with _open(name, data) as tif:
        first = tif.pages[0]
        shapes = {(tuple(p.shape), np.dtype(p.dtype).str) for p in tif.pages}
        if len(shapes) != 1:
            raise ValueError(f"member {name!r} has pages of several shapes or dtypes ({sorted(shapes)[:4]}); "
                             "one shape per file is read")
        letters = ("I" if len(tif.pages) > 1 else "") + first.axes
        shape = ((len(tif.pages),) if len(tif.pages) > 1 else ()) + tuple(first.shape)
        keep = [i for i, (a, n) in enumerate(zip(letters, shape)) if n > 1 or a in "YX"]
        shape = tuple(shape[i] for i in keep)
        if axes is None:
            axes = "".join({"I": "z", **AXIS}.get(letters[i], "?") for i in keep)
            if "?" in axes or len(set(axes)) != len(axes):
                raise ValueError(f"member {name!r}: page axes {first.axes!r} {tuple(first.shape)} x "
                                 f"{len(tif.pages)} pages do not map to czyx; set params.axes")
        if len(axes) != len(shape):
            raise ValueError(f"params.axes {axes!r} has {len(axes)} letters for the {len(shape)} axes {shape} "
                             f"of member {name!r}")
        return axes, shape, np.dtype(first.dtype)


def _pixels(name: str, data: bytes, shape: tuple[int, ...]) -> np.ndarray:
    with _open(name, data) as tif:
        return np.stack([p.asarray() for p in tif.pages]).reshape(shape)


def _voxel_size(name: str, data: bytes, axes: str) -> dict[str, float] | None:
    """From OME PhysicalSize* in the member's own OME-XML, or ImageJ unit/spacing + X/YResolution; None when the
    file doesn't say (as for an OME-XML that only points to a companion file)."""
    out: dict[str, float] = {}
    with _open(name, data) as tif:
        page, imagej = tif.pages[0], tif.imagej_metadata if tif.is_imagej else None
        description = page.description if "ImageDescription" in page.tags else ""
        if description.lstrip().startswith("<") and "OME" in description[:1000]:
            try:
                img = tifffile.xml2dict(description).get("OME", {}).get("Image")
                px = (img[0] if isinstance(img, list) else img or {}).get("Pixels") or {}
            except Exception:       # an OME-XML that does not parse says nothing about the pixel size
                px = {}
            for a in "zyx":
                v, unit = px.get(f"PhysicalSize{a.upper()}"), px.get(f"PhysicalSize{a.upper()}Unit", "µm")
                if a in axes and v and unit in NM:
                    out[a] = float(v) * NM[unit]
        elif imagej and imagej.get("unit") in NM:
            scale = NM[imagej["unit"]]
            for a, tag in (("y", "YResolution"), ("x", "XResolution")):
                num, den = page.tags[tag].value if tag in page.tags else (0, 0)
                if num:
                    out[a] = den / num * scale
            if "z" in axes and imagej.get("spacing"):
                out["z"] = float(imagej["spacing"]) * scale
    return out or None


class _Members:
    def __init__(self, files: list[tuple[str, bytes]], axes: str | None, stacked: bool):
        layouts = [_layout(name, data, axes) for name, data in files]
        inner, shape, dtype = layouts[0]
        for (name, _), layout in zip(files, layouts):
            if layout != layouts[0]:
                raise ValueError(f"member {name!r} is {layout[0]} {layout[1]} {layout[2]}, but {files[0][0]!r} is "
                                 f"{inner} {shape} {dtype}: the channels of one field must agree")
        if stacked and "c" in inner:
            raise ValueError(f"params.channels stacks the members along c, but {files[0][0]!r} already has a "
                             f"channel axis ({inner} {shape})")
        self._files: list[tuple[str, bytes]] | None = files
        self._inner, self._stacked = shape, stacked
        self._data: np.ndarray | None = None
        self.axes = "c" + inner if stacked else inner
        self.shape = (len(files),) + shape if stacked else shape
        self.dtype = dtype.newbyteorder("=")
        self.voxel_size_nm = _voxel_size(*files[0], inner)
        self.access = "file"    # any read costs the members whole: the stream has no index to seek by

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if self._data is None:      # decoded at the first read, then kept; the encoded bytes are let go
            planes = [_pixels(name, data, self._inner) for name, data in self._files or []]
            self._data = (np.stack(planes) if self._stacked else planes[0]).astype(self.dtype, copy=False)
            self._files = None
        return self._data[tuple(region.get(a, slice(None)) for a in self.axes)].copy()


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"tiff-targz-members reads the members of one archive, got {len(src.urls)} urls")
    member, tokens, axes = src.params.get("member"), src.params.get("channels"), src.params.get("axes")
    if member is not None and not (isinstance(member, str) and member):
        raise ValueError(f"params.member must be a member's name in the archive or a pattern for it, got {member!r}")
    if axes is not None and not isinstance(axes, str):
        raise ValueError(f"params.axes must be a string of letters from czyx, got {axes!r}")
    if tokens is not None:
        if not isinstance(tokens, list) or len(tokens) < 2 or len(set(map(str, tokens))) != len(tokens) or \
                not all(isinstance(t, str) and t and "/" not in t for t in tokens):
            raise ValueError("params.channels must be two or more different strings, the channel tokens of the "
                             f"file names, got {tokens!r}")
        if member is not None and not any(c in member for c in "*?[") and tokens[0] not in _base(member)[1]:
            raise ValueError(f"params.member {member!r} does not have params.channels[0] {tokens[0]!r} in its "
                             "file name")
    try:
        files = _load(io, src.urls[0], member, tokens or [], staged=False)
    except NeedsFetch:      # a server without Range or Content-Length: the archive has to be staged whole
        files = _load(io, src.urls[0], member, tokens or [], staged=True)
    return _Members(files, axes, tokens is not None)
