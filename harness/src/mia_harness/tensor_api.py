"""The tensorizer contract: a Stack of level patches in, a Sample of arrays out, checked against describe()."""
from dataclasses import dataclass, field

import numpy as np

SPEC_VERSION = 1
Shape = tuple[str | int, ...]          # ints or the symbols "L" (levels), "C" (channels), "P" (patch edge)


@dataclass(frozen=True)
class LevelPatch:
    factor: int
    img: np.ndarray                        # (C, P, P) in the source dtype
    labels: dict[str, np.ndarray]          # label key -> (P, P)
    scale_nm: tuple[float, float]          # (y, x) at this level
    translation_nm: tuple[float, float]    # (y, x) at this level
    start_px: tuple[int, int]              # (y, x) of the patch's first pixel at this level


@dataclass(frozen=True)
class Stack:
    levels: list[LevelPatch]
    patch: int
    plane: dict
    centre_nm: tuple[float, float]
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TensorSpec:
    """Output arrays by key: (shape with symbols, dtype name). A key ending in "/*" matches zero or more keys."""

    arrays: dict[str, tuple[Shape, str]]


Sample = dict[str, np.ndarray | dict]      # arrays plus one "meta" dict


def bbox_of(level: LevelPatch, patch: int) -> np.ndarray:
    """[[y0, x0], [y1, x1]] in nm: the first pixel's position and the last pixel's (MuViT's convention)."""
    start = np.asarray(level.start_px, dtype=np.float64)
    scale, shift = np.asarray(level.scale_nm), np.asarray(level.translation_nm)
    return np.stack([start * scale + shift, (start + patch - 1) * scale + shift]).astype(np.float32)


def _resolve(shape: Shape, symbols: dict[str, int]) -> tuple[int, ...]:
    return tuple(symbols[s] if isinstance(s, str) else int(s) for s in shape)


def check_sample(sample: Sample, spec: TensorSpec, *, L: int, C: int, P: int) -> None:
    """Every described key is present with the declared shape and dtype; nothing undescribed is present."""
    symbols = {"L": L, "C": C, "P": P}
    arrays = {k: v for k, v in sample.items() if k != "meta"}
    seen = set()
    for key, (shape, dtype) in spec.arrays.items():
        want = _resolve(shape, symbols)
        matches = [k for k in arrays if k.startswith(key[:-1])] if key.endswith("/*") else [key]
        if not key.endswith("/*") and key not in arrays:
            raise ValueError(f"{key}: missing from the sample")
        for k in matches:
            got = arrays[k]
            if not isinstance(got, np.ndarray) or tuple(got.shape) != want or np.dtype(got.dtype) != np.dtype(dtype):
                raise ValueError(f"{k}: expected shape {want} dtype {dtype}, got "
                                 f"{getattr(got, 'shape', None)} {getattr(got, 'dtype', type(got).__name__)}")
            seen.add(k)
    for k in arrays:
        if k not in seen:
            raise ValueError(f"{k}: not described by describe()")
    if not isinstance(sample.get("meta", {}), dict):
        raise ValueError("meta: must be a dict")


# Channel recipes (buffers spec v0.5 §2): how a tensorizer turns the stored channels into the crop's. The plan row
# names one; the harness only needs each op's output channel count for the contract check.
RECIPE_CHANNELS = {"channel": 1, "mean": 1, "luminance": 1, "replicate": 3, "rgb": 3, "additive": 3}


def recipe_channels(recipe: dict | None, stored: int) -> int:
    """Channels a sample has after `recipe`; no recipe keeps the stored count."""
    if not recipe:
        return stored
    op = recipe.get("op")
    if op not in RECIPE_CHANNELS:
        raise ValueError(f"unknown channel recipe op {op!r}")
    return RECIPE_CHANNELS[op]
