"""The classified objects of one QuPath image entry (`data.qpdata`), filled as class ids onto the pixel grid of one
of the slides the entry shows (yx uint8).

A .qpdata file is a Java object stream. Nothing in it is deserialized: the stream's grammar is walked (class
descriptions, field values, block data, back references) into plain records, from which the image description
(JSON), each object's class name and each ROI's coordinates are taken. A QuPath "sparse" image is a montage of
cropped slides; the JSON says where each crop sits, and that maps the ROIs back onto the slide. See README.md."""
import json
import struct
import time
from pathlib import Path

import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1

MAGIC = b"\xac\xed\x00\x05"
HANDLE0 = 0x7E0000              # the first back-reference handle
NULL, REFERENCE, CLASSDESC, OBJECT, STRING, ARRAY, CLASS, BLOCK, ENDBLOCK, BLOCKLONG, LONGSTRING, PROXYDESC, ENUM = (
    0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x7A, 0x7C, 0x7D, 0x7E)
WRITE_METHOD, SERIALIZABLE, EXTERNALIZABLE, BLOCK_DATA = 1, 2, 4, 8          # class description flags
PRIMITIVE = {"B": ">b", "C": ">H", "D": ">d", "F": ">f", "I": ">i", "J": ">q", "S": ">h", "Z": ">?"}
BAND = 1 << 22                  # pixels filled at a time
UNITS_NM = {"µm": 1000.0, "um": 1000.0, "nm": 1.0, "mm": 1e6}
WAITS = (20.0, 40.0, 60.0)      # seconds before each further fetch of a file a server refused with 429


class _Desc:
    """A class description: its name, flags, (type code, name) per field, and its superclass's description."""

    def __init__(self, name: str):
        self.name, self.flags, self.fields, self.parent = name, 0, [], None


class _Obj:
    """An object as the stream spells it: class name, field values by name, and what its class wrote itself."""

    def __init__(self, cls: str):
        self.cls, self.fields, self.extra = cls, {}, []


