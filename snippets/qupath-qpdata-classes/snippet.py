"""The annotations of one QuPath image data file (`data.qpdata`, the per-image file of a QuPath project) filled
onto the image's full-resolution pixel grid as a yx uint8 map of class ids.

A .qpdata file is a Java object serialization stream (magic aced0005) written by QuPath's PathIO: a version
line, a JSON string describing the image server (name, width, height, pixel calibration), the locale, the image
type, the stains, the workflow, the PathObjectHierarchy and a property map. No library on the allowlist reads
Java serialization, so this module walks the stream grammar itself. It never instantiates anything: an object
is kept as its class names, its field values and the raw data its class wrote. QuPath's path objects are
Externalizable and written in block-data mode, so their content is delimited in the stream and can be read
without knowing each class: a path object is a run of byte blocks and nested objects, among them its children,
its PathClass and its ROI.

The file is small (tens of kB to a few MB) and is read whole at open; pixels are rasterized on `read`.
"""
import json
import struct

import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

MAGIC = b"\xac\xed\x00\x05"
# stream tags (Java Object Serialization Specification, 6.4.2)
NULL, REFERENCE, CLASSDESC, OBJECT, STRING, ARRAY, CLASS = 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76
BLOCKDATA, ENDBLOCKDATA, RESET, BLOCKDATALONG = 0x77, 0x78, 0x79, 0x7A
LONGSTRING, PROXYCLASSDESC, ENUM = 0x7C, 0x7D, 0x7E
BASE_HANDLE = 0x7E0000
SC_WRITE_METHOD, SC_SERIALIZABLE, SC_EXTERNALIZABLE, SC_BLOCK_DATA = 0x01, 0x02, 0x04, 0x08
SCALAR = {"B": ">b", "C": ">H", "D": ">d", "F": ">f", "I": ">i", "J": ">q", "S": ">h", "Z": ">?"}
VECTOR = {"B": ">i1", "C": ">u2", "D": ">f8", "F": ">f4", "I": ">i4", "J": ">i8", "S": ">i2", "Z": "?"}

HIERARCHY = "qupath.lib.objects.hierarchy.PathObjectHierarchy"
PATH_OBJECT = "qupath.lib.objects.PathObject"
ANNOTATION = "qupath.lib.objects.PathAnnotationObject"
PATH_CLASS = "qupath.lib.objects.classes.PathClass"
ROI = "qupath.lib.roi."
POLYGON_ROI = "qupath.lib.roi.PolygonROI$SerializationProxy"
GEOMETRY_ROI = "qupath.lib.roi.GeometryROI$WKBSerializationProxy"
MICRON = ("µm", "μm")            # micro sign (what QuPath writes) and Greek mu
STRIP = 1 << 24                            # pixels rasterized at a time: bounds the memory of one fill

Polygon = list[np.ndarray]                 # rings, each (n, 2) float64 x, y; even-odd among them (holes)


class _Obj:
    """One serialized object: its class names from the most derived up, its fields, the data its classes wrote."""

    def __init__(self, classes: tuple[str, ...]) -> None:
        self.classes = classes
        self.fields: dict[str, object] = {}
        self.data: list[object] = []          # bytes (block data) and nested values, in stream order

    def isa(self, name: str) -> bool:
        return name in self.classes


