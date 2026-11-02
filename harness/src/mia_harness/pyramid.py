"""OME-Zarr pyramids on zarr v3: sparse 0.6 written from a stage plan, read back (0.5 or 0.6) in czyx order."""
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import zarr

from .api import Array
from .plan import StagePlan

CANON = "czyx"
UNIT_NM = {None: 1.0, "nanometer": 1.0, "nm": 1.0, "micrometer": 1e3, "um": 1e3, "µm": 1e3, "micron": 1e3,
           "millimeter": 1e6, "mm": 1e6}


@dataclass(frozen=True)
class LevelInfo:
    path: str
    factor: int
    shape: tuple[int, ...]              # canonical axis order
    scale_nm: dict[str, float]          # spatial axes only
    translation_nm: dict[str, float]
    origin: dict[str, int]              # the level's box origin in raw index space, spatial axes only


@dataclass(frozen=True)
class PyramidInfo:
    axes: str
    shape: tuple[int, ...]
    dtype: str
    levels: list[LevelInfo]


def block_mean(plane: np.ndarray, f: int) -> np.ndarray:
    """f×f block mean of a 2-D plane, remainder trimmed, rounded back to an integer dtype."""
    if f < 1:
        raise ValueError(f"factor must be >= 1, got {f}")
    if f == 1:
        return plane
    h, w = plane.shape[0] // f * f, plane.shape[1] // f * f
    mean = plane[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3), dtype=np.float64)   # no float64 copy of the plane
    return (np.rint(mean) if np.issubdtype(plane.dtype, np.integer) else mean).astype(plane.dtype)


def stride(plane: np.ndarray, f: int) -> np.ndarray:
    """Nearest-neighbour downsample: one sample per f×f block (offset (f-1)//2), remainder trimmed."""
    if f < 1:
        raise ValueError(f"factor must be >= 1, got {f}")
    h, w = plane.shape[0] // f * f, plane.shape[1] // f * f
    o = (f - 1) // 2
    return plane[o:h:f, o:w:f]


def _canonical(axes: str) -> str:
    return "".join(a for a in CANON if a in axes)


def _axis_meta(axis: str) -> dict:
    return {"name": "c", "type": "channel"} if axis == "c" else {"name": axis, "type": "space", "unit": "nanometer"}


def _read_yx(array: Array, outer: dict[str, int], rows: slice, cols: slice) -> np.ndarray:
    """The (y, x) block of `array` at the fixed `outer` (c, z) indices, whichever order the source keeps y and x in."""
    size = dict(zip(array.axes, array.shape))
    extent = {"y": len(range(size["y"])[rows]), "x": len(range(size["x"])[cols])}
    plane_axes = [a for a in array.axes if a in "yx"]
    block = np.asarray(array.read(outer | {"y": rows, "x": cols})).reshape(extent[plane_axes[0]], extent[plane_axes[1]])
    return block.T if plane_axes == ["x", "y"] else block


