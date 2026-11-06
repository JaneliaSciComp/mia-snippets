import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from mia_harness.provenance import (CONVENTION, HARNESS_URI, Artifact, Geometry, Plan, Provenance, SourceFile,
                                    VolumeSummary, VoxelAxis, artifact_id_of, check_step, record_of, stage_json,
                                    stage_step, summary, upgrade_schema_1, with_step)

CONVENTION_SCHEMA = Draft202012Validator(json.loads((Path(__file__).parent / "fixtures" / "provenance-0.2.0.schema.json")
                                                    .read_text()))
ART = Artifact(id="a" * 16, levels_key="p64-l1-2-4", binding_hash="b" * 64, plan_digest="d" * 64,
               harness_commit="h" * 40, env_digest="e" * 64, snippets_commit="1" * 40, record="r",
               record_commit="0" * 40)
SRC = SourceFile(url="http://h/a.npy", member=None, validators={"size": 1}, snippet={"name": "npy-local", "commit": "1" * 40},
                 access="chunk")
VOL = VolumeSummary(path="v0.zarr", axes="zyx", shape=[3, 600, 700], dtype="uint16",
                    voxel_size_nm={"z": 300.0, "y": 8.0, "x": 8.0},
                    levels=[{"factor": 1, "path": "s0", "shape": [1, 64, 64], "scale_nm": {}, "translation_nm": {},
                             "origin": {"z": 1, "y": 0, "x": 0}, "extent": {"z": 1, "y": 64, "x": 64}}],
                    labels={"gt": "labels/gt"}, intensity_sample=[[0.0, 1.0]],
                    samples={"train": [], "val": [], "test": []})
# a group as the harness wrote it before schema 2 (staging spec v0.2 §5), trimmed to one level and no samples
SCHEMA_1 = {"schema": 1, "kind": "raw", "label_key": None, "segmentation_type": None,
            "artifact": ART.model_dump(mode="json"), "derived_from": [SRC.model_dump(mode="json")],
            "geometry": {"raw_shape": [3, 600, 700], "source_axes": "zyx", "axes": "zyx", "dtype": "uint16",
                         "levels": [{"factor": 1, "path": "s0", "origin": {}, "extent": {}, "shape": [], "planned_planes": [],
                                     "chunks_written": 1, "bytes_written": 2}]},
            "voxel_size": {"y": {"nm": 8.0, "source": "assumed", "basis": "test", "range": None}},
            "downsampling": {"raw": "mean", "label": "nearest", "edge": "clip", "nearest_offset": "(L-1)//2"},
            "plan": {"digest": "d" * 64, "rule": {"seed": 0, "patch": 64}, "samples": {"train": [{"plane": {"z": 0}}]}},
            "statistics": {"intensity_sample": [[0.0]]},
            "verification": {"checks": {"geometry": {"samples": 1, "levels": 1, "mismatches": []}}, "ok": None,
                             "harness_commit": None, "time": None, "schemas_commit": None}}


def convention_errors(attributes: dict) -> list[str]:
    """Violations of the `provenance:` convention schema by a group's attributes, as a zarr.json would carry them."""
    document = {"zarr_format": 3, "node_type": "group", "attributes": attributes}
    return [e.message for e in CONVENTION_SCHEMA.iter_errors(document)]


def provenance(**kw) -> Provenance:
    return Provenance(geometry=Geometry(raw_shape=[3, 600, 700], source_axes="zyx", axes="zyx"),
                      plan=Plan(samples={}), **kw)


def test_stage_json_keeps_the_registry_keys():
    sj = stage_json(True, [], ART, [VOL], 0, {"v0.zarr/raw/zarr.json": "ab"})
    assert set(sj["volumes"][0]) >= {"path", "axes", "shape", "dtype", "voxel_size_nm", "levels", "labels",
                                     "intensity_sample", "samples"}
    assert sj["artifact"]["plan_digest"] == ART.plan_digest
    assert sj["zarr_json_sha256"] == {"v0.zarr/raw/zarr.json": "ab"}
    assert {"ok", "errors", "bytes_fetched", "verification", "checks"} <= set(sj)


def test_provenance_dumps_schema_2_and_forbids_extra_fields():
    dumped = provenance().model_dump(mode="json")
    assert dumped == {"schema": 2, "segmentation_type": None, "geometry": {"raw_shape": [3, 600, 700], "source_axes": "zyx",
                                                                           "axes": "zyx"},
                      "plan": {"samples": {}}, "statistics": None, "checks": {}}
    assert Provenance.model_validate(dumped) == provenance()
    with pytest.raises(ValidationError):
        Provenance.model_validate({**dumped, "artifact": {}})
    with pytest.raises(ValidationError):
        VoxelAxis(8.0, "guessed")


