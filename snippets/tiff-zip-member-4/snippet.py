"""One TIFF (multi-page or single) from a zip member or a plain URL, read with tifffile; optionally one channel
of it (`channel`), only a run of its z planes (`z_first`, `z_last`) and only a run of its rows (`y_first`,
`y_last`). A stack whose planes are stored one after another behind a single IFD is read by byte offset.

tiff-zip-member-3 reads plane k as page k of the file. A classic TIFF cannot address data past 4 GiB, so
ImageJ saves a larger stack with one IFD, for plane 0, and writes the other planes directly after it; the
number of planes is in the ImageJ description (`images=`, with `channels=`, `slices=`). Such a file has one
page, tifffile presents its series with the full shape, and plane k is the `plane bytes` at `plane 0's
offset + k * plane bytes`. This snippet reads those bytes (only the rows asked for) where tiff-zip-member-3
fails with an IndexError on every plane but the first. Any other file, and every param, is read exactly as
tiff-zip-member-3 reads it."""
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXIS = {"Z": "z", "Q": "z", "I": "z", "C": "c", "S": "c", "Y": "y", "X": "x"}   # tifffile letter -> czyx
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form
RUNS = {"z": "z planes", "y": "rows"}   # the axes a binding may give a run of, as `<axis>_first`/`<axis>_last`


class _Tiff:
    def __init__(self, tif: tifffile.TiffFile, access: str, axes: str | None, channel: int | None,
                 runs: dict[str, tuple[int | None, int | None]]):
        self.tif, self.access = tif, access
        s = tif.series[0]
        page = tuple(s.keyframe.shape)
        if tuple(s.shape[len(s.shape) - len(page):]) != page:
            raise ValueError(f"series shape {s.shape} does not end in its page shape {page}")
        self.dtype = np.dtype(s.dtype)
        self._outer = tuple(s.shape[:len(s.shape) - len(page)])   # dims that index pages
        self._page = page
        planes = int(np.prod(self._outer))
        # truncated: one page for several planes, which follow page 0's data (ImageJ over 4 GiB); else a page each
        self._plane0: int | None = _plane0_offset(tif, planes) if s.is_truncated and planes > 1 else None
        self._ndim = len(s.shape)
        self._keep = [i for i, (a, n) in enumerate(zip(s.axes, s.shape)) if n > 1 or a in "YX"]
        full_shape = tuple(s.shape[i] for i in self._keep)
        if axes is None:
            axes = "".join(AXIS.get(s.axes[i], "?") for i in self._keep)
            if "?" in axes or len(set(axes)) != len(axes):
                raise ValueError(f"tifffile axes {s.axes!r} {tuple(s.shape)} do not map to czyx; "
                                 "set params.axes (with a reason in the binding)")
        if len(axes) != len(full_shape):
            raise ValueError(f"params.axes {axes!r} has {len(axes)} letters for {len(full_shape)} axes {full_shape}")
        self._full_axes = axes
        sizes = dict(zip(axes, full_shape))
        self._channel = channel
        if channel is not None:
            if "c" not in axes:
                raise ValueError(f"params.channel is set, but tifffile axes {s.axes!r} {tuple(s.shape)} map to "
                                 f"{axes!r}, which has no channel axis")
            if not 0 <= channel < sizes["c"]:
                raise ValueError(f"params.channel {channel} out of range for {sizes['c']} channels")
            del sizes["c"]
        self._first: dict[str, int] = {}               # axis -> the file index of the array's index 0 on it
        for a, (first, last) in runs.items():
            if first is None and last is None:
                continue
            if a not in sizes:
                raise ValueError(f"params.{a}_first/{a}_last are set, but tifffile axes {s.axes!r} "
                                 f"{tuple(s.shape)} map to {axes!r}, which has no {a} axis")
            lo, hi = first or 0, sizes[a] - 1 if last is None else last
            if not 0 <= lo <= hi < sizes[a]:
                raise ValueError(f"params.{a}_first {lo} .. {a}_last {hi} is not a run of the file's "
                                 f"{sizes[a]} {RUNS[a]} (0-based, both ends included)")
            self._first[a], sizes[a] = lo, hi - lo + 1
        self.axes = "".join(sizes)      # check_array (run by the harness) checks letters and length
        self.shape = tuple(sizes.values())
        self.voxel_size_nm = _voxel_size(tif, self.axes)

    def __del__(self):          # the harness never closes arrays; a staged file's handle goes with the array
        self.tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        full: dict[str, int | slice] = dict(region)
        for a, first in self._first.items():
            full[a] = _shift(a, full.get(a, slice(None)), first, self.shape[self.axes.index(a)])
        if self._channel is not None:
            full["c"] = self._channel
        idx: list[int | slice] = [0] * self._ndim                  # dropped size-1 axes are index 0
        for i, a in zip(self._keep, self._full_axes):
            idx[i] = full.get(a, slice(None))
        n = len(self._outer)
        keys = np.arange(int(np.prod(self._outer))).reshape(self._outer)[tuple(idx[:n])]
        if self._plane0 is None:
            pages = [self.tif.asarray(key=int(k), series=0)[tuple(idx[n:])] for k in np.ravel(keys)]
        else:
            pages = [self._plane(int(k), idx[n], tuple(idx[n + 1:])) for k in np.ravel(keys)]
        return np.stack(pages).reshape(np.shape(keys) + pages[0].shape)

    def _plane(self, k: int, sel: int | slice, rest: tuple[int | slice, ...]) -> np.ndarray:
        """Plane k of a stack stored behind one IFD, indexed by `sel` on the page's first axis (its rows) and by
        `rest` on the others: one read of the rows from the first to the last asked for, at the plane's place in
        the file."""
        rows = self._page[0]
        picked = range(rows)[sel] if isinstance(sel, slice) else [range(rows)[sel]]   # an int out of range raises
        if len(picked) == 0:
            return np.empty((0,) + self._page[1:], self.dtype)[(slice(None),) + rest]
        lo, hi = min(picked[0], picked[-1]), max(picked[0], picked[-1]) + 1
        row = int(np.prod(self._page[1:])) * self.dtype.itemsize
        fh = self.tif.filehandle
        with fh.lock:
            fh.seek(self._plane0 + k * rows * row + lo * row)
            data = fh.read((hi - lo) * row)
        if len(data) != (hi - lo) * row:
            raise OSError(f"plane {k}: {len(data)} of {(hi - lo) * row} bytes read; the file ends early")
        block = np.frombuffer(data, self.dtype.newbyteorder(self.tif.byteorder)).reshape((hi - lo,) + self._page[1:])
        block = block.astype(self.dtype, copy=False)                  # native byte order, as tifffile returns pages
        if not isinstance(sel, slice):
            return block[0][rest]
        return (block if picked.step == 1 else block[np.asarray(picked) - lo])[(slice(None),) + rest]


