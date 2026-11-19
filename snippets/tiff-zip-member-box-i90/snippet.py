"""One TIFF (multi-page or single) from a zip member or a plain URL, read with tifffile; optionally one channel
of it (`channel`), and optionally only a box of it: a run of its z planes (`z_first`, `z_last`), of its rows
(`y_first`, `y_last`) and of its columns (`x_first`, `x_last`).

tiff-zip-member presents the whole file. A volume whose manual segmentation covers a few objects is unlabelled
everywhere else, so crops drawn over the whole of it pair raw full of structures with an empty label. With the
box the binding names the part of the file that forms the array, the same box for a raw and its label, which
keeps them voxel for voxel on one grid. Without the y/x params this snippet reads exactly what tiff-zip-member-2
reads, and without any of the box params and `channel` exactly what tiff-zip-member reads.

Of a page stored as it is (uncompressed, one sample per pixel, its rows one after another in the file) only the
rows a read covers are fetched, not the page: a box 302 rows high of a 1635-row float32 page is 2.3 MB of its
12.6 MB. Any other page is decoded whole, as before. The values are the same either way."""
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXIS = {"Z": "z", "Q": "z", "I": "z", "C": "c", "S": "c", "Y": "y", "X": "x"}   # tifffile letter -> czyx
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form


class _Tiff:
    def __init__(self, tif: tifffile.TiffFile, access: str, axes: str | None, channel: int | None,
                 box: dict[str, tuple[int | None, int | None]]):
        self.tif, self.access = tif, access
        s = tif.series[0]
        page = tuple(s.keyframe.shape)
        if tuple(s.shape[len(s.shape) - len(page):]) != page:
            raise ValueError(f"series shape {s.shape} does not end in its page shape {page}")
        self.dtype = np.dtype(s.dtype)
        self._outer = tuple(s.shape[:len(s.shape) - len(page)])   # dims that index pages
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
        self._first = {a: 0 for a in "zyx"}            # file index of the array's index 0, per boxed axis
        for a, (first, last) in box.items():
            if first is None and last is None:
                continue
            if a not in sizes:
                raise ValueError(f"params.{a}_first/{a}_last are set, but tifffile axes {s.axes!r} {tuple(s.shape)} "
                                 f"map to {axes!r}, which has no {a} axis")
            first, last = first or 0, sizes[a] - 1 if last is None else last
            if not 0 <= first <= last < sizes[a]:
                raise ValueError(f"params.{a}_first {first} .. {a}_last {last} is not a run of the file's "
                                 f"{sizes[a]} {a} indices (0-based, both ends included)")
            self._first[a], sizes[a] = first, last - first + 1
        self.axes = "".join(sizes)      # check_array (run by the harness) checks letters and length
        self.shape = tuple(sizes.values())
        self.voxel_size_nm = _voxel_size(tif, self.axes)

    def __del__(self):          # the harness never closes arrays; a staged file's handle goes with the array
        self.tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        full: dict[str, int | slice] = dict(region)
        for a, n in zip(self.axes, self.shape):
            if a in self._first:
                full[a] = _shift(full.get(a, slice(None)), self._first[a], n)
        if self._channel is not None:
            full["c"] = self._channel
        idx: list[int | slice] = [0] * self._ndim                  # dropped size-1 axes are index 0
        for i, a in zip(self._keep, self._full_axes):
            idx[i] = full.get(a, slice(None))
        n = len(self._outer)
        keys = np.arange(int(np.prod(self._outer))).reshape(self._outer)[tuple(idx[:n])]
        pages = [self._page(int(k), tuple(idx[n:])) for k in np.ravel(keys)]
        return np.stack(pages).reshape(np.shape(keys) + pages[0].shape)

    def _page(self, k: int, sel: tuple[int | slice, ...]) -> np.ndarray:
        """`sel` of page k of the series: from the rows `sel` covers alone when the page's rows can be read
        straight from the file (`_rows_at`), else from the whole page as tifffile decodes it."""
        page = self.tif.series[0].pages[k]
        at = None if page is None or len(sel) != 2 else _rows_at(page)
        if at is None:
            return self.tif.asarray(key=k, series=0)[sel]
        height, width = page.shape
        ys, xs = sel
        rows = np.arange(height)[ys]            # the file rows asked for, in the order asked
        if rows.size == 0:
            return np.empty((0, width), self.dtype)[:, xs]
        lo, hi = int(rows.min()), int(rows.max())
        stored = np.dtype(self.dtype).newbyteorder(self.tif.byteorder)
        fh = self.tif.filehandle
        fh.seek(at + lo * width * stored.itemsize)
        data = fh.read((hi - lo + 1) * width * stored.itemsize)
        if len(data) != (hi - lo + 1) * width * stored.itemsize:
            raise OSError(f"page {k}: rows {lo}..{hi} read back as {len(data)} bytes")
        block = np.frombuffer(data, stored).reshape(hi - lo + 1, width).astype(self.dtype, copy=False)
        return block[rows - lo][..., xs]


def _rows_at(page: tifffile.TiffPage | tifffile.TiffFrame) -> int | None:
    """The file offset of row 0 of a page whose rows lie in the file as they are, one after another: uncompressed
    whole-byte samples, one per pixel, no predictor, strips (or full-width tiles) in order with no gap. None for
    any other page."""
    key = page.keyframe
    if not key.is_final or key.dtype is None or len(key.shape) != 2:
        return None
    offsets, counts = page.dataoffsets, page.databytecounts
    if not offsets or len(offsets) != len(counts) or sum(counts) != key.shape[0] * key.shape[1] * key.dtype.itemsize:
        return None
    if any(o + c != nxt for o, c, nxt in zip(offsets, counts, offsets[1:])):
        return None
    return int(offsets[0])


def _shift(sel: int | slice, z0: int, n: int) -> int | slice:
    """A selection on the array's n indices of one axis as the same selection on the file's, whose index z0 is
    the array's index 0."""
    if not isinstance(sel, slice):
        k = int(sel) + n if int(sel) < 0 else int(sel)
        if not 0 <= k < n:
            raise IndexError(f"index {sel} is out of range for an axis of {n}")
        return k + z0
    r = range(n)[sel]
    if len(r) == 0:
        return slice(0, 0)
    stop = r.stop + z0
    return slice(r.start + z0, stop if stop >= 0 else None, r.step)   # a reversed slice down to file index 0


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
        raise ValueError(f"tiff-zip-member-box-i90 reads one file, got {len(src.urls)} urls")
    channel = _int(src.params, "channel")
    box = {a: (_int(src.params, f"{a}_first"), _int(src.params, f"{a}_last")) for a in "zyx"}
    try:
        tif, access = tifffile.TiffFile(io.open(src.urls[0])), "plane"
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        tif, access = tifffile.TiffFile(io.fetch(src.urls[0])), "file"
    return _Tiff(tif, access, src.params.get("axes"), channel, box)
