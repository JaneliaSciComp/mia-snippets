"""Single-page TIFF tiles from zip members chosen by glob (or listed one by one), stacked along z, with the
TIFF's sample (RGB) axis collapsed to one plane.

Three collapses, chosen by params: `samples: grey` for a grey image stored as identical RGB samples (sample 0
is returned, after checking the samples agree), `samples: onehot` for a mask whose pixels are set in exactly
one sample (class ids 1..S in sample order, 0 where none is set), or `sample: <k>` for one sample plane.
Files are opened lazily: `open_array` reads the first file's header only; `read` fetches the planes it needs.
"""
from fnmatch import fnmatchcase
from typing import BinaryIO

import numpy as np
import tifffile

from mia_harness.api import IO, Array, NeedsFetch, Source

SPEC_VERSION = 1

GLOB_CHARS = "*?["
MODES = ("grey", "onehot")


class _Tiles:
    def __init__(self, io: IO, urls: list[str], samples: str | None, sample: int | None):
        self._io, self._urls, self._samples, self._sample = io, urls, samples, sample
        first = self._raw_page(0)
        self._page_shape = first.shape                  # (Y, X) or (Y, X, S); every file must match
        self._file_dtype = first.dtype
        ny, nx = first.shape[:2]
        self.shape = (len(urls), ny, nx) if len(urls) > 1 else (ny, nx)
        self.axes = "zyx" if len(urls) > 1 else "yx"
        self.dtype = np.dtype("uint8") if samples == "onehot" else np.dtype(first.dtype)
        self.access = "plane"           # one file per plane: a y/x region costs that file, whether it is
        self.voxel_size_nm = None       # read by range or staged whole (deflated member)
        self._collapse(first)           # fail at open, not at the first crop, when the params don't fit the data

    def read(self, region: dict[str, int | slice]) -> np.ndarray:
        y, x = region.get("y", slice(None)), region.get("x", slice(None))
        if "z" not in self.axes:
            return self._plane(0)[y, x]
        sel = region.get("z", slice(None))
        if isinstance(sel, slice):
            return np.stack([self._plane(k)[y, x] for k in range(self.shape[0])[sel]])
        return self._plane(range(self.shape[0])[sel])[y, x]     # IndexError when out of range, like numpy

    def _plane(self, k: int) -> np.ndarray:
        return self._collapse(self._raw_page(k))

    def _raw_page(self, k: int) -> np.ndarray:
        """The decoded page of file k, (Y, X) or (Y, X, S), checked against file 0."""
        url = self._urls[k]
        try:
            fh: BinaryIO = self._io.open(url)
            tif = tifffile.TiffFile(fh)
        except NeedsFetch:      # deflated member, or a server without Range: stage the whole file
            tif = tifffile.TiffFile(self._io.fetch(url))
        with tif:
            s = tif.series[0]
            if s.axes not in ("YX", "YXS") or len(tif.series) != 1:
                raise ValueError(f"{url}: tifffile axes {s.axes!r} {tuple(s.shape)}; this snippet reads "
                                 "single-page YX or YXS (RGB) tiles only, see tiff-zip-member for stacks")
            a = np.asarray(s.asarray())
        if k > 0 and (a.shape != self._page_shape or a.dtype != self._file_dtype):
            raise ValueError(f"{url}: {a.dtype} {a.shape} differs from the first file's "
                             f"{self._file_dtype} {self._page_shape}; all files of one array must agree")
        return a

    def _collapse(self, a: np.ndarray) -> np.ndarray:
        """(Y, X, S) -> (Y, X) by the chosen mode; a (Y, X) page passes through only when no collapse is asked."""
        if a.ndim == 2:
            if self._samples == "onehot" or self._sample is not None:
                raise ValueError("the file has no sample axis; drop params.sample / samples: onehot")
            return a
        if self._sample is not None:
            if not 0 <= self._sample < a.shape[2]:
                raise ValueError(f"params.sample {self._sample} out of range for {a.shape[2]} samples")
            return a[:, :, self._sample]
        if self._samples == "grey":
            if not np.array_equal(a, np.broadcast_to(a[:, :, :1], a.shape)):
                raise ValueError("samples differ, so this is not a grey image stored as RGB; "
                                 "set params.sample (one sample) or samples: onehot (class map)")
            return a[:, :, 0]
        if self._samples == "onehot":
            hot = a != 0
            n = hot.sum(axis=2)
            if (n > 1).any():
                raise ValueError(f"{int((n > 1).sum())} pixels are set in more than one sample; not one-hot")
            return np.where(n == 1, np.argmax(hot, axis=2) + 1, 0).astype(np.uint8)
        raise ValueError(f"the file has {a.shape[2]} samples per pixel; set params.samples "
                         f"({' or '.join(MODES)}) or params.sample (an index) to collapse them to one plane")


def _split(url: str) -> tuple[str, str | None]:
    """(file url, zip member or None): `::` after the path starts, as the harness writes zip members."""
    path = url.find("/", url.find("://") + 3)
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


def _expand(urls: tuple[str, ...], io: IO) -> list[str]:
    """Each `<zip>::<glob>` becomes its matching members, sorted by name; other urls pass through."""
    out: list[str] = []
    for u in urls:
        zip_url, member = _split(u)
        if member is not None and any(ch in member for ch in GLOB_CHARS):
            names = sorted(n for n in io.list(zip_url) if fnmatchcase(n, member))
            if not names:
                raise ValueError(f"no member of {zip_url} matches {member!r}")
            out += [f"{zip_url}::{n}" for n in names]
        else:
            out.append(u)
    if len(set(out)) != len(out):
        raise ValueError("the urls (after glob expansion) name the same file twice")
    return out


def open_array(src: Source, io: IO) -> Array:
    samples, sample = src.params.get("samples"), src.params.get("sample")
    if samples is not None and samples not in MODES:
        raise ValueError(f"params.samples must be one of {MODES}, got {samples!r}")
    if sample is not None and (isinstance(sample, bool) or not isinstance(sample, int)):
        raise ValueError(f"params.sample must be an integer sample index, got {sample!r}")
    if samples is not None and sample is not None:
        raise ValueError("params.samples and params.sample are exclusive; set one")
    urls = _expand(src.urls, io)
    return _Tiles(io, urls, samples, sample)
