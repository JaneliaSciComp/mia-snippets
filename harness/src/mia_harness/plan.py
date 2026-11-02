"""Sparse staging plan: a plan rule plus a volume's real shape become a finite, deterministic list of samples."""
import dataclasses
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

from .sample import OVERLAP_MAX, overlap_frac


@dataclass(frozen=True)
class PlanRule:
    """What to sample: patch size, pyramid levels, sample counts per split, the plane split and the channel recipe."""
    seed: int
    patch: int
    levels: tuple[int, ...]
    samples: dict[str, int]
    split: dict
    channel_recipe: dict | None

    def digest(self) -> str:
        """sha256 hex of the rule's canonical JSON; equal rules give equal digests."""
        canonical = json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class PlanInput:
    """The contents of plan-rule.json: the rule and the artifact it stages."""
    rule: PlanRule
    artifact_id: str
    levels_key: str


def load_plan(path: Path) -> PlanInput:
    """Read plan-rule.json: {"artifact_id", "levels_key", "rule": {seed, patch, levels, samples, split, ...}}."""
    document = json.loads(Path(path).read_text())
    rule = dict(document["rule"])
    return PlanInput(rule=PlanRule(**{**rule, "levels": tuple(rule["levels"])}),
                     artifact_id=document["artifact_id"], levels_key=document["levels_key"])


def split_counts(n: int, fractions: dict[str, float]) -> dict[str, int]:
    """Largest-remainder apportionment of `n` keys; ties go to the earlier split in the spec's order."""
    exact = {name: n * fraction for name, fraction in fractions.items()}
    counts = {name: int(value) for name, value in exact.items()}
    remainders = sorted(fractions, key=lambda name: -(exact[name] - counts[name]))
    for name in remainders[: n - sum(counts.values())]:
        counts[name] += 1
    return counts


def split_planes(volume_id: str, planes: int, split: dict) -> dict[str, list[int]]:
    """Planes of one volume per split; byte for byte mia_buffers.register._split_planes (both repos pin its output)."""
    fractions = split["fractions"]
    if split["policy"] == "holdout":
        return {name: list(range(planes)) if name == "test" else [] for name in fractions}
    rng = random.Random(f"{split['seed']}:{volume_id}")
    order = list(range(planes))
    rng.shuffle(order)
    out: dict[str, list[int]] = {name: [] for name in fractions}
    if planes < len(fractions):
        for plane in order:
            out[rng.choices(list(fractions), weights=list(fractions.values()))[0]].append(plane)
        return {name: sorted(chosen) for name, chosen in out.items()}
    for name, count in split_counts(planes, fractions).items():
        out[name], order = sorted(order[:count]), order[count:]
    return out


def capacity(extent_y: int, extent_x: int, planes: int, patch: int, levels: tuple[int, ...]) -> int:
    """How many non-overlapping coarsest-level footprints fit in the volume."""
    footprint = patch * max(levels)
    return (extent_y // footprint) * (extent_x // footprint) * planes


def patch_start(centre_nm: float, scale: float, shift: float, patch: int, n: int) -> int:
    """First index of a patch centred on centre_nm on a level grid of n voxels (scale and shift in nm), clamped."""
    if n < patch:
        raise ValueError(f"patch {patch} is larger than the level's extent {n}")
    return int(min(max(round((centre_nm - shift) / scale - patch / 2), 0), n - patch))


def start_index(centre_nm: float, voxel_nm: float, factor: int, patch: int, extent_raw: int) -> int:
    """First index of a patch centred on centre_nm at a level `factor` times coarser than raw."""
    return patch_start(centre_nm, factor * voxel_nm, (factor - 1) / 2 * voxel_nm, patch, extent_raw // factor)


def derive_samples(rule: PlanRule, record: str, volume: int, axes: str, shape: tuple[int, ...],
                   voxel_size_nm: dict[str, float]) -> dict[str, list[dict]]:
    """Sample descriptors per split; one Random per split, so a larger count extends a smaller count's list.
    Val and test reject a sample whose coarsest footprint overlaps an accepted one by more than OVERLAP_MAX;
    every split drops exact duplicates. Each split spends at most 8 draws per wanted sample."""
    planes = shape[axes.index("z")] if "z" in axes else 1
    extents = [shape[axes.index(axis)] for axis in ("y", "x")]
    coarsest = max(rule.levels)
    half = rule.patch * coarsest // 2
    count_cap = capacity(*extents, planes, rule.patch, rule.levels)
    planes_by_split = split_planes(f"{record}/v{volume}", planes, rule.split)
    out: dict[str, list[dict]] = {}
    for split, planes_of_split in planes_by_split.items():
        count = min(rule.samples.get(split, 0), count_cap) if planes_of_split else 0
        rng = random.Random(f"{rule.seed}:{record}:v{volume}:{split}")
        accepted: list[dict] = []
        footprints: list[dict] = []
        for _ in range(8 * count):
            if len(accepted) == count:
                break
            z = rng.choice(planes_of_split)
            plane = {"z": z} if "z" in axes else {}
            centre_nm = [rng.randint(half, extent - half) * voxel_size_nm[axis] for axis, extent in zip("yx", extents)]
            starts = {str(factor): [start_index(centre, voxel_size_nm[axis], factor, rule.patch, extent)
                                    for centre, axis, extent in zip(centre_nm, "yx", extents)]
                      for factor in rule.levels}
            footprint = {"raw": 0, "plane": plane, "y": starts[str(coarsest)][0], "x": starts[str(coarsest)][1]}
            if footprint in footprints:
                continue
            overlaps = (overlap_frac(footprint, other, rule.patch) > OVERLAP_MAX for other in footprints)
            if split != "train" and any(overlaps):
                continue
            footprints.append(footprint)
            accepted.append({"plane": plane, "centre_nm": centre_nm, "levels": list(rule.levels), "patch": rule.patch,
                             "starts": starts})
        out[split] = accepted
    return out
