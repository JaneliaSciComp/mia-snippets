"""`harness describe` (SPEC §7): what a file says about itself, read from its headers and metadata with no snippet
code, and what a binding's arrays measure on a few planes. Deterministic: the same input gives the same JSON.
Everything a file states is untrusted data, so every string and list in the output is cut to a fixed length."""
from __future__ import annotations

import itertools
import json
import math
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree

import httpx
import numpy as np
import yaml

from .api import Array, BudgetExceeded, HostNotAllowed, NeedsFetch
from .binding import load_binding
from .io import IO, _split
from .run import _region, check_binding, open_arrays

FILE_BYTES = 64 << 20    # what one file-level describe may fetch: headers and metadata, never the pixels
TEXT = 300               # characters kept of any string a file states
ITEMS = 100              # entries kept of any list or mapping a file states
DEPTH = 8                # nesting kept of a file's metadata
PLANES = 5               # planes measured per array, evenly spaced through z
WINDOW = 1024            # the measured part of a plane: its central WINDOW x WINDOW pixels
CHANNELS = 8             # raw channels measured
CUT = "... (cut)"

TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")
MRC_SUFFIXES = (".mrc", ".mrcs", ".rec", ".st", ".ali", ".map")
MRC_MODES = {0: "int8", 1: "int16", 2: "float32", 3: "complex int16", 4: "complex64", 6: "uint16", 12: "float16",
             16: "rgb uint8", 101: "4-bit"}
IMOD_UNITS = {0: "pixel", 1: "m", -2: "cm", -3: "mm", -6: "um", -9: "nm", -10: "angstrom", -12: "pm"}
IMOD_HEADER = 240        # "IMODV1.2" + the 232-byte model structure; everything is big-endian
PNG_COLOR = {0: "grey", 2: "rgb", 3: "palette", 4: "grey+alpha", 6: "rgb+alpha"}
ZARR_META = ("zarr.json", ".zattrs", ".zgroup", ".zarray", "attributes.json")


def bound(value: Any, depth: int = 0) -> Any:
    """`value` as JSON data: strings cut to TEXT characters with control characters replaced, lists and mappings
    cut to ITEMS entries, nesting to DEPTH, numpy values as Python's, a float JSON cannot hold as its name."""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value[:TEXT + 1]).decode("latin-1")
    if isinstance(value, str):
        text = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in value[:TEXT])
        return text + (CUT if len(value) > TEXT else "")
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if depth >= DEPTH:
        return CUT
    if isinstance(value, np.ndarray):
        flat = value.ravel()
        value = flat[:ITEMS + 1].tolist() if value.ndim != 1 or flat.size > ITEMS else flat.tolist()
    if isinstance(value, Mapping):
        items = list(itertools.islice(value.items(), ITEMS + 1))
        out = {bound(str(key)): bound(item, depth + 1) for key, item in items[:ITEMS]}
        if len(items) > ITEMS:
            out[CUT] = True
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(itertools.islice(value, ITEMS + 1))
        return [bound(item, depth + 1) for item in items[:ITEMS]] + ([CUT] if len(items) > ITEMS else [])
    return bound(str(value))


def _report(io: IO, out: dict, work) -> dict:
    """Run `work(out)` and report what it raised the way smoke does; `out` keeps what was found before."""
    try:
        work(out)
    except HostNotAllowed as e:
        out["errors"].append(str(e))
        out["host_not_allowed"] = {"host": e.host, "chain": e.chain}
    except Exception as e:   # a file's bytes and snippet code can raise anything; describe's job is to report it
        out["errors"].append(f"{type(e).__name__}: {e}")
    out["bytes_fetched"] = io.bytes_fetched
    out["ok"] = not out["errors"]
    return bound(out)


# --- a file --------------------------------------------------------------------------------------------------

