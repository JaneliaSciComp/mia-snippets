"""Deterministic crop sampler (spec section 6.2). Pure: stdlib only."""
import random
from collections import Counter
from dataclasses import dataclass
from math import prod
from typing import Callable, Iterable, Iterator


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


def positions(v: Volume, size: int) -> int:
    planes, Y, X = _dims(v)
    return prod(planes.values()) * (Y - size + 1) * (X - size + 1)


def capacity(volumes: list[Volume], size: int) -> int:
    return sum(prod(p.values()) * (Y // size) * (X // size) for p, Y, X in map(_dims, volumes))


def effective_n(n: int, cap: int) -> int:
    return min(n, cap // 2)


def candidates(volumes: list[Volume], seed: str, *, size=256, per_plane=4, per_file=8) -> Iterator[CropSpec]:
    """Endless and deterministic per seed. Volume ~ positions, plane and (y, x) uniform; plane access yields
    per_plane draws on one plane, file access per_file draws in one volume, chunk access redraws every time."""
    rng = random.Random(seed)
    weights = [positions(v, size) for v in volumes]
    while True:
        v = rng.choices(volumes, weights)[0]
        planes, Y, X = _dims(v)
        def plane(): return {k: rng.randrange(n) for k, n in planes.items()}
        fixed = plane() if v.access == "plane" else None
        for _ in range({"plane": per_plane, "file": per_file}.get(v.access, 1)):
            yield CropSpec(v.index, fixed or plane(), rng.randrange(Y - size + 1), rng.randrange(X - size + 1), size)


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
