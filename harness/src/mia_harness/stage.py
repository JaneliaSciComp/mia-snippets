"""`harness stage`: a binding's arrays and a plan rule → one sparse OME-Zarr 0.6 image per raw volume, labels
alongside, provenance under `attributes.mia` and stage.json from the same model (staging spec §4.1, §5, §6)."""
import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import zarr

from .api import Array
from .binding import ArrayBinding, Binding, LabelBinding, binding_hash, load_binding
from .fetch import expand
from .io import IO, BudgetExceeded, HostNotAllowed, _split
from .plan import LevelPlan, PlanInput, StagePlan, derive_samples, planned_bytes, stage_plan
from .provenance import (VOXEL_AXIS, Artifact, Geometry, LevelRecord, Plan, Provenance, SourceFile, Verification,
                         VolumeSummary, VoxelAxis, merge_geometry, stage_json, summary)
from .pyramid import (Pyramid, PyramidInfo, _canonical, _read_yx, block_mean, open_pyramid, stride, write_pyramid,
                      write_volume_groups)
from .run import open_arrays

GEOMETRY_SAMPLES = 8   # planned samples per volume re-read through the snippet, at every level
SAMPLE_CHUNKS = 8      # written level-0 chunks per channel that feed the intensity sample
SAMPLE_KEEP = 4096     # values kept per channel (sorted) in stage.json's intensity_sample


@dataclass
class _Group:
    """One array of a volume to write: the raw or one label, its binding, stored axes, plan and name."""
    kind: Literal["raw", "label"]
    array: Array
    binding: ArrayBinding
    axes: str
    plan: StagePlan
    path: str                  # under v<i>.zarr: "raw" or "labels/<label_key>"
    name: str                  # "v<i>" or "v<i>/<label_key>", for error lines


@dataclass
class _Volume:
    """One raw volume planned for writing: its voxel size, samples and groups, raw first."""
    index: int
    voxel: dict[str, VoxelAxis]
    samples: dict[str, list[dict]]
    groups: list[_Group]


def intensity_sample(p: Pyramid, level_plan: LevelPlan, patch: int, rng: np.random.Generator) -> list[list[float]]:
    """Per channel, up to SAMPLE_KEEP values from SAMPLE_CHUNKS random written level-0 chunks, sorted: what
    registration turns into normalisation bounds for `normalize.scope: volume|dataset` (buffers spec §3). Only
    chunks the plan wrote are read, so no fill value enters the sample. `p` is an open Pyramid."""
    size = dict(zip(p.axes, p.levels[0].shape))
    chunks = sorted(level_plan.chunks)
    out = []
    for channel in range(size.get("c", 1)):
        values = []
        for k in rng.choice(len(chunks), min(SAMPLE_CHUNKS, len(chunks)), replace=False):
            at = dict(zip(p.axes, chunks[k]))
            region: dict = {a: slice(at[a] * patch, min((at[a] + 1) * patch, size[a])) for a in "yx"}
            region |= ({"z": at["z"]} if "z" in at else {}) | ({"c": channel} if "c" in at else {})
            values.append(np.asarray(p.read(0, region)).ravel())
        pooled = np.concatenate(values)
        if pooled.size > SAMPLE_KEEP:
            pooled = rng.choice(pooled, SAMPLE_KEEP, replace=False)
        out.append(np.sort(pooled).tolist())
    return out


def _voxel_size(array: Array, voxel: dict[str, VoxelAxis]) -> tuple[dict[str, VoxelAxis], list[str]]:
    """Per spatial axis: what the file says (recorded as `source: file`), else the caller's value; and the axes
    that have neither, for the caller to refuse rather than guess."""
    found = {a: VoxelAxis(float(nm), "file") for a, nm in (array.voxel_size_nm or {}).items() if nm is not None}
    merged = {**voxel, **found}
    spatial = [a for a in _canonical(array.axes) if a != "c"]
    return {a: merged[a] for a in spatial if a in merged}, [a for a in spatial if a not in merged]