def describe_file(address: str, io: IO) -> dict:
    """What the file at `address` (a url, `<zip>::<member>`, or a glob, which stands for its first match) says
    about itself. A url ending in `/` is described as a Zarr or N5 store when it has their metadata, otherwise
    as a directory index. Fetches at most FILE_BYTES."""
    io.byte_cap = min(io.byte_cap, FILE_BYTES)

    def work(out: dict) -> None:
        urls = io.expand(address)
        if urls != [address]:
            out["url"], out["matched"] = urls[0], len(urls)
        file_url, member = _split(urls[0])
        if member is None and file_url.endswith("/"):
            out.update(_store(file_url, io))
            return
        try:
            fh: BinaryIO = io.open(urls[0])
        except NeedsFetch:   # a deflated zip member, a server without Range: the whole file, under the cap
            fh = open(io.fetch(urls[0]), "rb")
        with fh:
            out["size"] = fh.seek(0, 2)
            fh.seek(0)
            head = fh.read(1024)
            out["format"] = kind = _detect(head, file_url if member is None else member)
            fh.seek(0)
            if kind == "zip":
                names = io.list(file_url) if member is None else []
                out.update(members=len(names), names=names)
            elif kind == "unknown":
                out["magic"] = head[:16].hex()
                if _is_text(head):
                    out["text"] = head.decode("utf-8", "replace")
            else:
                out.update({"tiff": _tiff, "mrc": _mrc, "hdf5": _hdf5, "imod": _imod, "png": _png,
                            "jpeg": _jpeg}[kind](fh))

    return _report(io, {"ok": False, "address": address, "errors": [], "bytes_fetched": 0}, work)


def _detect(head: bytes, name: str) -> str:
    """The format, from the file's first bytes; MRC also by its `MAP ` stamp or, for an old file without one,
    its suffix."""
    if head[:4] in TIFF_MAGIC:
        return "tiff"
    for magic, kind in ((b"\x89HDF\r\n\x1a\n", "hdf5"), (b"IMODV1.2", "imod"), (b"\x89PNG\r\n\x1a\n", "png"),
                        (b"\xff\xd8", "jpeg"), (b"PK\x03\x04", "zip")):
        if head.startswith(magic):
            return kind
    if head[208:212] == b"MAP " or (len(head) == 1024 and name.lower().endswith(MRC_SUFFIXES)):
        return "mrc"
    return "unknown"


def _is_text(head: bytes) -> bool:
    try:
        text = head[:256].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(text) and all(ch.isprintable() or ch in "\r\n\t" for ch in text)


def _tiff(fh: BinaryIO) -> dict:
    """The first page's layout, ImageJ and OME metadata (channel names in file order), and the series. Listing
    the series of a stack without ImageJ or OME metadata walks every page's directory, which the byte cap may
    stop: `series_error` then says so and the first page still stands."""
    import tifffile
    with tifffile.TiffFile(fh, _multifile=False) as tif:   # never opens the other files an OME-TIFF names
        page = tif.pages.first
        unit = page.tags.get("ResolutionUnit")
        out: dict = {"bigtiff": tif.is_bigtiff, "byteorder": tif.byteorder, "first_page": {
            "shape": list(page.shape), "axes": page.axes, "dtype": str(page.dtype),
            "compression": page.compression.name, "photometric": page.photometric.name,
            "colormap": "ColorMap" in page.tags, "tiled": page.is_tiled,
            "resolution": [_ratio(page.tags.get(tag)) for tag in ("XResolution", "YResolution")],
            "resolution_unit": getattr(unit.value, "name", unit.value) if unit else None}}
        if tif.is_imagej:
            out["imagej"] = tif.imagej_metadata
        if tif.is_ome:
            out["format"] = "ome-tiff"
            out["ome"] = _ome(tif.ome_metadata or "")
        elif not tif.is_imagej and page.description:
            out["description"] = page.description
        try:
            out["series"] = [{"kind": series.kind, "shape": list(series.shape), "axes": series.axes,
                              "dtype": str(series.dtype)} for series in tif.series[:ITEMS]]
        except Exception as e:
            out["series_error"] = f"{type(e).__name__}: {e}"
    return out


