"""One TIFF (multi-page or single) from a zip member or a plain URL, read with tifffile, with its channel axis
collapsed to one plane: `channel: <k>` returns channel k, `channels: onehot` returns class ids 1..C where every
pixel is set in exactly one channel.

tiff-zip-member exposes a multi-channel file as `c…yx`; the harness reads c=0 of a channelled label paired with
a channel-less raw, so the other channels of an ImageJ hyperstack holding one binary mask per class never reach
a crop. This snippet drops `c` at open, so each class can be bound as its own label (or, when the classes do not
overlap, as one class map)."""
import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

AXIS = {"Z": "z", "Q": "z", "I": "z", "C": "c", "S": "c", "Y": "y", "X": "x"}   # tifffile letter -> czyx
NM = {"nm": 1.0, "nanometer": 1.0, "um": 1e3, "µm": 1e3, "μm": 1e3, "\\u00B5m": 1e3,   # micro sign, Greek mu,
      "micron": 1e3, "micrometer": 1e3, "mm": 1e6}                                          # ImageJ's escaped form
MODES = ("onehot",)


class _Tiff:
    def __init__(self, tif: tifffile.TiffFile, access: str, axes: str | None, channel: int | None,
                 channels: str | None):
        self.tif, self.access = tif, access
        s = tif.series[0]
        page = tuple(s.keyframe.shape)
        if tuple(s.shape[len(s.shape) - len(page):]) != page:
            raise ValueError(f"series shape {s.shape} does not end in its page shape {page}")
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
        if "c" not in axes:
            raise ValueError(f"tifffile axes {s.axes!r} {tuple(s.shape)} map to {axes!r}, which has no channel "
                             "axis; this snippet collapses c, see tiff-zip-member for a file without one")
        self._full_axes, self._c = axes, axes.index("c")
        nc = full_shape[self._c]
        if channel is not None and not 0 <= channel < nc:
            raise ValueError(f"params.channel {channel} out of range for {nc} channels")
        self._channel, self._onehot = channel, channels == "onehot"
        self.axes = axes.replace("c", "")      # check_array (run by the harness) checks letters and length
        self.shape = full_shape[:self._c] + full_shape[self._c + 1:]
        self.dtype = np.dtype("uint8") if self._onehot else np.dtype(s.dtype)
        self.voxel_size_nm = _voxel_size(tif, self.axes)

    def __del__(self):          # the harness never closes arrays; a staged file's handle goes with the array
        self.tif.close()

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        full: dict[str, int | slice] = dict(region)
        full["c"] = slice(None) if self._onehot else self._channel
        idx: list[int | slice] = [0] * self._ndim                  # dropped size-1 axes are index 0
        for i, a in zip(self._keep, self._full_axes):
            idx[i] = full.get(a, slice(None))
        n = len(self._outer)
        keys = np.arange(int(np.prod(self._outer))).reshape(self._outer)[tuple(idx[:n])]
        pages = [self.tif.asarray(key=int(k), series=0)[tuple(idx[n:])] for k in np.ravel(keys)]
        out = np.stack(pages).reshape(np.shape(keys) + pages[0].shape)
        if not self._onehot:
            return out
        # c is kept as a dim; integer-indexed axes before it are gone, so its position is the number of
        # sliced axes before it
        pos = sum(isinstance(full.get(a, slice(None)), slice) for a in self._full_axes[:self._c])
        return _onehot(np.moveaxis(out, pos, -1))


def _onehot(a: np.ndarray) -> np.ndarray:
    """(..., C) -> (...) uint8 class ids 1..C in channel order, 0 where no channel is set."""
    hot = a != 0
    n = hot.sum(axis=-1)
    if (n > 1).any():
        raise ValueError(f"{int((n > 1).sum())} pixels are set in more than one channel, so the channels are not "
                         "one-hot; bind each channel as its own label with params.channel instead")
    return np.where(n == 1, np.argmax(hot, axis=-1) + 1, 0).astype(np.uint8)


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


def open_array(src: Source, io: IO) -> Array:
    if len(src.urls) != 1:
        raise ValueError(f"tiff-zip-member-channel reads one file, got {len(src.urls)} urls")
    channel, channels = src.params.get("channel"), src.params.get("channels")
    if channels is not None and channels not in MODES:
        raise ValueError(f"params.channels must be one of {MODES}, got {channels!r}")
    if channel is not None and (isinstance(channel, bool) or not isinstance(channel, int)):
        raise ValueError(f"params.channel must be an integer channel index, got {channel!r}")
    if (channel is None) == (channels is None):
        raise ValueError("set exactly one of params.channel (a 0-based index) or params.channels (onehot)")
    try:
        tif, access = tifffile.TiffFile(io.open(src.urls[0])), "plane"
    except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
        tif, access = tifffile.TiffFile(io.fetch(src.urls[0])), "file"
    return _Tiff(tif, access, src.params.get("axes"), channel, channels)
