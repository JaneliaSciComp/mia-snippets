import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from mia_harness.api import check_array
from mia_harness.binding import binding_hash, load_binding

GOOD = """
record: bbbc034-hipsc-nuclei-3d
record_commit: 37e18f6e5a1c9d0b2f4a6c8e0d1b3a5c7e9f0123
snippets_commit: 4c1d8a7b2e6f90135d4c3b2a1f0e9d8c7b6a5f4e
raw:
  - snippet: tiff-zip-member
    urls: [https://data.broadinstitute.org/bbbc/BBBC034/BBBC034_v1_dataset.zip::AICS_12_134_C=2.tif]
    params: {}
labels:
  - snippet: tiff-zip-member
    urls: [https://data.broadinstitute.org/bbbc/BBBC034/BBBC034_v1_DatasetGroundTruth.zip::ground_truth_segmented.tif]
    params: {}
    pairs_with: 0
    label_key: manual_gt-nucleus-instance
    segmentation_type: instance
reasons: ["axes zyx from ImageJ metadata, matches record", "label volume has no channel axis"]
"""


def write(tmp_path, text):
    p = tmp_path / "b.yaml"
    p.write_text(text)
    return p


def mutated(tmp_path, fn):
    d = yaml.safe_load(GOOD)
    fn(d)
    return write(tmp_path, yaml.safe_dump(d))


def test_example_parses_and_hashes(tmp_path):
    p = write(tmp_path, GOOD)
    b = load_binding(p)
    assert b.raw[0].snippet == "tiff-zip-member" and b.labels[0].segmentation_type == "instance"
    assert len(binding_hash(p)) == 64


@pytest.mark.parametrize("fn", [
    lambda d: d.update(extra=1),
    lambda d: d["raw"][0].update(extra=1),
    lambda d: d.update(raw=[]),
    lambda d: d["labels"][0].update(pairs_with=1),
    lambda d: d["labels"][0].update(pairs_with=-1),
    lambda d: d["labels"][0].update(segmentation_type="fuzzy"),
    lambda d: d["labels"].append({**d["labels"][0], "segmentation_type": "semantic"}),   # same key, same raw
])
def test_rejected(tmp_path, fn):
    with pytest.raises(ValidationError):
        load_binding(mutated(tmp_path, fn))


def test_same_label_key_on_different_raws_is_fine(tmp_path):
    def two_raws(d):
        d["raw"].append(d["raw"][0])
        d["labels"].append({**d["labels"][0], "pairs_with": 1})
    assert len(load_binding(mutated(tmp_path, two_raws)).labels) == 2


class A:
    axes, shape, dtype, voxel_size_nm, access = "zyx", (2, 3, 4), np.dtype("u1"), None, "chunk"


def test_check_array():
    check_array(A())
    for kw in [dict(axes="zxy "), dict(axes="zx"), dict(axes="zyy"), dict(axes="yx"), dict(access="net")]:
        a = A()
        for k, v in kw.items():
            setattr(a, k, v)
        with pytest.raises(ValueError):
            check_array(a)