def _shape(array: Array) -> tuple[int, ...]:
    """The array's shape in stored (canonical) axis order."""
    size = dict(zip(array.axes, array.shape))
    return tuple(size[a] for a in _canonical(array.axes))


def _channels(array: Array) -> int:
    """The array's channel count (1 without a c axis)."""
    return dict(zip(array.axes, array.shape)).get("c", 1)


def _plan_volume(i: int, raw: Array, binding: Binding, labels: list[Array], rule_input: PlanInput,
                 voxel: dict[str, VoxelAxis], errors: list[str]) -> _Volume | None:
    """Raw `i`'s samples and its groups' plans, or None after an error line. Reads no voxel."""
    rule = rule_input.rule
    vox, missing = _voxel_size(raw, voxel)
    if missing:
        errors.append(f"raw {i}: voxel size unknown for axes {missing}")
        return None
    mine = [(lb, arr) for lb, arr in zip(binding.labels, labels) if lb.pairs_with == i]
    spatial = {a: n for a, n in zip(_canonical(raw.axes), _shape(raw)) if a != "c"}
    for lb, arr in mine:   # a label may lack the raw's c axis; every spatial extent must match
        if {a: n for a, n in zip(_canonical(arr.axes), _shape(arr)) if a != "c"} != spatial:
            errors.append(f"raw {i}: label {lb.label_key} shape {list(_shape(arr))} differs from raw "
                          f"{list(_shape(raw))}")
            return None
    axes = _canonical(raw.axes)
    samples = derive_samples(rule, binding.record, i, axes, _shape(raw), {a: v.nm for a, v in vox.items()})
    if not any(samples.values()):
        errors.append(f"raw {i}: no samples planned")
        return None

    def group(kind: Literal["raw", "label"], array: Array, b: ArrayBinding, path: str, name: str) -> _Group:
        axes = _canonical(array.axes)   # same boxes for every group; a label's chunk tuples lack the raw's c
        plan = stage_plan(samples, axes, _shape(array), rule.levels, rule.patch)
        return _Group(kind, array, b, axes, plan, path, name)

    groups = [group("raw", raw, binding.raw[i], "raw", f"v{i}")]
    groups += [group("label", arr, lb, f"labels/{lb.label_key}", f"v{i}/{lb.label_key}") for lb, arr in mine]
    unrecorded = [f"raw {i}" + (f": label {g.binding.label_key}" if isinstance(g.binding, LabelBinding) else "")
                  for g in groups if not g.binding.urls]
    if unrecorded:   # derived_from is the only record of which bytes were staged; it may not be empty
        errors.extend(f"{where}: no source URLs to record" for where in unrecorded)
        return None
    return _Volume(i, vox, samples, groups)


def geometry_check(group: _Group, p: Pyramid, samples: dict[str, list[dict]], patch: int,
                   errors: list[str]) -> dict:
    """The first GEOMETRY_SAMPLES planned samples (train, val, test), every level, read through the reader
    against a fresh snippet read of their raw_index_box, block-meaned or strided afresh (staging spec §6)."""
    down = block_mean if group.kind == "raw" else stride
    drawn = [s for split in ("train", "val", "test") for s in samples.get(split, [])][:GEOMETRY_SAMPLES]
    channels = _channels(group.array)
    mismatches = []
    for k, sample in enumerate(drawn):
        for level, info in enumerate(p.levels):
            region = p.locate(level, tuple(sample["centre_nm"]), patch, sample["plane"])
            staged = np.asarray(p.read(level, region)).reshape(channels, patch, patch)
            box = p.raw_index_box(level, region)
            outer = {"z": box["z"][0]} if "z" in box else {}
            fresh = np.stack([down(_read_yx(group.array, outer | ({"c": c} if "c" in group.axes else {}),
                                            slice(*box["y"]), slice(*box["x"])), info.factor)
                              for c in range(channels)]).astype(staged.dtype, copy=False)
            if not np.array_equal(staged, fresh):
                diff = float(np.abs(staged.astype(np.float64) - fresh.astype(np.float64)).max())
                mismatches.append({"sample": k, "level": level, "max_abs_diff": diff})
                errors.append(f"{group.name} {info.path}: sample {k} differs from a fresh source read "
                              f"(max abs diff {diff})")
    return {"samples": len(drawn), "levels": len(p.levels), "mismatches": mismatches}


