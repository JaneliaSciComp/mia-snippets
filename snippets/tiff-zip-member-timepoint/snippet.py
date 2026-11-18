"""One TIFF (multi-page or single) from a zip member or a plain URL, read with tifffile, at one index of its
time axis (`t`); optionally one channel of it (`channel`).

An Array has only c, z, y and x, so a time-lapse TIFF (an ImageJ hyperstack with frames, an OME-TIFF with SizeT)
cannot be presented whole, and tiff-zip-member-2 refuses its T axis unless a binding relabels it as z. This
snippet reads the file as tiff-zip-member-2 does and fixes the time axis at `params.t`: a TCYX file is presented
as cyx, a TZCYX file as zcyx. One time frame per bound array, as ome-zarr2-http-timepoint does for Zarr."""
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXIS = {"T": "t", "Z": "z", "Q": "z", "I": "z", "C": "c", "S": "c", "Y": "y", "X": "x"}   # tifffile letter -> tczyx
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form


class _Tiff:
    def __init__(self, tif: tifffile.TiffFile, access: str, axes: str | None, t: int | None, channel: int | None):
        self.tif, self.access = tif, access
        s = tif.series[0]
        page = tuple(s.keyframe.shape)
        if tuple(s.shape[len(s.shape) - len(page):]) != page:
            raise ValueError(f"series shape {s.shape} does not end in its page shape {page}")
        self.dtype = np.dtype(s.dtype).newbyteorder("=")
        self._outer = tuple(s.shape[:len(s.shape) - len(page)])   # dims that index pages
        self._ndim = len(s.shape)
        self._keep = [i for i, (a, n) in enumerate(zip(s.axes, s.shape)) if n > 1 or a in "YX"]
        full_shape = tuple(s.shape[i] for i in self._keep)
        file = f"tifffile axes {s.axes!r} {tuple(s.shape)}"
        if axes is None:
            axes = "".join(AXIS.get(s.axes[i], "?") for i in self._keep)
            if "?" in axes or len(set(axes)) != len(axes):
                raise ValueError(f"{file} do not map to tczyx; set params.axes (with a reason in the binding)")
        elif not isinstance(axes, str) or not set(axes) <= set("tczyx") or len(set(axes)) != len(axes):
            raise ValueError(f"params.axes {axes!r} must be distinct letters from 'tczyx'")
        if len(axes) != len(full_shape):
            raise ValueError(f"params.axes {axes!r} has {len(axes)} letters for {len(full_shape)} axes {full_shape}")
        self._full_axes = axes
        sizes = dict(zip(axes, full_shape))
        self._fixed: dict[str, int] = {}               # the axes the array does not present, and the index read on each
        if "t" in axes:
            if t is None:
                raise ValueError(f"{file} has {sizes['t']} timepoints and an Array has no time axis; set params.t "
                                 f"(0..{sizes['t'] - 1}, with a reason in the binding)")
            if not 0 <= t < sizes["t"]:
                raise ValueError(f"params.t {t} out of range for {sizes['t']} timepoints (0..{sizes['t'] - 1})")
            self._fixed["t"] = t
            del sizes["t"]
        elif t is not None:
            raise ValueError(f"params.t is set, but {file} map to {axes!r}, which has no time axis longer than 1; "
                             "leave t out")
        if channel is not None:
            if "c" not in axes:
                raise ValueError(f"params.channel is set, but {file} map to {axes!r}, which has no channel axis")
            if not 0 <= channel < sizes["c"]:
                raise ValueError(f"params.channel {channel} out of range for {sizes['c']} channels")
            self._fixed["c"] = channel
            del sizes["c"]
        self.axes = "".join(sizes)      # check_array (run by the harness) checks letters and length
        self.shape = tuple(sizes.values())
        self.voxel_size_nm = _voxel_size(tif, self.axes)

    def __del__(self):          # the harness never closes arrays; a staged file's handle goes with the array
        self.tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        full: dict[str, int | slice] = {**region, **self._fixed}
        idx: list[int | slice] = [0] * self._ndim                  # dropped size-1 axes are index 0
        for i, a in zip(self._keep, self._full_axes):
            idx[i] = full.get(a, slice(None))
        n = len(self._outer)
        keys = np.arange(int(np.prod(self._outer))).reshape(self._outer)[tuple(idx[:n])]
        pages = [self.tif.asarray(key=int(k), series=0)[tuple(idx[n:])] for k in np.ravel(keys)]
        out = np.stack(pages).reshape(np.shape(keys) + pages[0].shape)
        return out.astype(self.dtype, copy=False)      # a big-endian file's values in the native order reported


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
        raise ValueError(f"tiff-zip-member-timepoint reads one file, got {len(src.urls)} urls")
    t, channel = _int(src.params, "t"), _int(src.params, "channel")
    try:
        tif, access = tifffile.TiffFile(io.open(src.urls[0])), "plane"
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        tif, access = tifffile.TiffFile(io.fetch(src.urls[0])), "file"
    return _Tiff(tif, access, src.params.get("axes"), t, channel)
