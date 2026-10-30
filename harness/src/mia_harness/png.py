"""Raw stretch, label encodings and PNG IO (spec section 6.3)."""
from pathlib import Path

import numpy as np
import png

from .api import CROP

NONFINITE_MAX = 0.05     # more non-finite pixels than this and the patch is rejected
CONST_MAX = 0.95         # share of finite pixels one value may hold
STD_MIN = 1e-6           # std must exceed this * (range + 1)


def stretch_raw(a: np.ndarray, p_lo: float = 0.1, p_hi: float = 99.9) -> tuple[np.ndarray, dict]:
    a = np.asarray(a)
    f = a.astype(np.float64)
    fin = np.isfinite(f)
    lo, hi = (float(v) for v in np.percentile(f[fin], [p_lo, p_hi]))
    if hi <= lo:
        raise ValueError("constant patch: hi <= lo")
    out = np.where(fin, np.clip((f - lo) / (hi - lo), 0, 1) * 65535, 0)
    out[np.isposinf(f)] = 65535
    nf = {"nan": int(np.isnan(f).sum()), "posinf": int(np.isposinf(f).sum()), "neginf": int(np.isneginf(f).sum())}
    return np.round(out).astype(np.uint16), {"kind": "stretch", "lo": lo, "hi": hi, "percentiles": [p_lo, p_hi],
                                             "nonfinite": nf, "source_dtype": str(a.dtype)}


def encode_label(a: np.ndarray, seg_type: str) -> tuple[np.ndarray, int, dict]:
    if seg_type == "binary":
        return (a != 0).astype(np.uint8), 8, {"kind": "binary"}
    lo, hi = int(a.min()), int(a.max())
    if lo >= 0 and hi <= 65535:
        return a.astype(np.uint16), 16, {"kind": "identity"}
    if seg_type == "semantic":
        raise ValueError(f"semantic label range {lo}..{hi} outside 0..65535")
    nz = a != 0
    ids, inv = np.unique(a[nz], return_inverse=True)
    if len(ids) > 65535:
        raise ValueError(f"{len(ids)} instances exceed 65535")
    out = np.zeros(a.shape, np.uint16)
    out[nz] = inv + 1
    return out, 16, {"kind": "renumbered", "instances": len(ids)}


def write_png(path: Path, a: np.ndarray, bitdepth: int) -> None:
    rgb = a.ndim == 3 and a.shape[2] == 3
    # ponytail: rows via tolist(), fine for 256x256 crops; use big-endian bytes if sizes grow
    with open(path, "wb") as f:
        png.Writer(a.shape[1], a.shape[0], greyscale=not rgb, bitdepth=bitdepth).write(f, a.reshape(a.shape[0], -1).tolist())


def read_png(path: Path, edge: int = CROP) -> tuple[np.ndarray, int]:
    """An edge x edge PNG; any other size is refused from the header, before a row is decoded."""
    with open(path, "rb") as f:
        w, h, rows, info = png.Reader(file=f).read()
        if (w, h) != (edge, edge):
            raise ValueError(f"{Path(path).name} is {w}x{h}, not {edge}x{edge}")
        a = np.array(list(rows), dtype=np.uint16 if info["bitdepth"] == 16 else np.uint8)
    return (a.reshape(h, w, -1) if info["planes"] > 1 else a), info["bitdepth"]


def stats(a: np.ndarray) -> dict:
    f = np.asarray(a, np.float64)
    p = np.percentile(f[np.isfinite(f)], [0, 1, 50, 99, 100])
    return dict(zip(["min", "p1", "p50", "p99", "max"], map(float, p)))


def content_ok(a: np.ndarray) -> bool:
    f = np.asarray(a, np.float64)
    fin = f[np.isfinite(f)]
    if fin.size < (1 - NONFINITE_MAX) * f.size or fin.size == 0:
        return False
    if fin.std() <= STD_MIN * (fin.max() - fin.min() + 1):
        return False
    return np.unique(fin, return_counts=True)[1].max() / fin.size <= CONST_MAX