def _derived_from(b: ArrayBinding, access: str, snippets_commit: str, io: IO) -> list[SourceFile]:
    """One entry per URL of the binding: its validators (and files.json's sha256 when it has one), the snippet."""
    out = []
    for url in b.urls:
        file_url, member = _split(url)
        manifest = io.cache.manifest_get(file_url) or {}
        validators = {**io.validators.get(file_url, {}),
                      **({"sha256": manifest["sha256"]} if "sha256" in manifest else {})}
        out.append(SourceFile(url=file_url, member=member, validators=validators,
                              snippet={"name": b.snippet, "commit": snippets_commit}, access=access))
    return out


def _level_records(plan: StagePlan, info: PyramidInfo, channels: int, itemsize: int) -> list[LevelRecord]:
    """Per written level: the plan's box and chunk count, the array's shape, the uncompressed bytes written."""
    return [LevelRecord(factor=lp.factor, path=li.path, origin=dict(lp.origin), extent=dict(lp.extent),
                        shape=list(li.shape), planned_planes=list(lp.planned_planes),
                        chunks_written=len(lp.chunks) * channels,
                        bytes_written=len(lp.chunks) * channels * plan.patch * plan.patch * itemsize)
            for lp, li in zip(plan.levels, info.levels)]


def _write_volume(volume: _Volume, out: Path, binding: Binding, artifact: Artifact, rule_input: PlanInput, io: IO,
                  rng: np.random.Generator, errors: list[str]) -> tuple[VolumeSummary, list[dict]]:
    """Write every group of the volume, check its geometry, then set its `mia` once; then the volume root."""
    rule = rule_input.rule
    vdir = out / f"v{volume.index}.zarr"
    nm = {a: v.nm for a, v in volume.voxel.items()}
    plan = Plan(digest=rule.digest(), rule=dataclasses.asdict(rule), samples=volume.samples)
    voxel_json = {a: VOXEL_AXIS.dump_python(v, mode="json") for a, v in volume.voxel.items()}
    provenances, checks, infos, sample = [], [], [], []
    for group in volume.groups:
        label = group.binding if isinstance(group.binding, LabelBinding) else None
        names = {"label_key": label.label_key, "segmentation_type": label.segmentation_type} if label else {}
        # what the writer (name, colors) and the reader (sparse, raw extent, voxel size) need until `mia` is set
        provisional = {"kind": group.kind, **names, "plan": plan.model_dump(mode="json"), "voxel_size": voxel_json,
                       "geometry": {"axes": group.axes, "raw_shape": list(_shape(group.array))}}
        info = write_pyramid(group.array, vdir / group.path, plan=group.plan, voxel_size_nm=nm, kind=group.kind,
                             coordinate_system=f"{binding.record}.v{volume.index}", mia=provisional,
                             downsampling_metadata={"method": "mean" if group.kind == "raw" else "nearest",
                                                    "factors": list(rule.levels), "edge": "clip",
                                                    "nearest_offset": "(L-1)//2"})
        infos.append(info)
        p = open_pyramid(vdir / group.path)
        geometry = geometry_check(group, p, volume.samples, rule.patch, errors)
        checks.append({**geometry, "mismatches": [{"group": group.name, **m} for m in geometry["mismatches"]]})
        if group.kind == "raw":
            sample = intensity_sample(p, group.plan.levels[0], rule.patch, rng)
        provenance = Provenance(
            kind=group.kind, **names, artifact=artifact,
            derived_from=_derived_from(group.binding, group.array.access, binding.snippets_commit, io),
            geometry=Geometry(raw_shape=list(info.shape), source_axes=group.array.axes, axes=info.axes,
                              dtype=info.dtype, levels=_level_records(group.plan, info, _channels(group.array),
                                                                      np.dtype(info.dtype).itemsize)),
            voxel_size=volume.voxel, plan=plan,
            statistics={"intensity_sample": sample} if group.kind == "raw" else None,
            verification=Verification(checks={"geometry": geometry}))
        zarr.open_group(vdir / group.path, mode="r+").attrs["mia"] = provenance.model_dump(mode="json")
        provenances.append(provenance)
    labels = {g.binding.label_key: g.path for g in volume.groups if isinstance(g.binding, LabelBinding)}
    write_volume_groups(vdir, list(labels), summary(provenances))
    raw_info, raw_plan = infos[0], volume.groups[0].plan
    levels = [{"factor": li.factor, "path": li.path, "shape": list(li.shape), "scale_nm": li.scale_nm,
               "translation_nm": li.translation_nm, "origin": dict(lp.origin), "extent": dict(lp.extent)}
              for li, lp in zip(raw_info.levels, raw_plan.levels)]
    return VolumeSummary(path=vdir.name, axes=raw_info.axes, shape=list(raw_info.shape), dtype=raw_info.dtype,
                         voxel_size_nm=nm, levels=levels, labels=labels, intensity_sample=sample,
                         samples=volume.samples), checks