def _plane0_offset(tif: tifffile.TiffFile, planes: int) -> int:
    """The file offset of plane 0 of a truncated series: its `planes` planes are stored one after another
    behind the file's only page. Raises ValueError unless the page is plain contiguous samples and the file is
    long enough to hold every plane."""
    s = tif.series[0]
    page = s.keyframe
    what = f"series 0 has one page for its {planes} planes (shape {tuple(s.shape)}, axes {s.axes!r})"
    if s.dataoffset is None or not page.is_final:
        raise ValueError(f"{what}, and the page is not stored as plain contiguous samples: the planes after "
                         "the first cannot be found by offset")
    offset, size, end = int(s.dataoffset), tif.filehandle.size, int(s.dataoffset) + planes * page.nbytes
    if end > size:
        raise ValueError(f"{what}; at {page.nbytes} bytes a plane from offset {offset} they end at byte {end}, "
                         f"but the file has {size} bytes")
    return offset


def _shift(axis: str, sel: int | slice, first: int, n: int) -> int | slice:
    """A selection on the array's n planes or rows as the same selection on the file's, whose index `first` is
    the array's index 0."""
    if not isinstance(sel, slice):
        k = int(sel) + n if int(sel) < 0 else int(sel)
        if not 0 <= k < n:
            raise IndexError(f"{axis} {sel} is out of range for {n} {RUNS[axis]}")
        return k + first
    r = range(n)[sel]
    if len(r) == 0:
        return slice(0, 0)
    stop = r.stop + first
    return slice(r.start + first, stop if stop >= 0 else None, r.step)   # a reversed slice down to file index 0


def _voxel_size(tif: tifffile.TiffFile, axes: str) -> dict[str, float] | None:
    """From OME PhysicalSize* or ImageJ unit/spacing + X/YResolution; None when the file doesn't say."""
    out = {}
    if tif.is_ome:
        img = tifffile.xml2dict(tif.ome_metadata)["OME"]["Image"]
        px = (img[0] if isinstance(img, list) else img)["Pixels"]
        for a in "zyx":
            v, unit = px.get(f"PhysicalSize{a.upper()}"), px.get(f"PhysicalSize{a.upper()}Unit", "µm")
            if a in axes and v and unit in NM:
                out[a] = float(v) * NM[unit]
    elif tif.is_imagej and tif.imagej_metadata.get("unit") in NM:
        scale, tags = NM[tif.imagej_metadata["unit"]], tif.pages[0].tags
        for a, tag in (("y", "YResolution"), ("x", "XResolution")):
            num, den = tags[tag].value if tag in tags else (0, 0)
            if num:
                out[a] = den / num * scale
        if "z" in axes and tif.imagej_metadata.get("spacing"):
            out["z"] = float(tif.imagej_metadata["spacing"]) * scale
    return out or None


def _int(params: dict, name: str) -> int | None:
    v = params.get(name)
    if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
        raise ValueError(f"params.{name} must be an integer, got {v!r}")
    return v


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"tiff-zip-member-4 reads one file, got {len(src.urls)} urls")
    channel = _int(src.params, "channel")
    runs = {a: (_int(src.params, f"{a}_first"), _int(src.params, f"{a}_last")) for a in RUNS}
    try:
        tif, access = tifffile.TiffFile(io.open(src.urls[0])), "plane"
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        tif, access = tifffile.TiffFile(io.fetch(src.urls[0])), "file"
    return _Tiff(tif, access, src.params.get("axes"), channel, runs)
