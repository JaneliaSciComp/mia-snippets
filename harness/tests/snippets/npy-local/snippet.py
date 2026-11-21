"""TEST ONLY: a .npy array from params.path (local, bypasses io) or from urls[0] through io."""
import numpy as np

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1


class _Npy:
    voxel_size_nm = None

    def __init__(self, data: np.ndarray, axes: str, access: str):
        self.data, self.axes, self.shape, self.dtype, self.access = data, axes, data.shape, data.dtype, access

    def read(self, region):
        return np.asarray(self.data[tuple(region.get(a, slice(None)) for a in self.axes)])


class _Planes:
    """A .npy read one z plane per request, by range: what the reader of a tiled or chunked format does."""
    voxel_size_nm = None

    def __init__(self, io: IO, url: str, axes: str | None, access: str):
        self._io, self._url, self.access = io, url, access
        with io.open(url) as fh:
            np.lib.format.read_magic(fh)
            self.shape, _, self.dtype = np.lib.format.read_array_header_1_0(fh)
            self._offset = fh.tell()
        self.axes = axes or "zyx"

    def read(self, region):
        z, *rest = (region.get(a, slice(None)) for a in self.axes)
        size = int(np.prod(self.shape[1:])) * self.dtype.itemsize
        planes = []
        with self._io.open(self._url) as fh:
            for k in (range(*z.indices(self.shape[0])) if isinstance(z, slice) else [z]):
                fh.seek(self._offset + k * size)
                planes.append(np.frombuffer(fh.read(size), self.dtype).reshape(self.shape[1:])[tuple(rest)])
        return np.stack(planes) if isinstance(z, slice) else planes[0]


def open_array(src: Source, io: IO) -> Array:
    axes, access = src.params.get("axes"), src.params.get("access", "chunk")
    if src.params.get("lazy"):
        return _Planes(io, src.urls[0], axes, access)
    if "path" in src.params:
        data = np.load(src.params["path"], mmap_mode="r")
    else:
        try:
            with io.open(src.urls[0]) as fh:   # ponytail: reads the whole file at open; fine for a test snippet
                data = np.load(fh)
        except NeedsFetch:                     # a server that ignores Range: the file whole, as real readers do
            data = np.load(io.fetch(src.urls[0]), mmap_mode="r")
    return _Npy(data, axes or {2: "yx", 3: "zyx"}[data.ndim], access)