def prefetch(binding, io: IO) -> None:
    """Every file of the binding into the dataset cache (the raw cache), a `<zip>::<glob>` url as its members, so
    the snippets then read locally; offline (a stage from the cache) each is a hit, or the error names it."""
    for b in [*binding.raw, *binding.labels]:
        for url in b.urls:
            for piece in expand(url, io):
                io.fetch(piece)


def stage(binding_path: Path, snapshot: Path, record_path: Path, io: IO, *, plan: PlanInput,
          voxel: dict[str, VoxelAxis], out: Path, seed: int = 0, stage_bytes: int | None = None) -> dict:
    """Fetch every source once (bounded by the job's byte cap, cached for a rebuild), open the arrays, plan every
    raw and refuse a plan over `stage_bytes` before any voxel is written; then write v<i>.zarr per raw with its
    labels, check the geometry, write provenance and stage.json."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if stage_bytes is None:
        stage_bytes = int(os.environ.get("MIA_HARNESS_STAGE_BYTES", io.byte_cap))
    errors: list[str] = []
    volumes: list[VolumeSummary] = []
    checks: list[dict] = []
    artifact = None
    blocked: dict = {}
    rng = np.random.default_rng(seed)
    try:
        binding = load_binding(binding_path)
        artifact = Artifact(id=plan.artifact_id, levels_key=plan.levels_key, binding_hash=binding_hash(binding_path),
                            plan_digest=plan.rule.digest(), harness_commit=plan.harness_commit,
                            env_digest=plan.env_digest, snippets_commit=binding.snippets_commit,
                            record=binding.record, record_commit=binding.record_commit)
        prefetch(binding, io)
        raws, labels = open_arrays(binding, Path(snapshot), io)
        planned = [v for i, raw in enumerate(raws)
                   if (v := _plan_volume(i, raw, binding, labels, plan, voxel, errors)) is not None]
        budget = sum(planned_bytes(g.plan, np.dtype(g.array.dtype).itemsize, _channels(g.array))
                     for v in planned for g in v.groups)
        if budget > stage_bytes:
            errors.insert(0, f"plan exceeds stage cap: {budget} > {stage_bytes} bytes")
        else:
            for volume in planned:
                summary_, volume_checks = _write_volume(volume, out, binding, artifact, plan, io, rng, errors)
                volumes.append(summary_)
                checks += volume_checks
    except HostNotAllowed as e:
        errors.append(str(e))
        blocked = {"host_not_allowed": {"host": e.host, "chain": e.chain}}
    except BudgetExceeded as e:
        errors.append(str(e))
    except Exception as e:      # snippet code can raise anything; the report says what
        errors.append(f"{type(e).__name__}: {e}")
    report = stage_json(not errors, errors, artifact, volumes, io.bytes_fetched, {},
                        verification=Verification(checks={"geometry": merge_geometry(checks)})) | blocked
    (out / "stage.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
