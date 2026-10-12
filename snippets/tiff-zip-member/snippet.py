"""One TIFF (multi-page or single) from a zip member or a plain URL, read with tifffile."""
import numpy as np
import tifffile

from mia_harness.api import NeedsFetch

SPEC_VERSION = 1

AXIS = {"Z": "z", "Q": "z", "I": "z", "C": "c", "S": "c", "Y": "y", "X": "x"}   # tifffile letter -> czyx
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "\u00b5m": 1e3, "\u03bcm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form


class _Tiff:
    def __init__(self, tif: tifffile.TiffFile, access: str, axes: str | None):
        self.tif, self.access = tif, access
        s = tif.series[0]
        page = tuple(s.keyframe.shape)
        if tuple(s.shape[len(s.shape) - len(page):]) != page:
            raise ValueError(f"series shape {s.shape} does not end in its page shape {page}")
        self.dtype = np.dtype(s.dtype)
        self._outer = tuple(s.shape[:len(s.shape) - len(page)])   # dims that index pages
        self._ndim = len(s.shape)
        self._keep = [i for i, (a, n) in enumerate(zip(s.axes, s.shape)) if n > 1 or a in "YX"]
        self.shape = tuple(s.shape[i] for i in self._keep)
        if axes is None:
            axes = "".join(AXIS.get(s.axes[i], "?") for i in self._keep)
            if "?" in axes or len(set(axes)) != len(axes):
                raise ValueError(f"tifffile axes {s.axes!r} {tuple(s.shape)} do not map to czyx; "
                                 "set params.axes (with a reason in the binding)")
        self.axes = axes      # check_array (run by the harness) checks letters and length
        self.voxel_size_nm = _voxel_size(tif, axes)

    def __del__(self):          # the harness never closes arrays; a staged file's handle goes with the array
        self.tif.close()

    def read(self, region: dict) -> np.ndarray:
        idx = [0] * self._ndim                  # dropped size-1 axes are index 0
        for i, a in zip(self._keep, self.axes):
            idx[i] = region.get(a, slice(None))
        n = len(self._outer)
        keys = np.arange(int(np.prod(self._outer))).reshape(self._outer)[tuple(idx[:n])]
        pages = [self.tif.asarray(key=int(k), series=0)[tuple(idx[n:])] for k in np.ravel(keys)]
        return np.stack(pages).reshape(np.shape(keys) + pages[0].shape)


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


def open_array(src, io):
    if len(src.urls) != 1:
        raise ValueError(f"tiff-zip-member reads one file, got {len(src.urls)} urls")
    try:
        tif, access = tifffile.TiffFile(io.open(src.urls[0])), "plane"
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        tif, access = tifffile.TiffFile(io.fetch(src.urls[0])), "file"
    return _Tiff(tif, access, src.params.get("axes"))
