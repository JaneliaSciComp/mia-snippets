"""What a staged artifact says about itself (staging spec §5). How it was made is the `provenance:` Zarr convention
(v0.2.0): a staging step and a check step on every group. `attributes.mia` (schema 2) keeps only what neither
OME-Zarr nor the convention carries and the reader or checker needs: the raw extent, the planned samples, the
intensity sample, the check results. stage.json is built from the same models."""
import hashlib
import json
from pathlib import Path
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
    """The inputs the staged artifact id hashes, as stage.json records them."""
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


class Geometry(_Strict):
    """The raw extent and axis orders: what places a sparse level box in the raw volume."""
    raw_shape: list[int]    # stored (canonical) axis order
    source_axes: str        # the raw array's own axis order
    axes: str


class Plan(_Strict):
    """The samples derived from the plan rule, per split; the rule itself is a parameter of the staging step."""
    samples: dict[str, list[dict]]


class Verification(_Strict):
    """stage.json's check results: the stage's geometry check, then check-stage's schemas, coverage and digests."""
    checks: dict[str, Any] = {}
    ok: bool | None = None
    harness_commit: str | None = None
    time: str | None = None
    schemas_commit: str | None = None


class Provenance(_Strict):
    """The `mia` attribute of a raw or label multiscale group."""
    model_config = ConfigDict(extra="forbid", validate_by_name=True, serialize_by_alias=True)
    schema_version: Literal[2] = Field(2, alias="schema")   # `schema` would shadow BaseModel.schema
    segmentation_type: str | None = None
    geometry: Geometry
    plan: Plan
    statistics: dict | None = None
    checks: dict[str, Any] = {}


def merge_geometry(checks: list[dict]) -> dict:
    """Geometry check results summed over groups; mismatches concatenated."""
    return {"samples": sum(c["samples"] for c in checks), "levels": sum(c["levels"] for c in checks),
            "mismatches": [m for c in checks for m in c["mismatches"]]}


def summary(volume_provenances: list[Provenance]) -> dict:
    """The volume root's `mia`: the groups' geometry checks merged."""
    geometry = [p.checks["geometry"] for p in volume_provenances if "geometry" in p.checks]
    return {"schema": 2, "checks": {"geometry": merge_geometry(geometry)} if geometry else {}}


# --- the `provenance:` Zarr convention -------------------------------------------------------------------------

CONVENTION = {"uuid": "0d4ef942-0f74-4187-ae72-016ddf083fe1", "name": "provenance:",
              "schema_url": "https://raw.githubusercontent.com/BioImageTools/zarr-convention-provenance/refs/tags/v0.2.0/schema.json",
              "spec_url": "https://github.com/BioImageTools/zarr-convention-provenance/blob/v0.2.0/README.md",
              "description": "The processing steps that produced or modified this node"}
HARNESS_URI = "https://github.com/JaneliaSciComp/mia-snippets"
STAGE_STEP = "Sparse OME-Zarr staging"
CHECK_STEP = "check-stage"
OLD_DEPENDENCY_KEYS = {"modules", "runtime-dependencies"}   # pre-release spellings of `runtime_dependencies`


def _software(commit: str | None) -> dict:
    return {"name": "mia-harness", "version": commit or "unknown", "uri": HARNESS_URI}


def _times(started: str | None, ended: str | None) -> dict:
    return {k: v for k, v in (("started", started), ("ended", ended)) if v}


def stage_step(artifact: Artifact, sources: list[SourceFile], parameters: dict, *, started: str | None = None,
               ended: str | None = None) -> dict:
    """The staging step: the harness, its lockfile, the snippets it loaded, the parameters as received, every source
    file (and the record) as inputs, the artifact id as the output."""
    step: dict = {"name": STAGE_STEP, "software": _software(artifact.harness_commit)}
    if artifact.env_digest:
        step["environment"] = {"name": "pixi.lock", "checksum": f"sha256:{artifact.env_digest}"}
        if artifact.harness_commit:
            step["environment"]["uri"] = f"{HARNESS_URI}/blob/{artifact.harness_commit}/harness/pixi.lock"
    snippets = sorted({(s.snippet["name"], s.snippet["commit"]) for s in sources})
    step["runtime_dependencies"] = [{"name": name, "version": commit, "uri": HARNESS_URI} for name, commit in snippets]
    step["parameters"] = parameters
    inputs: list[dict] = [{"id": artifact.record, "name": "record"}]
    for source in {(s.url, s.member): s for s in sources}.values():
        reference = {"uri": source.url}
        if source.member:
            reference["name"] = source.member
        if sha256 := source.validators.get("sha256"):
            reference["checksum"] = f"sha256:{sha256}"
        inputs.append(reference)
    step["inputs"] = inputs
    step["outputs"] = [{"id": artifact.id, "name": "artifact"}]
    return step | _times(started, ended)


def check_step(ok: bool, harness_commit: str | None, schemas_commit: str | None, started: str | None,
               ended: str | None) -> dict:
    """The check-stage step: what checked, against which schemas, and whether it passed."""
    step: dict = {"name": CHECK_STEP, "software": _software(harness_commit)}
    if schemas_commit:
        step["parameters"] = {"schemas_commit": schemas_commit}
    step["status"] = "ok" if ok else "failed"
    return step | _times(started, ended)