class _Stream:
    """A Java object stream (protocol 2) read by its grammar. No class is loaded and no object is made."""

    def __init__(self, buf: bytes):
        if buf[:4] != MAGIC:
            raise ValueError(f"not a Java object stream: starts with {buf[:4]!r}, not {MAGIC!r}")
        self.buf, self.pos, self.handles = buf, 4, []

    def done(self) -> bool:
        return self.pos >= len(self.buf)

    def _take(self, fmt: str):
        (v,) = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += struct.calcsize(fmt)
        return v

    def _bytes(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.buf):
            raise ValueError(f"Java stream: {n} bytes wanted at byte {self.pos} of {len(self.buf)}")
        v = self.buf[self.pos:self.pos + n]
        self.pos += n
        return v

    def _text(self, fmt: str) -> str:
        return self._bytes(self._take(fmt)).decode("utf-8", "replace")

    def _ref(self):
        i = self._take(">i") - HANDLE0
        if not 0 <= i < len(self.handles):
            raise ValueError(f"Java stream: back reference {i} at byte {self.pos} points at nothing")
        return self.handles[i]

    def content(self):
        """The next item: a run of block data as bytes, or an object."""
        tag = self.buf[self.pos]
        if tag in (BLOCK, BLOCKLONG):
            self.pos += 1
            return self._bytes(self._take(">B" if tag == BLOCK else ">i"))
        return self.item()

    def _contents(self) -> list:
        out = []
        while True:
            if self.done():
                raise ValueError("Java stream ends inside an object")
            if self.buf[self.pos] == ENDBLOCK:
                self.pos += 1
                return out
            out.append(self.content())

    def _desc(self) -> _Desc | None:
        tag = self._take(">B")
        if tag == NULL:
            return None
        if tag == REFERENCE:
            d = self._ref()
            if not isinstance(d, _Desc):
                raise ValueError(f"Java stream: reference before byte {self.pos} is not a class description")
            return d
        if tag == CLASSDESC:
            d = _Desc(self._text(">H"))
            self.pos += 8                       # serialVersionUID
            self.handles.append(d)
            d.flags = self._take(">B")
            for _ in range(self._take(">H")):
                code = chr(self._take(">B"))
                name = self._text(">H")
                if code in "[L":
                    self.item()                 # the field's class name, a string
                d.fields.append((code, name))
        elif tag == PROXYDESC:
            d = _Desc("(proxy)")
            self.handles.append(d)
            d.flags = SERIALIZABLE
            for _ in range(self._take(">i")):
                self._text(">H")
        else:
            raise ValueError(f"Java stream: tag {tag:#x} at byte {self.pos - 1} is not a class description")
        self._contents()                        # class annotation
        d.parent = self._desc()
        return d

    def _new(self, value=None) -> int:
        self.handles.append(value)
        return len(self.handles) - 1

    def item(self):
        """The next object: None, str, numpy array (primitive array), list (object array), _Obj, _Desc or an
        enum constant's name."""
        tag = self._take(">B")
        if tag == NULL:
            return None
        if tag == REFERENCE:
            return self._ref()
        if tag in (STRING, LONGSTRING):
            i = self._new()
            self.handles[i] = self._text(">H" if tag == STRING else ">q")
            return self.handles[i]
        if tag in (CLASSDESC, PROXYDESC):
            self.pos -= 1
            return self._desc()
        if tag == CLASS:
            d = self._desc()
            self._new(d)
            return d
        if tag == ENUM:
            self._desc()
            i = self._new()
            self.handles[i] = self.item()
            return self.handles[i]
        if tag == ARRAY:
            d = self._desc()
            i = self._new()
            n = self._take(">i")
            code = d.name[1:2] if d is not None else ""
            if code in PRIMITIVE:
                dt = np.dtype(PRIMITIVE[code])
                v = np.frombuffer(self._bytes(n * dt.itemsize), dt)
            else:
                v = [self.item() for _ in range(n)]
            self.handles[i] = v
            return v
        if tag == OBJECT:
            d = self._desc()
            if d is None:
                raise ValueError(f"Java stream: object without a class before byte {self.pos}")
            o = _Obj(d.name)
            self._new(o)
            if d.flags & EXTERNALIZABLE:        # one block for the whole object, written by its own code
                if not d.flags & BLOCK_DATA:
                    raise ValueError(f"Java stream: {d.name} is written without block data (protocol 1)")
                o.extra = self._contents()
                return o
            chain = []
            while d is not None:
                chain.append(d)
                d = d.parent
            for d in reversed(chain):           # superclass first
                if d.flags & SERIALIZABLE:
                    for code, name in d.fields:
                        o.fields[name] = self._take(PRIMITIVE[code]) if code in PRIMITIVE else self.item()
                    if d.flags & WRITE_METHOD:
                        o.extra += self._contents()
            return o
        raise ValueError(f"Java stream: unknown tag {tag:#x} at byte {self.pos - 1}")


