"""`harness check-stage`: a staged artifact's OME-Zarr 0.6 schemas, sample coverage, artifact ids and zarr.json
digests, recorded on every group as a `provenance:` check step (status) with the results under `mia.checks`, and in
stage.json (staging spec §6). Loads no snippet code."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .provenance import Verification, artifact_id_of, check_step, with_step
from .pyramid import NotStaged, open_pyramid
from .schemas import ngff

ERROR_PREFIX = "check-stage: "   # marks this check's lines in stage.json, so a re-run replaces rather than appends
LABELS_GROUP_SKIP = "no 0.6 schema covers a labels group"


def _group_errors(rel: str, attributes: dict, names: tuple[str, ...]) -> list[str]:
    """Every violation of each named schema by a group's attributes, as `"<rel>/zarr.json: <schema>: <error>"`."""
    return [f"{rel}/zarr.json: {name}: {e}" for name in names for e in ngff.errors(name, attributes)]


def _read_object(path: Path) -> dict:
    """A JSON object from `path`; ValueError (with the reason) when it is missing, unparsable or not an object."""
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as e:      # JSONDecodeError is a ValueError
        raise ValueError(f"cannot read: {type(e).__name__}: {e}") from e
    if not isinstance(document, dict):
        raise ValueError(f"not a JSON object but {type(document).__name__}")
    return document