class _JavaStream:
    """A reader of the serialization grammar. Values: None, str, bytes (block data), numpy arrays (primitive
    arrays), lists (object arrays), dicts (class descriptors), ("enum", class, constant) and _Obj."""

    def __init__(self, data: bytes, url: str) -> None:
        if data[:4] != MAGIC:
            raise ValueError(f"{url}: not a Java serialization stream (QuPath .qpdata): header={data[:4]!r}")
        self._b, self._p, self._url = data, 4, url
        self._handles: list[object] = []

    def at_end(self) -> bool:
        return self._p >= len(self._b)

    def _take(self, n: int) -> bytes:
        if n < 0 or self._p + n > len(self._b):
            raise ValueError(f"{self._url}: truncated or corrupt stream at byte {self._p} (wanted {n} bytes)")
        self._p += n
        return self._b[self._p - n:self._p]

    def _num(self, fmt: str) -> int | float | bool:
        return struct.unpack(fmt, self._take(struct.calcsize(fmt)))[0]

    def _peek(self) -> int:
        if self.at_end():
            raise ValueError(f"{self._url}: stream ends inside an object")
        return self._b[self._p]

    def _utf(self, long: bool = False) -> str:
        return self._take(int(self._num(">q" if long else ">H"))).decode("utf-8", "replace")

    def _new(self, value: object) -> int:
        self._handles.append(value)
        return len(self._handles) - 1

    def _ref(self) -> object:
        h = int(self._num(">i")) - BASE_HANDLE
        if not 0 <= h < len(self._handles):
            raise ValueError(f"{self._url}: back reference to an unknown handle at byte {self._p - 4}")
        return self._handles[h]

    def content(self) -> object:
        """The next block of primitive data (bytes) or the next object."""
        tag = self._peek()
        if tag in (BLOCKDATA, BLOCKDATALONG):
            self._p += 1
            return self._take(int(self._num(">B" if tag == BLOCKDATA else ">i")))
        return self._object()

    def _annotation(self) -> list[object]:
        """What a class wrote itself, up to the end marker; adjacent byte blocks are joined."""
        out: list[object] = []
        while self._peek() != ENDBLOCKDATA:
            v = self.content()
            if isinstance(v, bytes) and out and isinstance(out[-1], bytes):
                out[-1] += v
            else:
                out.append(v)
        self._p += 1
        return out

    def _class_desc(self) -> dict | None:
        tag = int(self._num(">B"))
        if tag == NULL:
            return None
        if tag == REFERENCE:
            d = self._ref()
            if not isinstance(d, dict):
                raise ValueError(f"{self._url}: a class reference at byte {self._p - 5} is not a class descriptor")
            return d
        if tag == CLASSDESC:
            d: dict = {"name": self._utf()}
            self._take(8)                                       # serialVersionUID
            self._new(d)
            d["flags"] = self._num(">B")
            fields = []
            for _ in range(int(self._num(">H"))):
                kind = chr(int(self._num(">B")))
                name = self._utf()
                if kind in "L[":
                    self._object()                              # the field's class name, a string
                elif kind not in SCALAR:
                    raise ValueError(f"{self._url}: unknown field type {kind!r} at byte {self._p}")
                fields.append((kind, name))
            d["fields"] = fields
            self._annotation()
            d["super"] = self._class_desc()
            return d
        if tag == PROXYCLASSDESC:
            d = {"name": "<proxy>", "flags": SC_SERIALIZABLE, "fields": []}
            self._new(d)
            for _ in range(int(self._num(">i"))):
                self._utf()
            self._annotation()
            d["super"] = self._class_desc()
            return d
        raise ValueError(f"{self._url}: unexpected tag {tag:#x} for a class descriptor at byte {self._p - 1}")

    def _object(self) -> object:
        tag = int(self._num(">B"))
        if tag == NULL:
            return None
        if tag == REFERENCE:
            return self._ref()
        if tag in (STRING, LONGSTRING):
            s = self._utf(tag == LONGSTRING)
            self._new(s)
            return s
        if tag in (CLASSDESC, PROXYCLASSDESC):
            self._p -= 1
            return self._class_desc()
        if tag == CLASS:
            named = self._class_desc()
            self._new(named)
            return named
        if tag == RESET:
            self._handles = []
            return self.content()
        if tag not in (ENUM, ARRAY, OBJECT):
            raise ValueError(f"{self._url}: unexpected tag {tag:#x} at byte {self._p - 1}")
        desc = self._class_desc()
        if desc is None:
            raise ValueError(f"{self._url}: object without a class at byte {self._p}")
        if tag == ENUM:
            h = self._new(None)
            self._handles[h] = ("enum", desc["name"], self._object())
            return self._handles[h]
        if tag == ARRAY:
            h = self._new(None)
            n, kind = int(self._num(">i")), desc["name"][1:2]
            if kind in VECTOR:
                dt = np.dtype(VECTOR[kind])
                self._handles[h] = np.frombuffer(self._take(n * dt.itemsize), dt)
            else:
                items: list[object] = []
                self._handles[h] = items
                for _ in range(n):
                    items.append(self._object())
            return self._handles[h]
        chain = []
        d: dict | None = desc
        while d is not None:
            chain.append(d)
            d = d["super"]
        obj = _Obj(tuple(c["name"] for c in chain))
        self._new(obj)
        if desc["flags"] & SC_EXTERNALIZABLE:                   # written once, by the object's writeExternal
            if not desc["flags"] & SC_BLOCK_DATA:
                raise ValueError(f"{self._url}: {desc['name']} was written without block data (stream protocol "
                                 f"1), so its content has no delimiters and cannot be read without the class")
            obj.data = self._annotation()
            return obj
        for c in reversed(chain):                               # superclass first
            if not c["flags"] & SC_SERIALIZABLE:
                continue
            for kind, name in c["fields"]:
                obj.fields[name] = self._num(SCALAR[kind]) if kind in SCALAR else self._object()
            if c["flags"] & SC_WRITE_METHOD:
                obj.data += self._annotation()
        return obj


