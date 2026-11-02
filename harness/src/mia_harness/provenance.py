"""What a staged artifact says about itself (staging spec §5): one model under every group's `attributes.mia`,
summarized on the volume root and in stage.json, so nothing exists only in stage.json."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.dataclasses import dataclass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(config=ConfigDict(extra="forbid"))
class VoxelAxis:
    """One axis's voxel size and where it came from; `basis` and `range` say how an assumed value was chosen."""
    nm: float
    source: Literal["record", "file", "assumed", "synthetic"]
    basis: str | None = None
    range: tuple[float, float] | None = None


VOXEL_AXIS = TypeAdapter(VoxelAxis)


class Artifact(_Strict):
    """The inputs the staged artifact id hashes, written once by the harness."""
    id: str
    levels_key: str
    binding_hash: str
    plan_digest: str
    harness_commit: str
    env_digest: str
    snippets_commit: str
    record: str
    record_commit: str


class SourceFile(_Strict):
    """One file the bytes came from: its validators when read, the snippet that read it and how."""
    url: str
    member: str | None
    validators: dict
    snippet: dict
    access: str


class LevelRecord(_Strict):
    """One written level: its box in raw index space, its array shape and what was written."""
    factor: int
    path: str
    origin: dict[str, int]
    extent: dict[str, int]
    shape: list[int]
    planned_planes: list[int]
    chunks_written: int
    bytes_written: int      # uncompressed: chunks_written × the chunk's item count × the item size


class Geometry(_Strict):
    """The raw extent and axis orders, the stored dtype and every written level."""
    raw_shape: list[int]    # stored (canonical) axis order
    source_axes: str        # the raw array's own axis order
    axes: str
    dtype: str
    levels: list[LevelRecord]


class Downsampling(_Strict):
    """How coarser levels were made: block means for raw, nearest samples for labels, remainders clipped."""
    raw: str = "mean"
    label: str = "nearest"
    edge: str = "clip"
    nearest_offset: str = "(L-1)//2"


class Plan(_Strict):
    """The plan rule verbatim, its digest and the samples derived from it per split."""
    digest: str
    rule: dict
    samples: dict[str, list[dict]]


class Verification(_Strict):
    """Check results: the stage's geometry check, then check-stage's schemas, coverage and digests."""
    checks: dict[str, Any] = {}
    ok: bool | None = None
    harness_commit: str | None = None
    time: str | None = None
    schemas_commit: str | None = None


class Provenance(_Strict):
    """The full `mia` attribute of a raw or label multiscale group."""
    model_config = ConfigDict(extra="forbid", validate_by_name=True, serialize_by_alias=True)
    schema_version: Literal[1] = Field(1, alias="schema")   # `schema` would shadow BaseModel.schema
    kind: Literal["raw", "label"]
    label_key: str | None = None
    segmentation_type: str | None = None
    artifact: Artifact
    derived_from: list[SourceFile]
    geometry: Geometry
    voxel_size: dict[str, VoxelAxis]
    downsampling: Downsampling = Downsampling()
    plan: Plan
    statistics: dict | None = None
    verification: Verification = Verification()


def merge_geometry(checks: list[dict]) -> dict:
    """Geometry check results summed over groups; mismatches concatenated."""
    return {"samples": sum(c["samples"] for c in checks), "levels": sum(c["levels"] for c in checks),
            "mismatches": [m for c in checks for m in c["mismatches"]]}


def summary(volume_provenances: list[Provenance]) -> dict:
    """The volume root's `mia`: the raw group's artifact, voxel size and plan digest, every group's source files,
    and the groups' geometry checks merged."""
    first = volume_provenances[0]
    derived = {(s.url, s.member): s for p in volume_provenances for s in p.derived_from}
    geometry = [p.verification.checks["geometry"] for p in volume_provenances if "geometry" in p.verification.checks]
    verification = Verification(checks={"geometry": merge_geometry(geometry)} if geometry else {})
    return {"schema": 1, "artifact": first.artifact.model_dump(mode="json"),
            "derived_from": [s.model_dump(mode="json") for s in derived.values()],
            "voxel_size": {a: VOXEL_AXIS.dump_python(v, mode="json") for a, v in first.voxel_size.items()},
            "plan": {"digest": first.plan.digest}, "verification": verification.model_dump(mode="json")}


class VolumeSummary(_Strict):
    """One volume as the registry reads it from stage.json (mia_buffers.register.volume_bodies)."""
    path: str
    axes: str
    shape: list[int]
    dtype: str
    voxel_size_nm: dict[str, float]
    levels: list[dict]      # factor, path, shape, scale_nm, translation_nm, origin, extent
    labels: dict[str, str]
    intensity_sample: list[list[float]]
    samples: dict[str, list[dict]]


def stage_json(ok: bool, errors: list[str], artifact: Artifact | None, volumes: list[VolumeSummary],
               bytes_fetched: int, digests: dict[str, str], *, verification: Verification | None = None) -> dict:
    """stage.json: the registry's index over the staged groups, built from the same models as their `mia`."""
    verification = verification or Verification()
    return {"ok": ok, "errors": list(errors), "artifact": artifact.model_dump(mode="json") if artifact else None,
            "volumes": [v.model_dump(mode="json") for v in volumes], "bytes_fetched": bytes_fetched,
            "zarr_json_sha256": dict(digests), "verification": verification.model_dump(mode="json"),
            "checks": verification.checks}