def _unquote(s: str) -> str:
    """Percent-decoding of a file: URI."""
    out, i, raw = bytearray(), 0, s.encode("utf-8")
    while i < len(raw):
        pair = raw[i + 1:i + 3]
        if raw[i] == 0x25 and len(pair) == 2 and all(c in b"0123456789abcdefABCDEF" for c in pair):
            out.append(int(pair, 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode("utf-8", "replace")


def _slide_name(builder: dict) -> str | None:
    """The file name of the slide a 'uri' image builder opens, or None for any other builder."""
    if not isinstance(builder, dict) or not isinstance(builder.get("uri"), str) \
            or builder.get("builderType", "uri") != "uri":
        return None
    return _unquote(builder["uri"]).replace("\\", "/").rsplit("/", 1)[-1]


def _places(server: dict, image: str) -> tuple[dict, list[tuple[int, int, int, int, int, int]], list[str]]:
    """Where slide `image` is shown in the entry's image: (the slide's metadata, [(x, y of the shown rectangle in
    the entry's image, x, y of the same rectangle on the slide, width, height)], every slide name the entry
    shows)."""
    names, places, meta = [], [], None
    if server.get("builderType") == "sparse":
        shown = []
        for region in server.get("regions") or []:
            for res in region.get("resolutions") or []:
                b = res.get("serverBuilder") or {}
                crop = b.get("region") if b.get("builderType") == "cropped" else None
                leaf = b.get("builder") if crop else b
                shown.append((region.get("region") or {}, res.get("downsample"), crop, leaf))
    else:
        shown = [({}, 1.0, None, server)]
    for at, down, crop, leaf in shown:
        name = _slide_name(leaf)
        if name is None:
            continue
        names.append(name)
        if name != image:
            continue
        m = leaf.get("metadata") or {}
        w, h = int(m["width"]), int(m["height"])
        if meta is not None and (int(meta["width"]), int(meta["height"])) != (w, h):
            raise ValueError(f"qpdata: slide {image!r} is listed with two sizes")
        meta = m
        box = crop or {"x": 0, "y": 0, "width": w, "height": h}
        cw, ch = int(box["width"]), int(box["height"])
        if down != 1.0 or (at and (int(at["width"]), int(at["height"])) != (cw, ch)):
            raise ValueError(f"qpdata: slide {image!r} is shown rescaled (downsample {down}, {cw}x{ch} shown as "
                             f"{at.get('width')}x{at.get('height')}); only pixel-for-pixel crops are read")
        places.append((int(at.get("x", 0)), int(at.get("y", 0)), int(box["x"]), int(box["y"]), cw, ch))
    return meta or {}, places, names


def _class_name(c: _Obj | None) -> str | None:
    """A PathClass's name as QuPath shows it: 'Parent: Child' for a derived class."""
    if c is None or not isinstance(c.fields.get("name"), str):
        return None
    parent = c.fields.get("parentClass")
    above = _class_name(parent) if isinstance(parent, _Obj) else None
    return f"{above}: {c.fields['name']}" if above else c.fields["name"]


def _is(v, prefix: str) -> bool:
    return isinstance(v, _Obj) and v.cls.startswith(prefix)


def _objects(o: _Obj, out: list[tuple[str | None, _Obj | None]]) -> None:
    """Every path object under `o` as (class name, ROI record), parents before children, in file order."""
    children = []
    cls = roi = None
    for v in o.extra:
        if _is(v, "qupath.lib.objects.classes."):
            cls = v
        elif _is(v, "qupath.lib.objects."):
            children.append(v)
        elif _is(v, "qupath.lib.roi.") and roi is None:
            roi = v
    out.append((_class_name(cls), roi))
    for c in children:
        _objects(c, out)


def _wkb(buf: bytes, p: int, rings: list[np.ndarray]) -> int:
    """The rings of a WKB polygon or multipolygon at buf[p:], appended to `rings`; the position after it."""
    e = "<" if buf[p] else ">"
    kind, n = struct.unpack_from(e + "II", buf, p + 1)
    p += 9
    if kind == 3:
        for _ in range(n):
            (m,) = struct.unpack_from(e + "I", buf, p)
            rings.append(np.frombuffer(buf, e + "f8", 2 * m, p + 4).reshape(m, 2))
            p += 4 + 16 * m
    elif kind in (6, 7):
        for _ in range(n):
            p = _wkb(buf, p, rings)
    else:
        raise ValueError(f"qpdata: WKB geometry type {kind} is not an area (only Polygon and MultiPolygon are filled)")
    return p


def _edges(roi: _Obj, cls: str) -> np.ndarray:
    """The non-horizontal boundary edges of an area ROI as rows (x0, y0, x1, y1), in the entry's image pixels."""
    f, kind = roi.fields, roi.cls.rsplit(".", 1)[-1]
    if f.get("z", 0) != 0 or f.get("t", 0) != 0:
        raise ValueError(f"qpdata: a {cls!r} ROI is on z {f.get('z')}, t {f.get('t')}; only one plane is read")
    rings: list[np.ndarray] = []
    if kind.startswith("GeometryROI") and isinstance(f.get("wkb"), np.ndarray):
        _wkb(f["wkb"].tobytes(), 0, rings)
    elif kind.startswith("PolygonROI") and isinstance(f.get("x"), np.ndarray) and isinstance(f.get("y"), np.ndarray):
        rings.append(np.stack([f["x"], f["y"]], axis=1))
    elif kind.startswith("RectangleROI") and all(isinstance(f.get(k), float) for k in ("x", "y", "x2", "y2")):
        rings.append(np.array([[f["x"], f["y"]], [f["x2"], f["y"]], [f["x2"], f["y2"]], [f["x"], f["y2"]]]))
    else:
        raise ValueError(f"qpdata: a {cls!r} object has a {kind} ROI; only polygon, rectangle and geometry (WKB "
                         "polygon) ROIs are filled")
    parts = []
    for r in rings:
        r = np.asarray(r, dtype=np.float64)
        if len(r) >= 3:
            parts.append(np.concatenate([r, np.roll(r, -1, axis=0)], axis=1))
    e = np.concatenate(parts) if parts else np.zeros((0, 4))
    return e[e[:, 1] != e[:, 3]]


def _fill(e: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """Even-odd fill of the edges `e` over pixels [y0, y1) x [x0, x1): True where the pixel's centre is inside."""
    h, w = y1 - y0, x1 - x0
    lo, hi = np.minimum(e[:, 1], e[:, 3]), np.maximum(e[:, 1], e[:, 3])
    first = np.maximum(np.ceil(lo - 0.5), y0)       # rows whose centre r + 0.5 lies in [lo, hi)
    last = np.minimum(np.ceil(hi - 0.5), y1)
    keep = last > first
    if not keep.any():
        return np.zeros((h, w), dtype=bool)
    e, first = e[keep], first[keep].astype(np.int64)
    n = last[keep].astype(np.int64) - first
    k = np.repeat(np.arange(len(e)), n)
    rows = first[k] + np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
    xc = e[k, 0] + (rows + 0.5 - e[k, 1]) * (e[k, 2] - e[k, 0]) / (e[k, 3] - e[k, 1])
    cols = np.clip(np.ceil(xc - 0.5), x0, x1).astype(np.int64) - x0     # first pixel centre at or right of the crossing
    flips = np.bincount((rows - y0) * (w + 1) + cols, minlength=h * (w + 1)).reshape(h, w + 1)
    return (np.cumsum(flips[:, :w], axis=1) & 1).astype(bool)


def _span(key: int | slice, n: int) -> tuple[int, int, int | np.ndarray]:
    """What `key` asks of an axis of length n: (lo, hi, index into [lo, hi)); an int index drops the axis."""
    if isinstance(key, slice):
        idx = np.arange(n)[key]
        if idx.size == 0:
            return 0, 0, idx
        lo = int(idx.min())
        return lo, int(idx.max()) + 1, idx - lo
    k = int(key) + (n if key < 0 else 0)
    if not 0 <= k < n:
        raise IndexError(f"index {key} is outside an axis of length {n}")
    return k, k + 1, 0


class _Labels:
    axes = "yx"
    dtype = np.dtype("uint8")
    access = "file"

    def __init__(self, path: Path, image: str, classes: list[str]):
        self._path, self._image, self._classes, self._shapes = path, image, classes, None
        with open(path, "rb") as fh:
            head = fh.read(1 << 16)
            stream = _Stream(head)
            try:
                stream.content()                    # "Data file version N"
                text = stream.content()
            except (ValueError, struct.error, IndexError):       # the description is longer than the head
                stream = _Stream(head + fh.read())
                stream.content()
                text = stream.content()
        doc = json.loads(text) if isinstance(text, str) and text.lstrip().startswith("{") else None
        if not isinstance(doc, dict) or not isinstance(doc.get("server"), dict):
            raise ValueError("qpdata: no JSON image description after the version (data file version 3 or later "
                             "is read)")
        meta, self._places, names = _places(doc["server"], image)
        if not self._places:
            raise ValueError(f"qpdata: this entry does not show a slide named {image!r}; it shows {sorted(set(names))}")
        self.shape = (int(meta["height"]), int(meta["width"]))
        cal = meta.get("pixelCalibration") or {}
        size = {}
        for axis, key in (("y", "pixelHeight"), ("x", "pixelWidth")):
            v = cal.get(key) or {}
            if v.get("unit") in UNITS_NM and isinstance(v.get("value"), (int, float)):
                size[axis] = float(v["value"]) * UNITS_NM[v["unit"]]
        self.voxel_size_nm = size if len(size) == 2 else None

    def _load(self) -> list[tuple[int, np.ndarray, tuple[int, int, int, int]]]:
        """(class id, edges, the pixel box x0, y0, x1, y1 that holds them) per object of a wanted class, lowest id
        first."""
        if self._shapes is None:
            stream = _Stream(self._path.read_bytes())
            root = None
            while not stream.done() and root is None:
                v = stream.content()
                if _is(v, "qupath.lib.objects.hierarchy.PathObjectHierarchy"):
                    root = v.fields.get("rootObject")
            if not isinstance(root, _Obj):
                raise ValueError("qpdata: no object hierarchy in the file")
            found: list[tuple[str | None, _Obj | None]] = []
            _objects(root, found)
            ids = {name: i + 1 for i, name in enumerate(self._classes)}
            missing = sorted(set(ids) - {c for c, _ in found})
            if missing:
                raise ValueError(f"qpdata: no object of class {missing}; the file's classes are "
                                 f"{sorted({c for c, _ in found if c})}")
            shapes = []
            for c, roi in found:
                e = _edges(roi, c) if c in ids and roi is not None else np.zeros((0, 4))
                if len(e):
                    xs, ys = e[:, [0, 2]], e[:, [1, 3]]
                    box = (int(np.floor(xs.min())), int(np.floor(ys.min())),
                           int(np.ceil(xs.max())) + 1, int(np.ceil(ys.max())) + 1)
                    shapes.append((ids[c], e, box))
            self._shapes = sorted(shapes, key=lambda s: s[0])
        return self._shapes

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        if set(region) - set("yx"):
            raise KeyError(f"axes {sorted(set(region) - set('yx'))} are not in 'yx'")
        ylo, yhi, yi = _span(region.get("y", slice(None)), self.shape[0])
        xlo, xhi, xi = _span(region.get("x", slice(None)), self.shape[1])
        out = np.zeros((yhi - ylo, xhi - xlo), dtype=self.dtype)
        for cid, edges, box in (self._load() if out.size else []):
            for ax, ay, sx, sy, w, h in self._places:       # the object's box, the shown part and the read, on the slide
                dx, dy = sx - ax, sy - ay
                x0, x1 = max(xlo, sx, box[0] + dx), min(xhi, sx + w, box[2] + dx)
                y0, y1 = max(ylo, sy, box[1] + dy), min(yhi, sy + h, box[3] + dy)
                if x0 >= x1 or y0 >= y1:
                    continue
                e = edges + np.array([dx, dy, dx, dy], dtype=np.float64)
                step = max(1, BAND // (x1 - x0))
                for b in range(y0, y1, step):
                    rows = slice(b - ylo, min(b + step, y1) - ylo)
                    out[rows, x0 - xlo:x1 - xlo][_fill(e, b, min(b + step, y1), x0, x1)] = cid
        return out[yi][..., xi]


def _fetch(io: IO, url: str) -> Path:
    """`io.fetch(url)`, asked again after each of WAITS when the server answers 429 Too Many Requests. A host
    that counts requests per client and minute (Zenodo: 133 in any 60 s) answers so once the job's other arrays
    have used the minute up, and `io` takes a 429 as final. Any other error is raised at once. Told by class
    name, since a snippet may not import the HTTP client."""
    for wait in (*WAITS, None):
        try:
            return io.fetch(url)
        except Exception as e:      # BudgetExceeded, HostNotAllowed, a 404, ...: not a matter of waiting
            status = getattr(getattr(e, "response", None), "status_code", None)
            if wait is None or status != 429 or "HTTPStatusError" not in {c.__name__ for c in type(e).__mro__}:
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"qupath-qpdata-classes reads one data.qpdata, got {len(src.urls)} urls")
    image, classes = src.params.get("image"), src.params.get("classes")
    if not isinstance(image, str) or not image:
        raise ValueError("param 'image' is required: the file name of the slide to put the classes on")
    if not isinstance(classes, list) or not classes or not all(isinstance(c, str) and c for c in classes) \
            or len(set(classes)) != len(classes) or len(classes) > 255:
        raise ValueError("param 'classes' is required: a list of 1 to 255 different class names; ids follow its order")
    return _Labels(_fetch(io, src.urls[0]), image, list(classes))