def _ratio(tag) -> float | None:
    """A TIFF rational tag as a number."""
    try:
        numerator, denominator = tag.value
        return numerator / denominator
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def _ome(xml: str) -> dict:
    """Each image of an OME-XML block: its sizes, physical pixel sizes and its channels' names in file order."""
    if len(xml) > 4 << 20:
        return {"error": "the OME-XML is over 4 MiB and was not parsed"}
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as e:
        return {"error": f"the OME-XML does not parse: {e}"}

    def local(element: ElementTree.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    images = []
    for image in itertools.islice((el for el in root.iter() if local(el) == "Image"), ITEMS):
        pixels = next((el for el in image if local(el) == "Pixels"), None)
        entry: dict = {"name": image.get("Name")}
        if pixels is not None:
            entry.update(
                dimension_order=pixels.get("DimensionOrder"), type=pixels.get("Type"),
                size={axis: pixels.get(f"Size{axis}") for axis in "XYZCT"},
                physical_size={axis: [pixels.get(f"PhysicalSize{axis}"), pixels.get(f"PhysicalSize{axis}Unit")]
                               for axis in "XYZ" if pixels.get(f"PhysicalSize{axis}")},
                channels=[{"index": index, "name": channel.get("Name"), "fluor": channel.get("Fluor"),
                           "samples_per_pixel": channel.get("SamplesPerPixel")}
                          for index, channel in enumerate(el for el in pixels if local(el) == "Channel")])
        images.append(entry)
    return {"images": images}


def _mrc(fh: BinaryIO) -> dict:
    """The 1024-byte MRC header: dims, mode, cell and voxel size, origin, axis order and the text labels."""
    from mrcfile.dtypes import HEADER_DTYPE
    head = fh.read(1024)
    if len(head) < 1024:
        raise ValueError(f"an MRC header is 1024 bytes; the file has {len(head)}")
    order = ">" if head[212:214] == b"\x11\x11" else "<"   # the machine stamp; files without one are little-endian
    h = np.frombuffer(head, HEADER_DTYPE.newbyteorder(order))[0]
    dims, sampling = [int(h[k]) for k in ("nx", "ny", "nz")], [int(h[k]) for k in ("mx", "my", "mz")]
    cell = [float(h["cella"][k]) for k in "xyz"]
    return {
        "byteorder": order, "shape_xyz": dims, "mode": int(h["mode"]),
        "dtype": MRC_MODES.get(int(h["mode"]), "unknown"), "sampling_xyz": sampling, "cell_angstrom": cell,
        "voxel_size_angstrom": [c / m if m > 0 else None for c, m in zip(cell, sampling)],
        "origin": [float(h["origin"][k]) for k in "xyz"],
        "axis_order": [int(h[k]) for k in ("mapc", "mapr", "maps")],
        "min_max_mean": [float(h[k]) for k in ("dmin", "dmax", "dmean")], "space_group": int(h["ispg"]),
        "extended_header_bytes": int(h["nsymbt"]), "extended_header_type": bytes(h["exttyp"]),
        "labels": [bytes(label).rstrip(b" \0") for label in h["label"][:max(0, min(int(h["nlabl"]), 10))]]}


def _hdf5(fh: BinaryIO) -> dict:
    """The file's groups and datasets, at most ITEMS of them: path, shape, dtype, chunks, compression, attrs."""
    import h5py
    nodes: list[dict] = []

    def attrs(obj) -> dict:
        found = {}
        for key in itertools.islice(obj.attrs, ITEMS):
            try:
                found[key] = obj.attrs[key]
            except Exception as e:   # an attribute of a type h5py cannot read
                found[key] = f"unreadable: {type(e).__name__}"
        return found

    def visit(name: str, obj) -> bool | None:
        if len(nodes) >= ITEMS:
            return True   # stops the walk
        node: dict = {"path": f"/{name}", "attrs": attrs(obj)}
        if isinstance(obj, h5py.Dataset):
            node.update(shape=list(obj.shape), dtype=str(obj.dtype), chunks=obj.chunks, compression=obj.compression)
        nodes.append(node)
        return None

    with h5py.File(fh, "r") as f:
        cut = f.visititems(visit)
        return {"attrs": attrs(f), "nodes": nodes, "nodes_cut": bool(cut)}


def _imod(fh: BinaryIO) -> dict:
    """An IMOD binary model: its image dims, scale and offsets, and each object with its number (from 1, the
    number `imodinfo` and a binding use), name, contour and point counts, flags and the sections it covers."""
    b = fh.read()
    if len(b) < IMOD_HEADER:
        raise ValueError(f"an IMOD model header is {IMOD_HEADER} bytes; the file has {len(b)}")
    nx, ny, nz, n_objects, flags = struct.unpack_from(">4iI", b, 136)
    pixel_size, units = struct.unpack_from(">fi", b, 216)
    out: dict = {
        "name": b[8:136].split(b"\0")[0], "dims_xyz": [nx, ny, nz], "objects_stated": n_objects,
        "flipped_yz": bool(flags & 1 << 16), "offset_xyz": list(struct.unpack_from(">3f", b, 172)),
        "scale_xyz": list(struct.unpack_from(">3f", b, 184)), "pixel_size": pixel_size,
        "pixel_size_unit": IMOD_UNITS.get(units, str(units)), "objects": []}
    p, objects, current = IMOD_HEADER, out["objects"], None
    while p + 8 <= len(b):
        chunk = b[p:p + 4]
        p += 4
        if chunk == b"IEOF":
            break
        if chunk == b"OBJT":            # name[64] extra[64] contsize flags axis drawmode red green blue ...
            contours, oflags = struct.unpack_from(">iI", b, p + 128)
            current = {"number": len(objects) + 1, "name": b[p:p + 64].split(b"\0")[0],
                       "contours_stated": contours, "contours": 0, "points": 0, "open": bool(oflags & 1 << 3),
                       "scattered_points": bool(oflags & 1 << 9),
                       "color_rgb": [round(v, 3) for v in struct.unpack_from(">3f", b, p + 144)],
                       "sections": None}
            if len(objects) < ITEMS:
                objects.append(current)
            p += 176
        elif chunk == b"CONT":          # psize flags time surf, then psize * (x, y, z) floats
            (psize,) = struct.unpack_from(">i", b, p)
            p += 16
            if psize < 0 or p + 12 * psize > len(b):
                raise ValueError(f"truncated IMOD model: a contour of {psize} points at byte {p - 20}")
            if current is not None and psize:
                z = np.floor(np.frombuffer(b, ">f4", 3 * psize, p)[2::3] + 0.5)   # IMOD's rounding to a section
                low, high = int(z.min()), int(z.max())
                seen = current["sections"]
                current["sections"] = [low, high] if seen is None else [min(seen[0], low), max(seen[1], high)]
                current["contours"] += 1
                current["points"] += psize
            p += 12 * psize
        elif chunk == b"MESH":          # vsize lsize flag time surf, vertices, index list
            vsize, lsize = struct.unpack_from(">2i", b, p)
            p += 16 + 12 * vsize + 4 * lsize
        else:                           # every other chunk: id, byte count, data
            (size,) = struct.unpack_from(">i", b, p)
            if size < 0:
                raise ValueError(f"bad IMOD chunk {chunk!r} at byte {p - 4}: size {size}")
            if chunk == b"MINX" and size >= 72:   # how the model sits on its image: scale, translation, rotation
                out["image_reference"] = {name: list(struct.unpack_from(">3f", b, p + 4 + offset))
                                          for name, offset in (("scale", 36), ("translation", 48), ("rotation", 60))}
            p += 4 + size
    return out


def _png(fh: BinaryIO) -> dict:
    """Size, bit depth, colour type and, for a palette image, its colours."""
    out: dict = {}
    fh.seek(8)
    for _ in range(64):   # the chunks before the pixels
        header = fh.read(8)
        if len(header) < 8:
            break
        length, chunk = struct.unpack(">I4s", header)
        if chunk == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", fh.read(13))
            out.update(width=width, height=height, bit_depth=depth, mode=PNG_COLOR.get(color, str(color)),
                       interlaced=bool(interlace))
            fh.seek(length - 13 + 4, 1)
        elif chunk == b"PLTE":
            colors = fh.read(min(length, 3 * ITEMS))
            out.update(palette_colors=length // 3, palette=[list(colors[i:i + 3]) for i in range(0, len(colors), 3)])
            fh.seek(length - len(colors) + 4, 1)
        elif chunk in (b"IDAT", b"IEND"):
            break
        else:
            out["transparency"] = out.get("transparency", False) or chunk == b"tRNS"
            fh.seek(length + 4, 1)
    return out


def _jpeg(fh: BinaryIO) -> dict:
    """Size, sample precision and components, from the frame header."""
    fh.seek(2)
    for _ in range(256):   # the segments before the scan
        header = fh.read(4)
        if len(header) < 4 or header[0] != 0xFF:
            break
        marker, length = header[1], struct.unpack(">H", header[2:])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):   # a start of frame
            precision, height, width, components = struct.unpack(">BHHB", fh.read(6))
            return {"width": width, "height": height, "bit_depth": precision, "components": components,
                    "mode": {1: "grey", 3: "colour", 4: "cmyk"}.get(components, str(components)),
                    "progressive": marker == 0xC2}
        if marker == 0xDA:
            break
        fh.seek(length - 2, 1)
    raise ValueError("no frame header before the scan")


def _store(url: str, io: IO) -> dict:
    """A url ending in `/`: the Zarr or N5 metadata documents found there and, for a multiscale group, the array
    metadata of the datasets it names; a directory without them as its index's entries."""
    def document(at: str):
        try:
            with io.open(at) as fh:
                if fh.seek(0, 2) > 1 << 20:
                    return "over 1 MiB, not read"
                fh.seek(0)
                return json.loads(fh.read())
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 404):   # 403 is what a bucket without listing answers for no key
                return None
            raise
        except ValueError:
            return "not JSON"

    found = {name: doc for name in ZARR_META if (doc := document(url + name)) is not None}
    if not found:
        names = io.list(url)
        return {"format": "directory", "entries": len(names), "names": names}
    zarr = [name for name in found if name != "attributes.json"]
    out: dict = {"format": "zarr" if zarr else "n5", "metadata": found}
    documents = [found.get(".zattrs"), found.get("zarr.json")]
    attributes = [doc.get("attributes") if name == "zarr.json" else doc
                  for name, doc in zip((".zattrs", "zarr.json"), documents) if isinstance(doc, dict)]
    scales: list[dict] = []
    for attrs in attributes:
        if isinstance(attrs, dict):   # OME-Zarr 0.4 names its multiscales at the top, 0.5 and later under `ome`
            ome = attrs.get("ome") if isinstance(attrs.get("ome"), dict) else {}
            scales += _dicts(attrs.get("multiscales") or ome.get("multiscales"))
    paths = [str(dataset.get("path")) for scale in scales for dataset in _dicts(scale.get("datasets"))][:CHANNELS]
    if paths:
        out["arrays"] = {path: next((doc for name in (".zarray", "zarr.json", "attributes.json")
                                     if (doc := document(f"{url}{path}/{name}")) is not None), None)
                         for path in paths}
    return out