def convention_attributes(steps: list[dict]) -> dict:
    return {"zarr_conventions": [CONVENTION], "provenance:steps": steps}


def with_step(attributes: dict, step: dict) -> dict:
    """`attributes` with `step` appended to the convention's steps, replacing an earlier step of the same name."""
    steps = [s for s in attributes.get("provenance:steps") or [] if s.get("name") != step["name"]]
    return {**attributes, **convention_attributes(steps + [step])}


def _stage_step_of(attributes: dict) -> dict:
    return next((s for s in attributes.get("provenance:steps") or [] if s.get("name") == STAGE_STEP), {})


def artifact_id_of(attributes: dict) -> str | None:
    """The artifact id a group's staging step names as its output."""
    outputs = _stage_step_of(attributes).get("outputs") or [{}]
    return outputs[0].get("id")


def record_of(attributes: dict) -> str | None:
    """The record a group's staging step names among its inputs."""
    return next((i.get("id") for i in _stage_step_of(attributes).get("inputs") or [] if i.get("name") == "record"),
                None)


def upgrade_schema_1(attributes: dict, rule: dict | None) -> dict:
    """A group's attributes with its schema-1 `mia` (artifact, derived_from, voxel_size, plan rule and digest,
    downsampling, kind, label_key, geometry levels, verification) split into schema 2 and the convention steps it
    implied. `rule` stands in for the plan rule where the group recorded only its digest (the volume root)."""
    mia = attributes["mia"]
    artifact = Artifact(**mia["artifact"])
    sources = [SourceFile(**s) for s in mia["derived_from"]]
    parameters = {k: v for k, v in (("plan", mia["plan"].get("rule", rule)), ("voxel_size", mia.get("voxel_size")))
                  if v is not None}
    steps = [stage_step(artifact, sources, parameters)]
    verification = mia.get("verification") or {}
    if verification.get("ok") is not None:
        steps.append(check_step(verification["ok"], verification.get("harness_commit"),
                                verification.get("schemas_commit"), verification.get("time"), verification.get("time")))
    slim: dict = {"schema": 2}
    if mia.get("segmentation_type"):
        slim["segmentation_type"] = mia["segmentation_type"]
    if "geometry" in mia:
        slim["geometry"] = {k: mia["geometry"][k] for k in ("raw_shape", "source_axes", "axes")}
    if "samples" in mia["plan"]:
        slim["plan"] = {"samples": mia["plan"]["samples"]}
    if mia.get("statistics") is not None:
        slim["statistics"] = mia["statistics"]
    slim["checks"] = verification.get("checks") or {}
    kept = {k: v for k, v in attributes.items() if k not in ("mia", "zarr_conventions", "provenance:steps")}
    return {**kept, "mia": slim, **convention_attributes(steps)}


def add_convention(out_dir: Path) -> dict:
    """Bring every group of a staged artifact under `out_dir` to schema 2 with the convention steps, in place, and
    refresh stage.json's zarr.json digests if it has any. Groups already at schema 2 only have a pre-release
    spelling of `runtime_dependencies` renamed; a `mia` that predates `artifact` cannot be upgraded. Returns counts: written,
    present, skipped."""
    out_dir = Path(out_dir)
    counts = {"written": 0, "present": 0, "skipped": 0}
    for path in sorted(out_dir.rglob("zarr.json")):
        document = json.loads(path.read_text())
        attributes = document.get("attributes") or {}
        mia = attributes.get("mia")
        if not isinstance(mia, dict):
            continue
        if mia.get("schema") == 2:
            stale = [s for s in attributes.get("provenance:steps") or [] if s.keys() & OLD_DEPENDENCY_KEYS]
            for step in stale:
                step["runtime_dependencies"] = step.pop(next(iter(step.keys() & OLD_DEPENDENCY_KEYS)))
            if stale:
                path.write_text(json.dumps(document, indent=2))
                counts["written"] += 1
            else:
                counts["present"] += 1
        elif "artifact" not in mia:
            counts["skipped"] += 1
        else:
            rule = None
            if "rule" not in mia["plan"]:     # the volume root: the raw group beside it recorded the rule
                raw = json.loads((path.parent / "raw" / "zarr.json").read_text())["attributes"]
                rule = (raw["mia"].get("plan") or {}).get("rule") or (_stage_step_of(raw).get("parameters") or {}).get("plan")
            document["attributes"] = upgrade_schema_1(attributes, rule)
            path.write_text(json.dumps(document, indent=2))
            counts["written"] += 1
    stage_path = out_dir / "stage.json"
    if counts["written"] and stage_path.exists():
        stage = json.loads(stage_path.read_text())
        if stage.get("zarr_json_sha256"):
            stage["zarr_json_sha256"] = {p.relative_to(out_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in sorted(out_dir.rglob("zarr.json"))}
            stage_path.write_text(json.dumps(stage, separators=(",", ":")) + "\n")
    return counts


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
