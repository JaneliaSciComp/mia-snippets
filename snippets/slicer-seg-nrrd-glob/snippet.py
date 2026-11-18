"""3D Slicer segmentation files (`.seg.nrrd`: an NRRD labelmap whose header names each label value's segment),
one 2D plane per file, from zip members or directory files chosen by glob (or listed one by one), stacked
along z. The values returned are class ids fixed by the binding, not the values a file stores: `segments`
lists the segment names in id order, and each file's stored values are renamed through its own header, so two
files that number the same segments differently still give the same ids. 0 (no segment) stays 0.

A plane that holds the value of a segment `segments` does not list raises, as does a stored value no segment
names; nothing is dropped silently. A segment the header names but no pixel has (an empty segment) needs no
entry. Files are opened lazily: `open_array` reads the first file's header only; `read` fetches and inflates
the files it needs, one file per z plane.
"""
import re
import zlib

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

TYPES = {"i1": ("signed char", "int8", "int8_t"),
         "u1": ("uchar", "unsigned char", "uint8", "uint8_t"),
         "i2": ("short", "short int", "signed short", "signed short int", "int16", "int16_t"),
         "u2": ("ushort", "unsigned short", "unsigned short int", "uint16", "uint16_t"),
         "i4": ("int", "signed int", "int32", "int32_t"),
         "u4": ("uint", "unsigned int", "uint32", "uint32_t")}
DTYPE = {name: code for code, names in TYPES.items() for name in names}     # NRRD type -> numpy code
SEGMENT = re.compile(r"Segment(\d+)_(Name|LabelValue|Layer)")
NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
VECTOR = re.compile(rf"\(\s*({NUMBER})\s*,\s*({NUMBER})\s*,\s*({NUMBER})\s*\)")