def write_pyramid(array: Array, out: Path, *, plan: StagePlan, voxel_size_nm: dict[str, float],
                  kind: Literal["raw", "label"], coordinate_system: str, mia: dict,
                  downsampling_metadata: dict) -> PyramidInfo:
    """Write the chunks `plan` names, and no others, of `array` as a sparse OME-Zarr 0.6 multiscale group at `out`.

    Each level holds its plan's box (origin in raw index space), levels scale y and x only. Raw levels are block
    means, label levels nearest samples. Chunks are one plane of patch×patch, written even when all zero, so a
    missing chunk always means "not planned". The half-pixel translation per level follows the lead's crops.
    """
    axes = _canonical(array.axes)
    size = dict(zip(array.axes, array.shape))
    missing = [a for a in axes if a != "c" and a not in voxel_size_nm]
    if missing:
        raise ValueError(f"voxel size unknown for axes {missing}")
    dtype = np.dtype(array.dtype)
    if kind == "label" and dtype == np.bool_:
        dtype = np.dtype(np.uint8)
    elif kind == "label" and not np.issubdtype(dtype, np.integer):
        raise ValueError(f"label dtype {dtype} is not an integer type")
    channels = size.get("c", 1)
    patch = plan.patch
    outer = [a for a in axes if a in "cz"]
    down = block_mean if kind == "raw" else stride
    group = zarr.open_group(Path(out), mode="w", zarr_format=3)
    infos, arrays = [], []
    # the chunks of every level, grouped by the raw (c, z) plane they are cut from
    todo: dict[tuple[int, ...], list[tuple[int, tuple[int, ...], int, int]]] = defaultdict(list)
    for i, level in enumerate(plan.levels):
        factor = level.factor
        shape = tuple(channels if a == "c" else n for a, n in zip(axes, level.shape))
        chunks = tuple(1 if a in "cz" else patch for a in axes)
        arrays.append(group.create_array(name=f"s{i}", shape=shape, chunks=chunks, dtype=dtype,
                                         config={"write_empty_chunks": True}))
        scale = {a: voxel_size_nm[a] * (factor if a in "yx" else 1) for a in axes if a != "c"}
        translation = {a: (level.origin[a] + ((factor - 1) / 2 if a in "yx" else 0)) * voxel_size_nm[a]
                       for a in axes if a != "c"}
        infos.append(LevelInfo(f"s{i}", factor, shape, scale, translation, dict(level.origin)))
        for chunk in level.chunks:
            at = dict(zip(axes, chunk))
            for channel in range(channels):
                stored = tuple(channel if a == "c" else at["z"] for a in outer)
                raw = tuple(channel if a == "c" else at["z"] + level.origin["z"] for a in outer)
                todo[raw].append((i, stored, at["y"], at["x"]))
    # ponytail: a plane-access source holds a whole plane in memory; tile it when a plane exceeds a few GB.
    for raw in sorted(todo):
        region = dict(zip(outer, raw))
        plane = _read_yx(array, region, slice(None), slice(None)) if getattr(array, "access", None) == "plane" else None
        for i, stored, cy, cx in sorted(todo[raw]):
            level, factor = plan.levels[i], plan.levels[i].factor
            ny, nx = level.shape[-2:]
            y0, x0 = cy * patch, cx * patch
            y1, x1 = min(y0 + patch, ny), min(x0 + patch, nx)
            rows = slice(level.origin["y"] + y0 * factor, level.origin["y"] + y1 * factor)
            cols = slice(level.origin["x"] + x0 * factor, level.origin["x"] + x1 * factor)
            block = plane[rows, cols] if plane is not None else _read_yx(array, region, rows, cols)
            arrays[i][stored + (slice(y0, y1), slice(x0, x1))] = down(block, factor).astype(dtype, copy=False)
    datasets = [{"path": info.path, "coordinateTransformations": [{
        "type": "sequence", "input": {"path": info.path}, "output": {"name": coordinate_system},
        "transformations": [{"type": "scale", "scale": [1.0 if a == "c" else info.scale_nm[a] for a in axes]},
                            {"type": "translation",
                             "translation": [0.0 if a == "c" else info.translation_nm[a] for a in axes]}]}]}
        for info in infos]
    ome: dict = {"version": "0.6", "multiscales": [{
        "name": mia.get("label_key", kind), "type": "mean" if kind == "raw" else "nearest",
        "metadata": downsampling_metadata,
        "coordinateSystems": [{"name": coordinate_system, "axes": [_axis_meta(a) for a in axes]}],
        "datasets": datasets}]}
    if kind == "label":
        ome["image-label"] = {"source": {"image": "../../raw"}}
        if mia.get("segmentation_type") == "binary":
            ome["image-label"]["colors"] = [{"label-value": 0, "rgba": [0, 0, 0, 0]},
                                            {"label-value": 1, "rgba": [255, 255, 255, 255]}]
    group.attrs["ome"] = ome
    group.attrs["mia"] = mia
    return PyramidInfo(axes, tuple(size[a] for a in axes), dtype.name, infos)