def _wkb(data: bytes, url: str) -> list[Polygon]:
    """The polygons of a well-known-binary geometry (as JTS writes it): Polygon, MultiPolygon or a collection."""
    pos = 0

    def num(code: str, count: int = 1) -> np.ndarray:
        nonlocal pos
        dt = np.dtype(code)
        if pos + count * dt.itemsize > len(data):
            raise ValueError(f"{url}: truncated WKB geometry")
        pos += count * dt.itemsize
        return np.frombuffer(data, dt, count, pos - count * dt.itemsize)

    def geometry() -> list[Polygon]:
        order = "<" if int(num("u1")[0]) else ">"
        kind = int(num(order + "u4")[0])
        if kind & 0x20000000:                                   # EWKB: an SRID follows
            num(order + "u4")
        dims, flat = 2 + bool(kind & 0x80000000) + bool(kind & 0x40000000), kind & 0x1FFFFFFF   # EWKB Z, M flags
        if 1000 <= flat < 4000:                                 # ISO WKB: 1000 Z, 2000 M, 3000 ZM
            dims, flat = 2 + (1, 1, 2)[flat // 1000 - 1], flat % 1000
        if flat == 3:
            rings = []
            for _ in range(int(num(order + "u4")[0])):
                n = int(num(order + "u4")[0])
                if n:
                    rings.append(num(order + "f8", n * dims).reshape(n, dims)[:, :2].astype(np.float64))
            return [rings]
        if flat in (6, 7):
            out: list[Polygon] = []
            for _ in range(int(num(order + "u4")[0])):
                out += geometry()
            return out
        names = {1: "Point", 2: "LineString", 4: "MultiPoint", 5: "MultiLineString"}
        raise ValueError(f"{url}: a GeometryROI holds a WKB {names.get(flat, f'type {flat}')}, which encloses no "
                         f"pixels; only Polygon, MultiPolygon and collections of them are rasterized")

    polygons = geometry()
    return [p for p in polygons if p]


def _fill(rings: Polygon, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """Even-odd fill of rows y0..y1, columns x0..x1: True where the pixel centre (x + 0.5, y + 0.5) is inside."""
    a = np.concatenate(rings)
    b = np.concatenate([np.roll(r, -1, axis=0) for r in rings])
    # an edge crosses the centre line of row r when min(ya, yb) <= r + 0.5 < max(ya, yb)
    first = np.clip(np.ceil(np.minimum(a[:, 1], b[:, 1]) - 0.5), y0, y1).astype(np.int64)
    stop = np.clip(np.ceil(np.maximum(a[:, 1], b[:, 1]) - 0.5), y0, y1).astype(np.int64)
    n = stop - first
    e = np.repeat(np.arange(len(a)), n)
    rows = np.arange(len(e)) - np.repeat(np.cumsum(n) - n, n) + first[e]
    xc = a[e, 0] + (rows + 0.5 - a[e, 1]) * (b[e, 0] - a[e, 0]) / (b[e, 1] - a[e, 1])
    cols = np.clip(np.ceil(xc - 0.5), x0, x1).astype(np.int64)     # first pixel centre at or right of the crossing
    count = np.zeros((y1 - y0, x1 - x0 + 1), np.uint8)             # uint8 wraps at 256, which keeps the parity
    np.add.at(count, (rows - y0, cols - x0), 1)
    return (np.cumsum(count, axis=1, dtype=np.uint8)[:, :-1] & 1).astype(bool)


class _Shape:
    """One annotation: its class id, its polygons and the pixel box that can hold their pixel centres."""

    def __init__(self, class_id: int, polygons: list[Polygon]) -> None:
        self.class_id, self.polygons = class_id, polygons
        pts = np.concatenate([r for p in polygons for r in p])
        if not np.isfinite(pts).all():
            raise ValueError("an annotation has a vertex that is not a finite number")
        lo, hi = np.ceil(pts.min(axis=0) - 0.5), np.ceil(pts.max(axis=0) - 0.5)
        self.x0, self.y0, self.x1, self.y1 = int(lo[0]), int(lo[1]), int(hi[0]), int(hi[1])


def _class_name(path_class: _Obj) -> str:
    """A PathClass as QuPath prints it: 'Name', or 'Parent: Name' for a derived class."""
    parent, name = path_class.fields.get("parentClass"), str(path_class.fields.get("name"))
    return f"{_class_name(parent)}: {name}" if isinstance(parent, _Obj) and parent.fields.get("name") else name


def _annotations(root: _Obj, url: str) -> list[tuple[str | None, _Obj]]:
    """(class name or None, ROI) of every annotation object under the root, in file order."""
    out: list[tuple[str | None, _Obj]] = []
    todo = [root]
    while todo:
        obj = todo.pop()
        parts = [v for v in obj.data if isinstance(v, _Obj)]
        todo += reversed([v for v in parts if v.isa(PATH_OBJECT)])
        if not obj.isa(ANNOTATION):
            continue
        rois = [v for v in parts if v.classes[0].startswith(ROI)]
        classes = [v for v in parts if v.isa(PATH_CLASS)]
        if len(rois) != 1 or len(classes) > 1:
            raise ValueError(f"{url}: an annotation with {len(rois)} ROIs and {len(classes)} classes; this "
                             f"reader expects one ROI and at most one class per annotation")
        out.append((_class_name(classes[0]) if classes else None, rois[0]))
    return out


def _polygons(roi: _Obj, url: str) -> list[Polygon]:
    kind = roi.classes[0]
    if kind == POLYGON_ROI:
        x, y = roi.fields.get("x"), roi.fields.get("y")
        if not isinstance(x, np.ndarray) or not isinstance(y, np.ndarray) or len(x) != len(y):
            raise ValueError(f"{url}: a PolygonROI without matching x and y vertex arrays")
        return [[np.column_stack([x, y]).astype(np.float64)]] if len(x) else []
    if kind == GEOMETRY_ROI:
        wkb = roi.fields.get("wkb")
        if not isinstance(wkb, np.ndarray):
            raise ValueError(f"{url}: a GeometryROI without WKB bytes")
        return _wkb(wkb.tobytes(), url)
    raise ValueError(f"{url}: ROI type {kind.removeprefix(ROI)} is not read by this snippet (only PolygonROI and "
                     f"GeometryROI are)")


class _QuPathClasses:
    axes: str = "yx"
    access: str = "file"          # the whole .qpdata is read at open; a read only rasterizes
    dtype: np.dtype = np.dtype("uint8")

    def __init__(self, data: bytes, url: str, names: list[str]) -> None:
        stream = _JavaStream(data, url)
        server, hierarchy = None, None
        while hierarchy is None and not stream.at_end():
            v = stream.content()
            if server is None and isinstance(v, str) and v.lstrip().startswith("{"):
                server = v
            elif isinstance(v, _Obj) and v.isa(HIERARCHY):
                hierarchy = v
        if hierarchy is None or not isinstance(hierarchy.fields.get("rootObject"), _Obj):
            raise ValueError(f"{url}: no PathObjectHierarchy with a root object in this stream")
        try:
            meta = json.loads(server or "")["server"]["metadata"]
            width, height = int(meta["width"]), int(meta["height"])
        except (ValueError, KeyError, TypeError):
            raise ValueError(f"{url}: no image-server JSON with a width and height before the hierarchy (QuPath "
                             f"writes it from data file version 3); the image grid is unknown") from None
        if width < 1 or height < 1 or int(meta.get("sizeZ", 1)) != 1 or int(meta.get("sizeT", 1)) != 1:
            raise ValueError(f"{url}: image {width}x{height}, sizeZ {meta.get('sizeZ')}, sizeT {meta.get('sizeT')}: "
                             f"only one 2D plane is read")
        self.shape: tuple[int, ...] = (height, width)
        self.image_name: str | None = meta.get("name")
        self.voxel_size_nm: dict[str, float] | None = None
        try:
            cal = meta["pixelCalibration"]
            (py, uy), (px, ux) = ((float(cal[k]["value"]), cal[k]["unit"]) for k in ("pixelHeight", "pixelWidth"))
            if uy in MICRON and ux in MICRON and py > 0 and px > 0:
                self.voxel_size_nm = {"y": py * 1000.0, "x": px * 1000.0}
        except (ValueError, KeyError, TypeError):
            pass
        found = _annotations(hierarchy.fields["rootObject"], url)
        self.class_counts: dict[str | None, int] = {}
        for name, _ in found:
            self.class_counts[name] = self.class_counts.get(name, 0) + 1
        if not any(n in self.class_counts for n in names):
            have = ", ".join(f"{n!r} ({c})" if n is not None else f"unclassified ({c})"
                             for n, c in self.class_counts.items()) or "none"
            raise ValueError(f"{url}: no annotation has any of the classes {names}; the file's annotations: {have}")
        self._shapes: list[_Shape] = []
        for class_id, wanted in enumerate(names, 1):            # painted in id order: the later class wins
            for name, roi in found:
                polygons = _polygons(roi, url) if name == wanted else []
                if polygons:
                    self._shapes.append(_Shape(class_id, polygons))

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        spans = []
        for axis, n in zip("yx", self.shape):
            i = region.get(axis, slice(None))
            spans.append(range(n)[i] if isinstance(i, slice) else range(range(n)[i], range(n)[i] + 1))
        rows, cols = spans
        if not rows or not cols:
            out = np.zeros((len(rows), len(cols)), self.dtype)
        else:
            y0, y1 = min(rows[0], rows[-1]), max(rows[0], rows[-1]) + 1
            x0, x1 = min(cols[0], cols[-1]), max(cols[0], cols[-1]) + 1
            out = np.zeros((y1 - y0, x1 - x0), self.dtype)
            for s in self._shapes:
                ya, yb, xa, xb = max(s.y0, y0), min(s.y1, y1), max(s.x0, x0), min(s.x1, x1)
                if ya >= yb or xa >= xb:
                    continue
                step = max(1, STRIP // (xb - xa))
                for top in range(ya, yb, step):
                    end = min(top + step, yb)
                    inside = np.zeros((end - top, xb - xa), bool)
                    for rings in s.polygons:
                        inside |= _fill(rings, top, end, xa, xb)
                    out[top - y0:end - y0, xa - x0:xb - x0][inside] = s.class_id
            if rows.step != 1 or cols.step != 1:
                out = out[np.asarray(rows)[:, None] - y0, np.asarray(cols)[None, :] - x0]
        if not isinstance(region.get("x", slice(None)), slice):
            out = out[:, 0]
        return out if isinstance(region.get("y", slice(None)), slice) else out[0]


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"qupath-qpdata-classes reads one .qpdata file, got {len(src.urls)} urls")
    classes = src.params.get("classes")
    names = [c.strip() for c in classes.split(",")] if isinstance(classes, str) else []
    if not names or not all(names) or len(set(names)) != len(names) or len(names) > 255:
        raise ValueError(f"param classes must be 1 to 255 distinct QuPath class names separated by commas, in "
                         f"id order (the first is id 1), got {classes!r}")
    url = src.urls[0]
    try:
        with io.open(url) as fh:
            data = fh.read()
    except NeedsFetch:                      # a deflated zip member
        data = io.fetch(url).read_bytes()
    return _QuPathClasses(data, url, names)