class _Header:
    """What one file's header says: the plane's size, how its values are stored, and the segment each names."""

    def __init__(self, data: bytes, url: str):
        if data[:7] != b"NRRD000":
            raise ValueError(f"{url}: not an NRRD file: header={data[:8]!r}")
        ends = [(i, len(sep)) for sep in (b"\n\n", b"\r\n\r\n") if (i := data.find(sep)) >= 0]
        if not ends:
            raise ValueError(f"{url}: the NRRD header has no blank line after it, so the file holds no data "
                             "(a detached header); this snippet reads attached data only")
        end, gap = min(ends)
        self.offset = end + gap
        fields: dict[str, str] = {}     # `field: value`
        keys: dict[str, str] = {}       # `key:=value`
        for line in data[:end].decode("utf-8", "replace").splitlines()[1:]:
            if line.startswith("#") or not line.strip():
                continue
            pair, field = line.find(":="), line.find(": ")
            if pair >= 0 and (field < 0 or pair < field):
                keys[line[:pair]] = line[pair + 2:]
            elif field >= 0:
                fields[line[:field].strip().lower()] = line[field + 2:].strip()
            else:
                raise ValueError(f"{url}: NRRD header line {line[:60]!r} is neither `field: value` nor `key:=value`")
        self._url, self._fields = url, fields
        self.size = self._size()                    # (ny, nx)
        self.dtype = self._dtype()
        self.encoding = fields.get("encoding", "").lower()
        if self.encoding not in ("raw", "gzip", "gz"):
            raise ValueError(f"{url}: NRRD encoding {fields.get('encoding')!r}; this snippet reads raw and gzip")
        for name in ("data file", "datafile", "line skip", "lineskip", "byte skip", "byteskip"):
            if fields.get(name, "0") != "0":
                raise ValueError(f"{url}: NRRD field {name!r} = {fields[name]!r} is not supported; this snippet "
                                 "reads data that follows the header directly")
        self.names = self._names(keys)              # stored value -> segment name

    def _size(self) -> tuple[int, int]:
        f, url = self._fields, self._url
        try:
            sizes = [int(s) for s in f["sizes"].split()]
            dimension = int(f["dimension"])
        except (KeyError, ValueError):
            raise ValueError(f"{url}: the NRRD header has no readable `dimension` and `sizes`") from None
        if dimension != 3 or len(sizes) != 3 or sizes[2] != 1 or min(sizes) < 1:
            raise ValueError(f"{url}: NRRD dimension {dimension}, sizes {sizes}; this snippet reads one plane per "
                             "file, sizes `nx ny 1` (4 dimensions are a segmentation with overlapping layers, a "
                             "third size above 1 is a volume)")
        if "space directions" in f:
            # the index grid is returned as stored; a rotated, mirrored or sheared grid would not be the image's
            rows = [tuple(float(v) for v in m) for m in VECTOR.findall(f["space directions"])]
            if len(rows) != 3 or any((rows[i][j] != 0) != (i == j) or rows[i][i] < 0 for i in range(3) for j in range(3)):
                raise ValueError(f"{url}: space directions {f['space directions']!r} are not an axis-aligned, "
                                 "unmirrored grid; this snippet returns the stored index grid and cannot say how "
                                 "it lies on the image")
        return sizes[1], sizes[0]

    def _dtype(self) -> np.dtype:
        f, url = self._fields, self._url
        code = DTYPE.get(f.get("type", "").lower())
        if code is None:
            raise ValueError(f"{url}: NRRD type {f.get('type')!r} is not an integer of at most 32 bits, so not a "
                             "labelmap this snippet reads")
        if code[1] == "1":
            return np.dtype(code)
        order = {"little": "<", "big": ">"}.get(f.get("endian", "").lower())
        if order is None:
            raise ValueError(f"{url}: NRRD type {f['type']!r} needs `endian: little` or `big`, got {f.get('endian')!r}")
        return np.dtype(order + code)

    def _names(self, keys: dict[str, str]) -> dict[int, str]:
        url = self._url
        segments: dict[int, dict[str, str]] = {}
        for key, value in keys.items():
            if (m := SEGMENT.fullmatch(key)) is not None:
                segments.setdefault(int(m.group(1)), {})[m.group(2)] = value
        if not segments:
            raise ValueError(f"{url}: the NRRD header has no Segment<N>_Name entries, so it is not a 3D Slicer "
                             "segmentation and its values have no names")
        if keys.get("Segmentation_ReferenceImageExtentOffset", "0 0 0").split() != ["0", "0", "0"]:
            raise ValueError(f"{url}: Segmentation_ReferenceImageExtentOffset is "
                             f"{keys['Segmentation_ReferenceImageExtentOffset']!r}: the labelmap is cropped to a "
                             "part of its image, which this snippet does not place")
        names: dict[int, str] = {}
        for n, seg in sorted(segments.items()):
            if "Name" not in seg or "LabelValue" not in seg:
                raise ValueError(f"{url}: Segment{n} has no Name or no LabelValue (files of 3D Slicer before 4.11 "
                                 "store one segment per layer instead); this snippet does not read it")
            if seg.get("Layer", "0").strip() != "0":
                raise ValueError(f"{url}: Segment{n} ({seg['Name']!r}) is in layer {seg['Layer']!r}; segments in "
                                 "several layers overlap and are not one id per pixel")
            try:
                value = int(seg["LabelValue"])
            except ValueError:
                raise ValueError(f"{url}: Segment{n}_LabelValue {seg['LabelValue']!r} is not an integer") from None
            if value == 0 or value in names:
                raise ValueError(f"{url}: Segment{n} ({seg['Name']!r}) has label value {value}, which is "
                                 + ("the value of no segment" if value == 0 else f"also {names[value]!r}'s"))
            names[value] = seg["Name"]
        return names


