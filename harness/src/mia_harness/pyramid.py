"""NGFF 0.5 pyramids on zarr v3: written one plane at a time from an Array, read back in canonical czyx order."""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import zarr

from .api import Array

OME_VERSION = "0.5"
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


def write_pyramid(array: Array, out: Path, *, levels: list[int], patch: int, voxel_size_nm: dict[str, float],
                  kind: Literal["raw", "label"], attrs: dict) -> PyramidInfo:
    """Write `array` as an NGFF 0.5 multiscale group at `out`, levels scaling y and x only.

    Raw levels are block means, label levels nearest samples. Chunks are one plane of patch×patch. The half-pixel
    translation per level follows the lead's existing crops.
    """
    axes = _canonical(array.axes)
    size = dict(zip(array.axes, array.shape))
    missing = [a for a in axes if a != "c" and a not in voxel_size_nm]
    if missing:
        raise ValueError(f"voxel size unknown for axes {missing}")
    outer = [a for a in axes if a in "cz"]
    down = block_mean if kind == "raw" else stride
    group = zarr.open_group(Path(out), mode="w", zarr_format=3)
    infos, arrays = [], []
    for i, f in enumerate(levels):
        shape = tuple(size[a] if a in "cz" else size[a] // f for a in axes)
        chunks = tuple(1 if a in "cz" else patch for a in axes)
        arrays.append(group.create_array(name=f"s{i}", shape=shape, chunks=chunks, dtype=array.dtype))
        scale = {a: (voxel_size_nm[a] * f if a in "yx" else voxel_size_nm[a]) for a in axes if a != "c"}
        translation = {a: (voxel_size_nm[a] * (f - 1) / 2 if a in "yx" else 0.0) for a in axes if a != "c"}
        infos.append(LevelInfo(f"s{i}", f, shape, scale, translation))
    # one (c, z) plane at a time through the snippet, all levels of it written before the next
    # ponytail: a whole plane is held in memory; tile it when a plane exceeds a few GB.
    plane_axes = [a for a in array.axes if a in "yx"]
    ranges = [range(size[a]) for a in outer]
    for index in np.ndindex(*[len(r) for r in ranges]) if outer else [()]:
        region = {a: int(i) for a, i in zip(outer, index)} | {"y": slice(None), "x": slice(None)}
        plane = np.asarray(array.read(region)).reshape(size[plane_axes[0]], size[plane_axes[1]])
        if plane_axes == ["x", "y"]:
            plane = plane.T
        for arr, f in zip(arrays, levels):
            arr[tuple(index)] = down(plane, f)
    datasets = []
    for info in infos:
        transforms = [{"type": "scale", "scale": [1.0 if a == "c" else info.scale_nm[a] for a in axes]}]
        if info.factor != 1:
            transforms.append({"type": "translation", "translation": [0.0 if a == "c" else info.translation_nm[a] for a in axes]})
        datasets.append({"path": info.path, "coordinateTransformations": transforms})
    ome = {"version": OME_VERSION, "multiscales": [{"name": kind, "axes": [_axis_meta(a) for a in axes], "datasets": datasets}]}
    if kind == "label":
        ome["image-label"] = {"version": OME_VERSION, "source": {"image": "../../raw"}}
    group.attrs["ome"] = ome
    group.attrs["mia"] = {**attrs, "kind": kind, "levels": list(levels), "patch": patch, "source_axes": array.axes,
                          "access": getattr(array, "access", None)}
    return PyramidInfo(axes, tuple(size[a] for a in axes), np.dtype(array.dtype).name, infos)


def write_volume_groups(vdir: Path, labels: list[str]) -> None:
    """Make `vdir` one OME-Zarr image: the root group's `multiscales` points at raw/s*, and with labels the root
    lists them and `labels/` is the spec's labels group. The arrays and the raw/label groups are untouched."""
    raw = json.loads((vdir / "raw" / "zarr.json").read_text())["attributes"]["ome"]["multiscales"][0]
    datasets = [{**d, "path": f"raw/{d['path']}"} for d in raw["datasets"]]
    ome: dict = {"version": OME_VERSION,
                 "multiscales": [{"name": raw.get("name", "raw"), "type": "image", "axes": raw["axes"], "datasets": datasets}]}
    if labels:
        ome["labels"] = list(labels)
        _write_group(vdir / "labels", {"ome": {"version": OME_VERSION, "labels": list(labels)}})
    _write_group(vdir, {"ome": ome})


def _write_group(path: Path, attributes: dict) -> None:
    (path / "zarr.json").write_text(json.dumps({"zarr_format": 3, "node_type": "group", "attributes": attributes}, indent=1))


class Pyramid:
    """An NGFF multiscale group opened for reading; regions are addressed and returned in canonical axis order."""

    def __init__(self, path: Path):
        self.path = Path(path)
        group = zarr.open_group(self.path, mode="r")
        self.attrs = dict(group.attrs)
        ms = (self.attrs.get("ome") or self.attrs)["multiscales"][0]
        self.storage_axes = [a["name"] for a in ms["axes"]]
        if any(a not in CANON for a in self.storage_axes):
            raise ValueError(f"unsupported axes {self.storage_axes}; only {CANON} are served")
        self.axes = _canonical("".join(self.storage_axes))
        self._perm = [self.storage_axes.index(a) for a in self.axes]   # storage position of each canonical axis
        units = {a["name"]: UNIT_NM[a.get("unit")] for a in ms["axes"] if a["name"] != "c"}
        self._arrays, self.levels = [], []
        for ds in ms["datasets"]:
            arr = group[ds["path"]]
            scale = translation = None
            for t in ds["coordinateTransformations"]:
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
            self.levels.append(LevelInfo(ds["path"], 0, tuple(arr.shape[i] for i in self._perm), spatial, shift))
        base = self.levels[0].scale_nm["x"]
        self.levels = [LevelInfo(lv.path, int(round(lv.scale_nm["x"] / base)), lv.shape, lv.scale_nm, lv.translation_nm)
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
