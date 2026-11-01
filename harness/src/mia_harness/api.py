"""The snippet contract (SPEC.md section 2)."""
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

import numpy as np

SPEC_VERSION = 1
AXES = "czyx"
CROP = 256          # default crop edge in y and x; a raw smaller than it is cropped at its own extent (SPEC §6.2)


class BudgetExceeded(RuntimeError):
    """The job's byte cap would be passed."""


class HostNotAllowed(RuntimeError):
    """A URL or redirect hop leaves the hosts the job was granted."""

    def __init__(self, host: str, chain: list[str]):
        super().__init__(f"host {host!r} is not allowed (requests: {' -> '.join(chain)})")
        self.host, self.chain = host, chain


class NeedsFetch(Exception):
    """This file can't be read by range (a deflated zip member, a server ignoring Range): use `io.fetch`."""


@dataclass(frozen=True)
class Source:
    urls: tuple[str, ...]
    params: dict


class IO(Protocol):
    """The only way a snippet reads bytes. Counts network bytes against the job's cap, checks every redirect
    hop's host, records validators for provenance. Implemented by mia_harness.io.IO."""

    def open(self, url: str) -> BinaryIO: ...     # seekable, range-backed; raises NeedsFetch when ranges won't do
    def fetch(self, url: str) -> Path: ...        # the whole file, staged once
    def list(self, url: str) -> list[str]: ...    # zip members of a zip url


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
