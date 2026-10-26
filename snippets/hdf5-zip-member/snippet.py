"""One HDF5 dataset (chosen by `key`) from a zip member or a plain URL, read with h5py.

A stored zip member or a plain file on a Range-capable server is read through `io.open` with h5py's
file-object driver; a deflated member (or a server without Range) is staged whole with `io.fetch`.
"""
from pathlib import Path
from typing import BinaryIO

import h5py
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

DEFAULT_AXES = {2: "yx", 3: "zyx", 4: "czyx"}   # by dataset rank, unless params.axes says otherwise
UM_TO_NM = 1e3


class _Dataset:
    def __init__(self, f: h5py.File, key: str, access: str, axes: str | None, element_size: bool):
        self._f = f
        ds = f.get(key)
        if not isinstance(ds, h5py.Dataset):
            raise KeyError(f"no dataset {key!r} in the file; datasets: {_datasets(f)}")
        self._ds = ds
        self.shape = tuple(int(n) for n in ds.shape)
        self.dtype = np.dtype(ds.dtype)
        if axes is None:
            axes = DEFAULT_AXES.get(ds.ndim)
            if axes is None:
                raise ValueError(f"dataset {key!r} has rank {ds.ndim} {self.shape}; set params.axes "
                                 "(one czyx letter per dimension, with a reason in the binding)")
        if len(axes) != ds.ndim:
            raise ValueError(f"params.axes {axes!r} has {len(axes)} letters, dataset {key!r} has rank {ds.ndim}")
        self.axes = axes      # check_array (run by the harness) checks letters and uniqueness
        self.access = access if access == "file" else ("chunk" if ds.chunks else "plane")
        self.voxel_size_nm = _voxel_size(ds, axes) if element_size else None

    def __del__(self) -> None:      # the harness never closes arrays; the file handle goes with the array
        try:
            self._f.close()
        except Exception:
            pass

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        idx = tuple(_index(region.get(a, slice(None)), n) for a, n in zip(self.axes, self.shape))
        return np.asarray(self._ds[idx])


def _index(sel: int | slice, n: int) -> int | slice:
    """Normalise one axis selection for h5py: non-negative ints, positive-step slices with stop >= start."""
    if isinstance(sel, slice):
        start, stop, step = sel.indices(n)
        if step < 1:
            raise ValueError(f"h5py reads only positive slice steps, got {sel}")
        return slice(start, max(start, stop), step)
    return range(n)[sel]        # IndexError when out of range, like numpy


def _datasets(f: h5py.File) -> list[str]:
    names: list[str] = []
    f.visititems(lambda name, obj: names.append(name) if isinstance(obj, h5py.Dataset) else None)
    return names


def _voxel_size(ds: h5py.Dataset, axes: str) -> dict[str, float] | None:
    """`element_size_um` (ImageJ HDF5 / ilastik convention: µm per spatial axis, zyx order) on the dataset,
    else on the file root; None when absent or when its length does not match the spatial axes."""
    spatial = [a for a in axes if a in "zyx"]
    for attrs in (ds.attrs, ds.file.attrs):
        if "element_size_um" in attrs:
            vals = [float(v) for v in np.ravel(attrs["element_size_um"])]
            if len(vals) == len(spatial) and all(v > 0 for v in vals):
                return {a: v * UM_TO_NM for a, v in zip(spatial, vals)}
            return None
    return None


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"hdf5-zip-member reads one file, got {len(src.urls)} urls")
    key = src.params.get("key")
    if not isinstance(key, str) or not key:
        raise ValueError("hdf5-zip-member needs params.key, the HDF5 dataset path (e.g. 'raw' or '/label')")
    try:
        fh: BinaryIO = io.open(src.urls[0])
        f, access = h5py.File(fh, "r"), "chunk"
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        path: Path = io.fetch(src.urls[0])
        f, access = h5py.File(path, "r", locking=False), "file"
    return _Dataset(f, key, access, src.params.get("axes"), not src.params.get("ignore_element_size_um", False))
