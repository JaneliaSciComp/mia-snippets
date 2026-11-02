import pytest
from pydantic import ValidationError

from mia_harness.provenance import (Artifact, Geometry, Plan, Provenance, SourceFile, Verification, VolumeSummary,
                                    VoxelAxis, stage_json, summary)

ART = Artifact(id="a" * 16, levels_key="p64-l1-2-4", binding_hash="b" * 64, plan_digest="d" * 64,
               harness_commit="h" * 40, env_digest="e" * 64, snippets_commit="1" * 40, record="r",
               record_commit="0" * 40)
VOL = VolumeSummary(path="v0.zarr", axes="zyx", shape=[3, 600, 700], dtype="uint16",
                    voxel_size_nm={"z": 300.0, "y": 8.0, "x": 8.0},
                    levels=[{"factor": 1, "path": "s0", "shape": [1, 64, 64], "scale_nm": {}, "translation_nm": {},
                             "origin": {"z": 1, "y": 0, "x": 0}, "extent": {"z": 1, "y": 64, "x": 64}}],
                    labels={"gt": "labels/gt"}, intensity_sample=[[0.0, 1.0]],
                    samples={"train": [], "val": [], "test": []})


def provenance(**kw) -> Provenance:
    return Provenance(kind="raw", artifact=ART, geometry=Geometry(raw_shape=[3, 600, 700], source_axes="zyx",
                                                                  axes="zyx", dtype="uint16", levels=[]),
                      derived_from=[SourceFile(url="http://h/a.npy", member=None, validators={"size": 1},
                                               snippet={"name": "npy-local", "commit": "1" * 40}, access="chunk")],
                      voxel_size={"y": VoxelAxis(8.0, "assumed", "test")},
                      plan=Plan(digest="d" * 64, rule={}, samples={}), **kw)


def test_stage_json_keeps_the_registry_keys():
    sj = stage_json(True, [], ART, [VOL], 0, {"v0.zarr/raw/zarr.json": "ab"})
    assert set(sj["volumes"][0]) >= {"path", "axes", "shape", "dtype", "voxel_size_nm", "levels", "labels",
                                     "intensity_sample", "samples"}
    assert sj["artifact"]["plan_digest"] == ART.plan_digest
    assert sj["zarr_json_sha256"] == {"v0.zarr/raw/zarr.json": "ab"}
    assert {"ok", "errors", "bytes_fetched", "verification", "checks"} <= set(sj)


def test_provenance_dumps_schema_1_and_forbids_extra_fields():
    dumped = provenance().model_dump(mode="json")
    assert dumped["schema"] == 1 and dumped["voxel_size"]["y"] == {"nm": 8.0, "source": "assumed", "basis": "test",
                                                                   "range": None}
    assert dumped["downsampling"] == {"raw": "mean", "label": "nearest", "edge": "clip", "nearest_offset": "(L-1)//2"}
    assert Provenance.model_validate(dumped) == provenance()
    with pytest.raises(ValidationError):
        Provenance.model_validate({**dumped, "extra": 1})
    with pytest.raises(ValidationError):
        VoxelAxis(8.0, "guessed")


def test_summary_merges_the_groups():
    geometry = {"samples": 2, "levels": 3, "mismatches": [{"sample": 0, "level": 1, "max_abs_diff": 1.0}]}
    raw = provenance(verification=Verification(checks={"geometry": geometry}))
    label = provenance(verification=Verification(checks={"geometry": {**geometry, "mismatches": []}}))
    root = summary([raw, label])
    assert root["artifact"]["id"] == "a" * 16 and root["plan"] == {"digest": "d" * 64}
    assert len(root["derived_from"]) == 1
    assert root["verification"]["checks"]["geometry"] == {"samples": 4, "levels": 6,
                                                          "mismatches": geometry["mismatches"]}
