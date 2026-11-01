"""Deterministic crop sampler (spec section 6.2). Pure: stdlib only."""
import random
from collections import Counter
from dataclasses import dataclass
from math import prod
from typing import Callable, Iterable, Iterator


OVERLAP_MAX = 0.25       # a crop may share at most this share of its area with another on the same plane


@dataclass(frozen=True)
class Volume:
    index: int
    axes: str
    shape: tuple[int, ...]
    access: str


@dataclass(frozen=True)
class CropSpec:
    raw: int
    plane: dict
    y: int
    x: int
    size: int = 256


def _dims(v: Volume) -> tuple[dict, int, int]:
    d = dict(zip(v.axes, v.shape))
    return {k: n for k, n in d.items() if k in "cz"}, d["y"], d["x"]


def _edge(size: int | dict, v: Volume) -> int:
    """The crop edge of a volume: one size for all, or a dict by volume index (a raw smaller than the size is
    cropped at its own extent)."""
    return size[v.index] if isinstance(size, dict) else size


def positions(v: Volume, size: int | dict) -> int:
    planes, Y, X = _dims(v)
    s = _edge(size, v)
    return prod(planes.values()) * (Y - s + 1) * (X - s + 1)


def capacity(volumes: list[Volume], size: int | dict) -> int:
    return sum(prod(p.values()) * (Y // s) * (X // s) for v in volumes for p, Y, X, s in [(*_dims(v), _edge(size, v))])


def effective_n(n: int, cap: int) -> int:
    """At most half the capacity, so the overlap rule leaves room; a capacity of 1 or 2 (a whole-image crop) still
    yields one."""
    return min(n, max(cap // 2, 1 if cap else 0))


def candidates(volumes: list[Volume], seed: str, *, size: int | dict = 256, per_plane=4, per_file=8) -> Iterator[CropSpec]:
    """Endless and deterministic per seed. Volume ~ positions, plane and (y, x) uniform; plane access yields
    per_plane draws on one plane, file access per_file draws in one volume, chunk access redraws every time."""
    rng = random.Random(seed)
    weights = [positions(v, size) for v in volumes]
    while True:
        v = rng.choices(volumes, weights)[0]
        planes, Y, X = _dims(v)
        s = _edge(size, v)
        def plane(): return {k: rng.randrange(n) for k, n in planes.items()}
        fixed = plane() if v.access == "plane" else None
        for _ in range({"plane": per_plane, "file": per_file}.get(v.access, 1)):
            yield CropSpec(v.index, fixed or plane(), rng.randrange(Y - s + 1), rng.randrange(X - s + 1), s)


def overlap_frac(a: dict, b: dict, size: int) -> float:
    """Shared area / size^2; 0 unless same raw and plane (z and channel). a, b: dicts with raw, plane, y, x."""
    if a["raw"] != b["raw"] or a["plane"] != b["plane"]:
        return 0.0
    return max(0, size - abs(a["y"] - b["y"])) * max(0, size - abs(a["x"] - b["x"])) / size**2


def collect(cands: Iterable[CropSpec], n: int, reject: Callable[[CropSpec, list], str | None]) -> tuple[list, Counter]:
    """Take candidates until n are accepted or 10 * n have been drawn (one budget for every rejection kind).
    reject(candidate, accepted) returns a kind such as "content" or "overlap", or None to accept."""
    acc, rej = [], Counter()
    for i, c in enumerate(cands):
        if len(acc) >= n or i >= 10 * n:
            break
        kind = reject(c, acc)
        if kind: rej[kind] += 1
        else: acc.append(c)
    return acc, rej