def _coverage(rel: str, group: Path, mia: dict) -> tuple[int, int, list[dict]]:
    """Every planned sample of the group located at every level, at the start the plan recorded for it:
    (samples, levels, misses). A miss's `reason` is "not staged" (with the absent `keys`) or "starts"."""
    pyramid = open_pyramid(group)
    samples = [s for split in mia["plan"]["samples"].values() for s in split]
    missing = []
    for sample_index, sample in enumerate(samples):
        for level, info in enumerate(pyramid.levels):
            miss = {"group": rel, "sample": sample_index, "level": level}
            try:
                region = pyramid.locate(level, tuple(sample["centre_nm"]), sample["patch"], sample["plane"])
            except NotStaged as e:
                missing.append(miss | {"reason": "not staged", "keys": e.keys})
                continue
            located = [region[a].start + info.origin[a] // info.factor for a in "yx"]
            planned = sample["starts"].get(str(info.factor))
            if located != planned:
                missing.append(miss | {"reason": "starts", "keys": [], "located": located, "planned": planned})
    return len(samples), len(pyramid.levels), missing


def check_stage(out_dir: Path, *, harness_commit: str | None = None) -> dict:
    """Check a `harness stage` output directory and record the result on every group (a check step, `mia.checks`) and in
    stage.json (with the sha256 of every zarr.json as left on disk). Returns the report; `ok` iff schemas,
    coverage, geometry and artifact ids all pass and the stage itself succeeded. Broken input is an error line,
    never an exception; an unreadable stage.json is reported and nothing is written."""
    out_dir = Path(out_dir)
    if harness_commit is None:
        harness_commit = os.environ.get("MIA_HARNESS_COMMIT")
    time = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        stage = _read_object(out_dir / "stage.json")
    except ValueError as e:
        return {"ok": False, "errors": [f"stage.json: {e}"], "checks": {}, "zarr_json_sha256": {},
                "harness_commit": harness_commit, "time": time, "schemas_commit": ngff.SCHEMAS_COMMIT}
    errors: list[str] = []
    stage_errors = [e for e in stage.get("errors") or [] if not str(e).startswith(ERROR_PREFIX)]  # earlier runs' go
    if stage_errors:
        errors.append(f"stage.json reports {len(stage_errors)} errors")
    artifact_id = (stage.get("artifact") or {}).get("id")
    schemas: dict = {"errors": [], "applied": {}, "skipped": {}}
    strict: dict = {"image": [], "label": []}
    coverage: dict = {"samples": 0, "levels": 0, "missing": []}
    documents: list[Path] = []     # the zarr.json of every readable group, to receive the verification

    volumes = sorted(out_dir.glob("v*.zarr"))
    if not volumes:
        errors.append("no volumes")
    listed = sorted(str(v.get("path")) for v in stage.get("volumes") or [] if isinstance(v, dict))
    if [v.name for v in volumes] != listed:
        errors.append(f"volumes on disk {[v.name for v in volumes]} != stage.json {listed}")
    for vdir in volumes:
        groups = [(vdir, "image"), (vdir / "raw", "image")]
        groups += [(d.parent, "label") for d in sorted((vdir / "labels").glob("*/zarr.json"))]
        if (vdir / "labels" / "zarr.json").exists():
            schemas["skipped"][f"{vdir.name}/labels/zarr.json"] = LABELS_GROUP_SKIP
        for group, kind in groups:
            rel = group.relative_to(out_dir).as_posix()
            try:
                attributes = _read_object(group / "zarr.json").get("attributes")
            except ValueError as e:
                errors.append(f"{rel}/zarr.json: {e}")
                continue
            if not isinstance(attributes, dict):
                errors.append(f"{rel}/zarr.json: no attributes object")
                continue
            names = ("image", "ome_zarr") if kind == "image" else ("image", "label", "ome_zarr")
            schemas["applied"][f"{rel}/zarr.json"] = list(names)
            schemas["errors"] += _group_errors(rel, attributes, names)
            strict["image"] += _group_errors(rel, attributes, ("strict_image",))
            if kind == "label":
                strict["label"] += _group_errors(rel, attributes, ("strict_label",))
            documents.append(group / "zarr.json")
            mia = attributes.get("mia") if isinstance(attributes.get("mia"), dict) else {}
            group_id = artifact_id_of(attributes)
            if group_id != artifact_id:
                errors.append(f"{rel}: artifact id {group_id} != stage.json {artifact_id}")
            if "samples" not in (mia.get("plan") or {}):
                continue        # the volume root carries the plan digest only
            try:
                samples, levels, missing = _coverage(rel, group, mia)
            except Exception as e:     # an unreadable group is a failed check, not a crash without a report
                errors.append(f"{rel}: cannot read the pyramid: {type(e).__name__}: {e}")
                continue
            coverage["samples"] += samples
            coverage["levels"] += levels
            coverage["missing"] += missing
    errors += schemas["errors"]
    errors += [f"{m['group']}: sample {m['sample']} level {m['level']} " + (
        f"not staged: {m['keys']}" if m["reason"] == "not staged"
        else f"located at {m['located']}, planned at {m['planned']}") for m in coverage["missing"]]
    geometry = (stage.get("checks") or {}).get("geometry")
    geometry = geometry if isinstance(geometry, dict) else {}
    if not isinstance(geometry.get("mismatches"), list):
        errors.append("stage.json has no geometry check")
    elif geometry["mismatches"]:
        errors.append(f"geometry: {len(geometry['mismatches'])} mismatches")
    ok = not errors
    checks = {"schemas": schemas, "strict": strict, "coverage": coverage, "geometry": geometry}
    verification = Verification(checks=checks, ok=ok, harness_commit=harness_commit, time=time,
                                schemas_commit=ngff.SCHEMAS_COMMIT).model_dump(mode="json")
    step = check_step(ok, harness_commit, ngff.SCHEMAS_COMMIT, time, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for path in documents:
        document = json.loads(path.read_text())
        mia = document["attributes"].get("mia")
        attributes = {**document["attributes"], "mia": {**(mia if isinstance(mia, dict) else {}), "checks": checks}}
        document["attributes"] = with_step(attributes, step)
        path.write_text(json.dumps(document, indent=2))
    digests = {p.relative_to(out_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(out_dir.rglob("zarr.json"))}
    stage |= {"ok": ok, "errors": stage_errors + [ERROR_PREFIX + e for e in errors], "zarr_json_sha256": digests,
              "verification": verification, "checks": checks}
    (out_dir / "stage.json").write_text(json.dumps(stage, separators=(",", ":")) + "\n")
    return {"ok": ok, "errors": errors, "checks": checks, "zarr_json_sha256": digests,
            "harness_commit": harness_commit, "time": time, "schemas_commit": ngff.SCHEMAS_COMMIT}