def write_volume_groups(vdir: Path, labels: list[str], mia_summary: dict) -> None:
    """Make `vdir` one OME-Zarr 0.6 image: the root group's `multiscales` is raw's with paths under raw/, and with
    labels the root lists them and `labels/` is the spec's labels group. The arrays and the raw/label groups are
    untouched."""
    raw = json.loads((vdir / "raw" / "zarr.json").read_text())["attributes"]["ome"]["multiscales"][0]
    datasets = [{"path": f"raw/{d['path']}",
                 "coordinateTransformations": [{**t, "input": {"path": f"raw/{d['path']}"}}
                                               for t in d["coordinateTransformations"]]}
                for d in raw["datasets"]]
    ome: dict = {"version": "0.6", "multiscales": [{**raw, "datasets": datasets}]}
    if labels:
        ome["labels"] = list(labels)
        _write_group(vdir / "labels", {"ome": {"version": "0.6", "labels": list(labels)}})
    _write_group(vdir, {"ome": ome, "mia": mia_summary})


def _write_group(path: Path, attributes: dict) -> None:
    (path / "zarr.json").write_text(json.dumps({"zarr_format": 3, "node_type": "group", "attributes": attributes}, indent=1))


class Pyramid:
    """An NGFF multiscale group opened for reading; regions are addressed and returned in canonical axis order."""

    def __init__(self, path: Path):
        self.path = Path(path)
        group = zarr.open_group(self.path, mode="r")
        self.attrs = dict(group.attrs)
        ms = (self.attrs.get("ome") or self.attrs)["multiscales"][0]
        axes_meta = ms["axes"] if "axes" in ms else ms["coordinateSystems"][0]["axes"]   # 0.5, else 0.6
        self.storage_axes = [a["name"] for a in axes_meta]
        if any(a not in CANON for a in self.storage_axes):
            raise ValueError(f"unsupported axes {self.storage_axes}; only {CANON} are served")
        self.axes = _canonical("".join(self.storage_axes))
        self._perm = [self.storage_axes.index(a) for a in self.axes]   # storage position of each canonical axis
        units = {a["name"]: UNIT_NM[a.get("unit")] for a in axes_meta if a["name"] != "c"}
        self._arrays, self.levels = [], []
        for ds in ms["datasets"]:
            arr = group[ds["path"]]
            scale = translation = None
            # ponytail: a 0.6 sequence is flattened, origin left 0 (translation_nm carries it); Task 5 reads it
            flat = [u for t in ds["coordinateTransformations"] for u in t.get("transformations", [t])]
            for t in flat:
                if t["type"] == "scale":
                    scale = t["scale"]
                elif t["type"] == "translation":
                    translation = t["translation"]
            if scale is None:
                raise ValueError(f"{ds['path']}: no scale transformation")
            translation = translation or [0.0] * len(scale)
            spatial = {a: scale[i] * units[a] for i, a in enumerate(self.storage_axes) if a != "c"}
            shift = {a: translation[i] * units[a] for i, a in enumerate(self.storage_axes) if a != "c"}
            self._arrays.append(arr)
            origin = {a: 0 for a in self.axes if a != "c"}
            shape = tuple(arr.shape[i] for i in self._perm)
            self.levels.append(LevelInfo(ds["path"], 0, shape, spatial, shift, origin))
        base = self.levels[0].scale_nm["x"]
        self.levels = [LevelInfo(lv.path, int(round(lv.scale_nm["x"] / base)), lv.shape, lv.scale_nm, lv.translation_nm,
                                 lv.origin)
                       for lv in self.levels]
        self.dtype = self._arrays[0].dtype

    def factor_of(self, level: int) -> int:
        return self.levels[level].factor

    def read(self, level: int, region: dict) -> np.ndarray:
        index = tuple(region.get(a, slice(None)) for a in self.storage_axes)
        data = np.asarray(self._arrays[level][index])
        kept = [a for a, i in zip(self.storage_axes, index) if isinstance(i, slice)]
        order = [kept.index(a) for a in self.axes if a in kept]
        return data.transpose(order) if order != list(range(len(order))) else data


def open_pyramid(path: Path) -> Pyramid:
    return Pyramid(path)
