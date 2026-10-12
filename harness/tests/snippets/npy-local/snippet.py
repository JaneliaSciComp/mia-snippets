"""TEST ONLY: a .npy array from params.path (local, bypasses io) or from urls[0] through io."""
import numpy as np

from mia_harness.api import IO, Array, Source

SPEC_VERSION = 1


class _Npy:
    voxel_size_nm = None
    access = "chunk"

    def __init__(self, data: np.ndarray, axes: str):
        self.data, self.axes, self.shape, self.dtype = data, axes, data.shape, data.dtype

    def read(self, region):
        return np.asarray(self.data[tuple(region.get(a, slice(None)) for a in self.axes)])


def open_array(src: Source, io: IO) -> Array:
    if "path" in src.params:
        data = np.load(src.params["path"], mmap_mode="r")
    else:
        data = np.load(io.open(src.urls[0]))  # ponytail: reads the whole file at open; fine for a test snippet
    return _Npy(data, src.params.get("axes") or {2: "yx", 3: "zyx"}[data.ndim])