class _Segmentations:
    def __init__(self, io: IO, urls: list[str], ids: dict[str, int]):
        self._io, self._urls, self._ids = io, urls, ids
        self._last: tuple[int, np.ndarray] | None = None    # the last file decoded
        head = _Header(self._bytes(0), urls[0])             # header only: nothing is inflated before read
        self._size = head.size
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.shape = ((len(urls),) if len(urls) > 1 else ()) + head.size
        self.dtype = np.dtype("uint8" if max(ids.values()) <= 255 else "uint16")    # of the ids, not of the file
        self.access = "plane"           # one file per z plane: any region of it costs the whole file
        self.voxel_size_nm = None       # Slicer writes spacing 1 for an image without a scale: not a pixel size

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(region.get(k, slice(None)) for k in "yx")
        if "z" not in self.axes:
            return self._plane(0)[idx]
        sel = region.get("z", slice(None))
        if not isinstance(sel, slice):
            return self._plane(range(len(self._urls))[sel])[idx]    # IndexError when out of range, like numpy
        planes = [self._plane(k)[idx] for k in range(len(self._urls))[sel]]
        if not planes:
            return np.empty((0,) + np.empty(self._size, self.dtype)[idx].shape, self.dtype)
        return np.stack(planes)

    def _bytes(self, k: int) -> bytes:
        try:
            with self._io.open(self._urls[k]) as fh:
                return fh.read()
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            return self._io.fetch(self._urls[k]).read_bytes()

    def _plane(self, k: int) -> np.ndarray:
        """File k as (Y, X) class ids: each stored value renamed through this file's own header."""
        if self._last is not None and self._last[0] == k:
            return self._last[1]
        url, data = self._urls[k], self._bytes(k)
        head = _Header(data, url)
        if head.size != self._size:
            raise ValueError(f"{url}: plane {head.size} (y, x) differs from the first file's {self._size}; all "
                             "files of one array must agree")
        count = head.size[0] * head.size[1] * head.dtype.itemsize
        body = data[head.offset:]
        if head.encoding != "raw":
            body = zlib.decompressobj(31).decompress(body, count + 1)   # gzip; bounded, whatever the file claims
        if len(body) != count:
            raise ValueError(f"{url}: the data are {'more than ' if len(body) > count else ''}{len(body)} bytes, "
                             f"not the {count} of a {head.dtype.name} plane {head.size}")
        stored = np.frombuffer(body, head.dtype).reshape(head.size)     # NRRD's first axis (x) varies fastest
        values, inverse = np.unique(stored, return_inverse=True)
        unnamed = [int(v) for v in values if v != 0 and int(v) not in head.names]
        if unnamed:
            raise ValueError(f"{url}: the data hold the values {unnamed}, which no Segment<N>_LabelValue of the "
                             f"header names ({sorted(head.names)})")
        unlisted = sorted({head.names[int(v)] for v in values if v != 0} - set(self._ids))
        if unlisted:
            raise ValueError(f"{url}: pixels belong to the segments {unlisted}, which params.segments does not "
                             f"list ({sorted(self._ids)}); every segment name the data use must be given an id")
        ids = np.array([0 if v == 0 else self._ids[head.names[int(v)]] for v in values], self.dtype)
        plane = ids[inverse.reshape(-1)].reshape(head.size)
        self._last = (k, plane)
        return plane


def _ids(segments: object) -> dict[str, int]:
    """params.segments as name -> id: the id is the entry's position from 1; an entry that is a list holds
    several names for one id."""
    if not isinstance(segments, list) or not segments:
        raise ValueError("params.segments is required: the segment names of the files in class id order "
                         f"(id = position, from 1), got {segments!r}")
    if len(segments) > 65535:
        raise ValueError(f"params.segments lists {len(segments)} ids; this snippet returns at most 65535")
    ids: dict[str, int] = {}
    for k, entry in enumerate(segments, 1):
        names = entry if isinstance(entry, list) else [entry]
        if not names or not all(isinstance(n, str) for n in names):
            raise ValueError(f"params.segments entry {k} must be a segment name or a list of names, got {entry!r}")
        for n in names:
            if n in ids:
                raise ValueError(f"params.segments names {n!r} twice")
            ids[n] = k
    return ids


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each glob url (`<zip>::<glob>`, `<dir>/::<glob>`) becomes its matching files, sorted by name; other urls
    pass through. Binding order is kept across urls."""
    out = [u for url in urls for u in io.expand(url)]
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    return _Segmentations(io, _expand(src.urls, io), _ids(src.params.get("segments")))