def test_stage_step_validates_and_names_the_software_environment_runtime_dependencies_inputs_and_output():
    hashed = SRC.model_copy(update={"validators": {"size": 1, "sha256": "f" * 64}})
    member = SRC.model_copy(update={"member": "train/*.tif"})
    step = stage_step(ART, [hashed, member, member], {"plan": {"seed": 0}}, started="2026-11-04T10:00:00+00:00",
                      ended="2026-11-04T10:01:00+00:00")
    attributes = {"zarr_conventions": [CONVENTION], "provenance:steps": [step]}
    assert convention_errors(attributes) == []
    assert step["software"] == {"name": "mia-harness", "version": "h" * 40, "uri": HARNESS_URI}
    assert step["environment"] == {"name": "pixi.lock", "checksum": "sha256:" + "e" * 64,
                                   "uri": f"{HARNESS_URI}/blob/{'h' * 40}/harness/pixi.lock"}
    assert step["runtime-dependencies"] == [{"name": "npy-local", "version": "1" * 40, "uri": HARNESS_URI}]
    assert step["parameters"] == {"plan": {"seed": 0}}
    assert step["inputs"] == [{"id": "r", "name": "record"}, {"uri": "http://h/a.npy", "checksum": "sha256:" + "f" * 64},
                              {"uri": "http://h/a.npy", "name": "train/*.tif"}]     # each source once
    assert step["outputs"] == [{"id": "a" * 16, "name": "artifact"}] and step["started"] < step["ended"]
    assert artifact_id_of(attributes) == "a" * 16 and record_of(attributes) == "r"
    bare = stage_step(ART.model_copy(update={"harness_commit": "", "env_digest": ""}), [], {})
    assert bare["software"]["version"] == "unknown" and "environment" not in bare and "started" not in bare
    assert convention_errors({"zarr_conventions": [CONVENTION], "provenance:steps": [bare]}) == []


def test_check_step_is_appended_once_with_its_status():
    first = check_step(False, None, "0.6", "2026-11-04T10:00:00Z", "2026-11-04T10:00:01Z")
    attributes = with_step({"provenance:steps": [stage_step(ART, [SRC], {})]}, first)
    attributes = with_step(attributes, check_step(True, "h" * 40, "0.6", "2026-11-04T11:00:00Z", None))
    assert convention_errors(attributes) == []
    names = [s["name"] for s in attributes["provenance:steps"]]
    assert names == ["Sparse OME-Zarr staging", "check-stage"]       # a re-run replaces, never appends
    check = attributes["provenance:steps"][-1]
    assert check["status"] == "ok" and check["parameters"] == {"schemas_commit": "0.6"} and "ended" not in check
    assert artifact_id_of(attributes) == "a" * 16


def test_upgrade_schema_1_splits_mia_into_schema_2_and_the_steps():
    upgraded = upgrade_schema_1({"ome": {"version": "0.6"}, "mia": SCHEMA_1}, rule=None)
    assert convention_errors(upgraded) == [] and upgraded["ome"] == {"version": "0.6"}
    assert upgraded["mia"] == {"schema": 2, "geometry": {"raw_shape": [3, 600, 700], "source_axes": "zyx", "axes": "zyx"},
                               "plan": {"samples": {"train": [{"plane": {"z": 0}}]}}, "statistics": {"intensity_sample": [[0.0]]},
                               "checks": {"geometry": {"samples": 1, "levels": 1, "mismatches": []}}}
    assert Provenance.model_validate(upgraded["mia"])
    step, = upgraded["provenance:steps"]       # never checked: no check step
    assert step["parameters"] == {"plan": {"seed": 0, "patch": 64}, "voxel_size": SCHEMA_1["voxel_size"]}
    assert step["environment"]["checksum"] == "sha256:" + "e" * 64 and step["runtime-dependencies"][0]["name"] == "npy-local"
    assert "started" not in step and artifact_id_of(upgraded) == "a" * 16 and record_of(upgraded) == "r"
    # the volume root: plan digest only, so the rule comes from the raw group; a verification that ran becomes a step
    root = {**SCHEMA_1, "plan": {"digest": "d" * 64}, "kind": None, "geometry": None, "statistics": None,
            "verification": {**SCHEMA_1["verification"], "ok": True, "harness_commit": "h" * 40, "time": "2026-11-02T18:04:20+00:00",
                             "schemas_commit": "0.6"}}
    root.pop("geometry"), root.pop("statistics")
    upgraded = upgrade_schema_1({"mia": root}, rule={"seed": 0, "patch": 64})
    assert convention_errors(upgraded) == [] and set(upgraded["mia"]) == {"schema", "checks"}
    stage, check = upgraded["provenance:steps"]
    assert stage["parameters"]["plan"] == {"seed": 0, "patch": 64}
    assert check["status"] == "ok" and check["software"]["version"] == "h" * 40 and check["ended"] == "2026-11-02T18:04:20+00:00"


def test_summary_merges_the_groups():
    geometry = {"samples": 2, "levels": 3, "mismatches": [{"sample": 0, "level": 1, "max_abs_diff": 1.0}]}
    raw = provenance(checks={"geometry": geometry})
    label = provenance(checks={"geometry": {**geometry, "mismatches": []}})
    assert summary([raw, label]) == {"schema": 2, "checks": {"geometry": {"samples": 4, "levels": 6,
                                                                           "mismatches": geometry["mismatches"]}}}
