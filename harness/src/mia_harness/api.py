"""The snippet contract (SPEC.md section 2)."""
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np

SPEC_VERSION = 1
AXES = "czyx"


@dataclass(frozen=True)
class Source:
    urls: tuple[str, ...]
    params: dict


class Array(Protocol):
    axes: str
    shape: tuple[int, ...]
    dtype: np.dtype
    voxel_size_nm: dict[str, float] | None
    access: Literal["chunk", "plane", "file"]

    def read(self, region: dict[str, int | slice]) -> np.ndarray: ...


def check_array(a: Array) -> None:
    """axes ⊆ czyx, unique, contain y and x, len(axes) == len(shape), access valid. Raises ValueError."""
    if not (set(a.axes) <= set(AXES) and len(set(a.axes)) == len(a.axes) and {"y", "x"} <= set(a.axes)):
        raise ValueError(f"bad axes {a.axes!r}")
    if len(a.axes) != len(a.shape):
        raise ValueError(f"axes {a.axes!r} do not match shape {a.shape}")
    if a.access not in ("chunk", "plane", "file"):
        raise ValueError(f"bad access {a.access!r}")