def _dicts(value: Any) -> list[dict]:
    """The mappings in `value` when it is a list; a file's metadata may hold anything where a list is expected."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# --- a binding's arrays ----------------------------------------------------------------------------------------

def describe_arrays(binding_path: Path, snapshot: Path, record_path: Path | None, io: IO, *,
                    planes: int = PLANES) -> dict:
    """What the arrays of a binding measure, opened through their snippets as smoke opens them. Each label array,
    on `planes` planes evenly spaced through z (the central WINDOW x WINDOW pixels of each): the fraction of
    nonzero pixels, the distinct ids and how many 4-connected components each id has. One component per id is
    what instance ids look like; a few ids with many components each is what semantic ids look like. Each raw
    array, on its middle plane: intensity percentiles per channel. With a record, smoke's checks of dtypes and
    of labels against their raws are reported too. What was measured before the byte cap is kept."""
    def work(out: dict) -> None:
        binding = load_binding(binding_path)
        raws, labels = open_arrays(binding, Path(snapshot), io)
        if record_path:
            record = yaml.safe_load(Path(record_path).read_text()) or {}
            out["warnings"], mismatched = check_binding(binding, record, raws, labels)
            out["errors"] += mismatched
        for index, (bound_array, array) in enumerate(zip(binding.raw, raws)):
            entry = {"index": index, "snippet": bound_array.snippet, **_layout(array), "channels": []}
            out["raw"].append(entry)
            middle = _planes(array, 1)[0]
            entry["plane"] = middle
            for channel in range(min(dict(zip(array.axes, array.shape)).get("c", 1), CHANNELS)):
                _measure(out, f"raw {index}", lambda: entry["channels"].append(
                    {"c": channel, **_intensity(_window(array, {**middle, "c": channel}))}))
        for index, (bound_label, array) in enumerate(zip(binding.labels, labels)):
            entry = {"index": index, "label_key": bound_label.label_key, "pairs_with": bound_label.pairs_with,
                     "snippet": bound_label.snippet, **_layout(array), "planes": []}
            out["labels"].append(entry)
            for plane in _planes(array, planes):
                _measure(out, f"label {index}", lambda: entry["planes"].append(
                    {"plane": plane, **label_stats(_window(array, plane))}))

    return _report(io, {"ok": False, "raw": [], "labels": [], "warnings": [], "errors": [], "bytes_fetched": 0},
                   work)


def _measure(out: dict, what: str, step) -> None:
    """Run one measurement; what a snippet raises is that array's error and the others go on. The byte cap and
    a host that is not allowed end the whole describe."""
    try:
        step()
    except (BudgetExceeded, HostNotAllowed):
        raise
    except Exception as e:
        out["errors"].append(f"{what}: {type(e).__name__}: {e}")


def _layout(array: Array) -> dict:
    return {"axes": array.axes, "shape": list(array.shape), "dtype": np.dtype(array.dtype).name,
            "voxel_size_nm": array.voxel_size_nm, "access": array.access}


def _planes(array: Array, count: int) -> list[dict]:
    """`count` planes evenly spaced through z, each once; the one plane of an array without z."""
    depth = dict(zip(array.axes, array.shape)).get("z")
    if depth is None:
        return [{}]
    return [{"z": z} for z in sorted({int((k + 0.5) * depth / max(count, 1)) for k in range(max(count, 1))})]


def _window(array: Array, plane: dict) -> np.ndarray:
    """The central WINDOW x WINDOW pixels of a plane (the whole plane when it is smaller)."""
    sizes = dict(zip(array.axes, array.shape))
    box = [slice((sizes[axis] - min(sizes[axis], WINDOW)) // 2, (sizes[axis] + min(sizes[axis], WINDOW)) // 2)
           for axis in "yx"]
    return np.asarray(array.read(_region(array, plane, *box)))


def _intensity(patch: np.ndarray) -> dict:
    low, median, high = (float(v) for v in np.percentile(patch, [1, 50, 99]))
    return {"min": float(patch.min()), "p1": low, "p50": median, "p99": high, "max": float(patch.max())}


def label_stats(patch: np.ndarray) -> dict:
    """One label plane: the fraction of nonzero pixels, the distinct nonzero values and, per value, its number of
    4-connected components, summed up as how many values have exactly one, and the median and largest count."""
    if patch.ndim != 2:
        raise ValueError(f"a label plane read back with shape {list(patch.shape)}")
    values, dense = np.unique(patch, return_inverse=True)
    dense = dense.reshape(patch.shape)
    zero = np.flatnonzero(values == 0)
    counts = components_per_id(dense, int(zero[0]) if zero.size else None, values.size)
    counts = counts[values != 0]
    out: dict = {"fraction_nonzero": float(np.count_nonzero(patch) / patch.size), "ids": int(counts.size)}
    if counts.size:
        ids = values[values != 0]
        out.update(id_min=ids.min(), id_max=ids.max(), one_component_ids=int((counts == 1).sum()),
                   median_components=float(np.median(counts)), max_components=int(counts.max()))
    return out


def components_per_id(dense: np.ndarray, background: int | None, ids: int) -> np.ndarray:
    """For a 2D array of id indices in 0..ids-1: the number of 4-connected components of each index, 0 for
    `background`. Pixels are joined to their right and lower neighbours of the same index, and each joined set
    is counted once, at the pixel that stays its own parent."""
    index = np.arange(dense.size).reshape(dense.shape)
    inside = np.ones(dense.shape, bool) if background is None else dense != background
    right = (dense[:, 1:] == dense[:, :-1]) & inside[:, 1:]
    down = (dense[1:] == dense[:-1]) & inside[1:]
    a = np.concatenate([index[:, :-1][right], index[:-1][down]])
    b = np.concatenate([index[:, 1:][right], index[1:][down]])
    parent = index.ravel().copy()
    while True:
        root_a, root_b = parent[a], parent[b]
        differ = root_a != root_b
        if not differ.any():
            break
        np.minimum.at(parent, np.maximum(root_a, root_b)[differ], np.minimum(root_a, root_b)[differ])
        while not np.array_equal(following := parent[parent], parent):   # every pixel points at its set's root
            parent = following
    roots = np.flatnonzero((parent == index.ravel()) & inside.ravel())
    return np.bincount(dense.ravel()[roots], minlength=ids)
