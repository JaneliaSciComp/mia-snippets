"""Sparse staging plan: a plan rule plus a volume's real shape become a finite, deterministic list of samples."""
import dataclasses
import hashlib
import itertools
import json
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path



@dataclass(frozen=True)
class PlanRule:
    """What to sample: patch size, pyramid levels, sample counts per split, the plane split, the channel recipe
    and the filters a sample must pass (`{"name", **parameters}` each, in name order; see `filters.py`)."""
    seed: int
    patch: int
    levels: tuple[int, ...]
    samples: dict[str, int]
    split: dict
    channel_recipe: dict | None
    filters: tuple[dict, ...] = ()

    def document(self) -> dict:
        """The rule as the worker wrote it: `filters` is there only when the rule has some."""
        document = dataclasses.asdict(self)
        document["filters"] = list(self.filters)
        if not self.filters:
            del document["filters"]
        return document

    def digest(self) -> str:
        """sha256 hex of the rule's canonical JSON; equal rules give equal digests."""
        canonical = json.dumps(self.document(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class PlanInput:
    """The contents of plan-rule.json: the rule and the artifact block inputs only the worker knows."""
    rule: PlanRule
    artifact_id: str
    levels_key: str
    harness_commit: str = ""
    env_digest: str = ""       # sha256 of the harness pixi.lock


def load_plan(path: Path) -> PlanInput:
    """Read plan-rule.json: {"artifact_id", "levels_key", "harness_commit"?, "env_digest"?,
    "rule": {seed, patch, levels, samples, split, channel_recipe, filters?}}."""
    document = json.loads(Path(path).read_text())
    rule = dict(document["rule"])
    return PlanInput(rule=PlanRule(**{**rule, "levels": tuple(rule["levels"]),
                                      "filters": tuple(rule.get("filters", ()))}),
                     artifact_id=document["artifact_id"], levels_key=document["levels_key"],
                     harness_commit=document.get("harness_commit", ""), env_digest=document.get("env_digest", ""))


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


def patch_start(centre_nm: float, scale: float, shift: float, patch: int, n: int) -> int:
    """First index of a patch centred on centre_nm on a level grid of n voxels (scale and shift in nm), clamped."""
    if n < patch:
        raise ValueError(f"patch {patch} is larger than the level's extent {n}")
    return int(min(max(round((centre_nm - shift) / scale - patch / 2), 0), n - patch))


def start_index(centre_nm: float, voxel_nm: float, factor: int, patch: int, extent_raw: int) -> int:
    """First index of a patch centred on centre_nm at a level `factor` times coarser than raw."""
    return patch_start(centre_nm, factor * voxel_nm, (factor - 1) / 2 * voxel_nm, patch, extent_raw // factor)


def planes_per_split(rule: PlanRule, record: str, volume: int, axes: str, shape: tuple[int, ...]) -> dict[str, int]:
    """How many of the volume's planes each split holds; none when the coarsest footprint exceeds its y or x."""
    planes = shape[axes.index("z")] if "z" in axes else 1
    # the registry's volume id (record/<volume dir>), so both repos put every plane in the same split
    by_split = split_planes(f"{record}/v{volume}.zarr", planes, rule.split)
    footprint = rule.patch * max(rule.levels)
    fits = all(shape[axes.index(axis)] >= footprint for axis in "yx")
    return {split: len(chosen) if fits else 0 for split, chosen in by_split.items()}


def share_out(rule: PlanRule, record: str, planes: list[dict[str, int]]) -> list[dict[str, int]]:
    """Per volume, how many of the record's `samples[split]` crops it gets: each crop picks a volume in proportion
    to the planes it holds in the split (`planes`, one entry per volume). The picks of a split come in order from
    one Random, so a larger count only adds to each volume's share."""
    shares: list[dict[str, int]] = [{} for _ in planes]
    for split in rule.split["fractions"]:
        weights = [held.get(split, 0) for held in planes]
        picked: Counter[int] = Counter()
        if any(weights):
            rng = random.Random(f"{rule.seed}:{record}:{split}")
            picked.update(rng.choices(range(len(planes)), weights=weights, k=rule.samples.get(split, 0)))
        for index, share in enumerate(shares):
            share[split] = picked[index]
    return shares


# ponytail: a sparse volume costs thousands of draws, each a label read, and a volume with nothing passable costs
# this many per split before it stops; scan the label coarsely once and draw from where it is annotated.
GIVE_UP = 2000   # rejected draws in a row after which a split of a volume ends with what it has


def select_samples(rule: PlanRule, record: str, volume: int, axes: str, shape: tuple[int, ...],
                   voxel_size_nm: dict[str, float], counts: dict[str, int],
                   keep: Callable[[dict], str | None] | None = None) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Sample descriptors per split, placed at random: a plane of the split, then a position whose coarsest
    footprint lies inside the volume (its start a multiple of the coarsest factor, so every level's start lands on
    its own grid). Crops of a split may overlap, but a position drawn again is not listed twice; a split without a
    plane gets none. Finer levels sit inside their coarsest footprint.

    Without `keep` a split takes `counts[split]` draws, so a small volume gives fewer crops than draws. With
    `keep` (a sample descriptor → the name of the filter that rejects it, or None) it draws until `counts[split]`
    are kept or `GIVE_UP` draws in a row were rejected, a repeated position being one. Either way the draws come in
    order from one Random, so a larger count extends a smaller count's list.

    Also the selection, per split: {"wanted", "staged", "draws", "rejected": {"repeat" or a filter's name: count}}."""
    planes = shape[axes.index("z")] if "z" in axes else 1
    extents = [shape[axes.index(axis)] for axis in ("y", "x")]
    coarsest = max(rule.levels)
    footprint = rule.patch * coarsest
    planes_by_split = split_planes(f"{record}/v{volume}.zarr", planes, rule.split)
    out: dict[str, list[dict]] = {}
    selection: dict[str, dict] = {}
    for split, planes_of_split in planes_by_split.items():
        rng = random.Random(f"{rule.seed}:{record}:v{volume}:{split}")
        wanted = counts.get(split, 0) if planes_of_split else 0
        accepted: list[dict] = []
        drawn: set[tuple[int, ...]] = set()
        rejected: Counter[str] = Counter()
        draws = run = 0
        while len(accepted) < wanted and (draws < wanted if keep is None else run < GIVE_UP):
            draws += 1
            z = rng.choice(planes_of_split)
            first = [rng.randrange((extent - footprint) // coarsest + 1) * coarsest for extent in extents]
            reason = "repeat" if (z, *first) in drawn else None
            drawn.add((z, *first))
            if reason is None:
                centre_nm = [(start + footprint / 2) * voxel_size_nm[axis] for start, axis in zip(first, "yx")]
                starts = {str(factor): [start_index(centre, voxel_size_nm[axis], factor, rule.patch, extent)
                                        for centre, axis, extent in zip(centre_nm, "yx", extents)]
                          for factor in rule.levels}
                sample = {"plane": {"z": z} if "z" in axes else {}, "centre_nm": centre_nm,
                          "levels": list(rule.levels), "patch": rule.patch, "starts": starts}
                reason = keep(sample) if keep else None
            if reason:
                rejected[reason] += 1
                run += 1
                continue
            run = 0
            accepted.append(sample)
        out[split] = accepted
        selection[split] = {"wanted": wanted, "staged": len(accepted), "draws": draws, "rejected": dict(rejected)}
    return out, selection


def derive_samples(rule: PlanRule, record: str, volume: int, axes: str, shape: tuple[int, ...],
                   voxel_size_nm: dict[str, float], counts: dict[str, int]) -> dict[str, list[dict]]:
    """`select_samples` with no filter: `counts[split]` draws per split."""
    return select_samples(rule, record, volume, axes, shape, voxel_size_nm, counts)[0]


@dataclass(frozen=True)
class LevelPlan:
    """One level's box (origin and extent in raw index space), its stored shape and the chunks its samples touch.
    `shape` and the chunk tuples span the stored axes; `c` is 1 and 0 here, the writer expands it per channel."""
    factor: int
    origin: dict[str, int]
    extent: dict[str, int]
    shape: tuple[int, ...]
    planned_planes: tuple[int, ...]
    chunks: frozenset[tuple[int, ...]]


@dataclass(frozen=True)
class StagePlan:
    """The per-level plans in the order of the rule's levels; `patch` is the chunk edge on y and x."""
    levels: tuple[LevelPlan, ...]
    patch: int


def stage_plan(samples: dict[str, list[dict]], axes: str, shape: tuple[int, ...], levels: tuple[int, ...],
               patch: int) -> StagePlan:
    """Per level, the smallest box holding every sample (whole level blocks, origin on a sample start) and the set
    of chunks, on a patch-sized grid anchored at the box origin, that some sample touches."""
    drawn = [sample for split in samples.values() for sample in split]
    if not drawn:
        raise ValueError("no samples planned")
    planes = [sample["plane"]["z"] for sample in drawn] if "z" in axes else []
    out = []
    for factor in levels:
        starts = [sample["starts"][str(factor)] for sample in drawn]
        origin: dict[str, int] = {"z": min(planes)} if planes else {}
        extent: dict[str, int] = {"z": max(planes) - min(planes) + 1} if planes else {}
        for index, axis in enumerate("yx"):
            origin[axis] = min(start[index] * factor for start in starts) // factor * factor
            extent[axis] = max((start[index] + patch) * factor for start in starts) - origin[axis]
            if origin[axis] + extent[axis] > shape[axes.index(axis)]:
                raise ValueError(f"level {factor} box ends past the raw {axis} extent {shape[axes.index(axis)]}")
        level_shape = {"c": 1, "z": extent.get("z", 0), "y": extent["y"] // factor, "x": extent["x"] // factor}
        chunks = set()
        for sample, start in zip(drawn, starts):
            touched = {"c": [0], "z": [sample["plane"].get("z", 0) - origin.get("z", 0)]}
            for index, axis in enumerate("yx"):
                offset = start[index] - origin[axis] // factor
                touched[axis] = range(offset // patch, (offset + patch - 1) // patch + 1)
            chunks.update(itertools.product(*(touched[axis] for axis in axes)))
        out.append(LevelPlan(factor=factor, origin=origin, extent=extent,
                             shape=tuple(level_shape[axis] for axis in axes),
                             planned_planes=tuple(sorted(set(planes))), chunks=frozenset(chunks)))
    return StagePlan(levels=tuple(out), patch=patch)


def planned_bytes(plan: StagePlan, itemsize: int, channels: int) -> int:
    """Bytes the planned chunks hold once written: every chunk times channels, patch squared and the item size."""
    return sum(len(level.chunks) for level in plan.levels) * channels * plan.patch * plan.patch * itemsize
