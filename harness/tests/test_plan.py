import pytest

from mia_harness.plan import (PlanRule, capacity, derive_samples, planned_bytes, split_planes, stage_plan,
                              start_index)
from mia_harness.sample import OVERLAP_MAX, overlap_frac

RULE = PlanRule(seed=0, patch=64, levels=(1, 2, 4), samples={"train": 10, "val": 3, "test": 3},
                split={"policy": "fractions", "fractions": {"train": 0.6, "val": 0.2, "test": 0.2}, "seed": 0},
                channel_recipe=None)
VOX = {"z": 300.0, "y": 10.0, "x": 10.0}


def test_digest_is_stable_and_record_local():
    assert RULE.digest() == PlanRule(**{**RULE.__dict__}).digest()
    assert RULE.digest() != PlanRule(**{**RULE.__dict__, "seed": 1}).digest()


def test_split_planes_golden():   # part B asserts the same numbers in tests/test_buffers_register.py
    assert split_planes("r/v0.zarr", 10, RULE.split) == {"train": [0, 1, 4, 5, 7, 8], "val": [3, 6], "test": [2, 9]}


def test_start_index_rounds_then_clamps():
    assert start_index(3000.0, 10.0, 1, 64, 600) == 268 and start_index(3000.0, 10.0, 2, 64, 600) == 118
    assert start_index(0.0, 10.0, 1, 64, 600) == 0 and start_index(1e9, 10.0, 4, 64, 600) == 150 - 64


def test_derive_is_deterministic_and_a_prefix():
    a = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX)
    b = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX)
    assert a == b and len(a["train"]) == 10 and set(a) == {"train", "val", "test"}
    more_rule = PlanRule(**{**RULE.__dict__, "samples": {"train": 20, "val": 3, "test": 3}})
    more = derive_samples(more_rule, "r", 0, "zyx", (10, 600, 700), VOX)
    assert more["train"][:10] == a["train"]
    for s in a["train"]:
        assert set(s["starts"]) == {"1", "2", "4"}


def test_derived_planes_are_the_registry_split_of_the_volume_dir():
    """The registry splits `<record>/<volume path>` (v<i>.zarr); a sample on another split's plane would leak."""
    for volume in (0, 1):
        derived = derive_samples(RULE, "r", volume, "zyx", (10, 600, 700), VOX)
        by_split = split_planes(f"r/v{volume}.zarr", 10, RULE.split)
        for split, samples in derived.items():
            assert samples and all(s["plane"]["z"] in by_split[split] for s in samples)


def test_capacity_counts_the_planes_of_each_split():
    """2x2 coarsest tiles per plane: val and test hold 2 planes each, so they cap at 8 though train holds 6."""
    greedy = PlanRule(**{**RULE.__dict__, "samples": {"train": 100, "val": 100, "test": 100}})
    derived = derive_samples(greedy, "r", 0, "zyx", (10, 600, 700), VOX)
    assert len(derived["train"]) == 2 * 2 * 6 and len(derived["val"]) <= 2 * 2 * 2 and len(derived["test"]) <= 8


def test_capacity_caps_and_eval_rejects_overlap():
    assert capacity(600, 700, 10, 64, (1, 2, 4)) == 2 * 2 * 10
    tiny = derive_samples(RULE, "r", 0, "yx", (256, 256), {"y": 10.0, "x": 10.0})
    assert sum(len(v) for v in tiny.values()) == 1   # one plane, one position: one sample in one split
    evals = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX)["val"]
    assert len({(s["plane"]["z"], tuple(s["starts"]["4"])) for s in evals}) == len(evals)


def test_2d_volume_has_no_plane_key():
    drawn = derive_samples(RULE, "r", 0, "yx", (600, 700), {"y": 10.0, "x": 10.0})   # the one plane is in one split
    assert [s["plane"] for samples in drawn.values() for s in samples[:1]] == [{}]


def test_eval_overlap_rule_rejects_what_train_accepts():
    dense = PlanRule(seed=0, patch=64, levels=(1, 2, 4), samples={"train": 8, "val": 8},
                     split={"policy": "fractions", "fractions": {"train": 0.5, "val": 0.5}, "seed": 0},
                     channel_recipe=None)
    drawn = derive_samples(dense, "r", 0, "zyx", (2, 512, 512), VOX)

    def overlaps(samples):
        footprints = [{"raw": 0, "plane": s["plane"], "y": s["starts"]["4"][0], "x": s["starts"]["4"][1]} for s in samples]
        return [overlap_frac(a, b, 64) for i, a in enumerate(footprints) for b in footprints[i + 1:]]

    assert len(drawn["val"]) >= 2 and max(overlaps(drawn["val"])) <= OVERLAP_MAX
    assert max(overlaps(drawn["train"])) > OVERLAP_MAX   # same fixture without the rule overlaps, so the rule did work


def test_stage_plan_boxes_align_and_only_touched_chunks_are_planned():
    samples = {"train": [{"plane": {"z": 2}, "centre_nm": [0, 0], "levels": [1, 2, 4], "patch": 64,
                          "starts": {"1": [100, 200], "2": [50, 100], "4": [25, 50]}}]}
    plan = stage_plan(samples, "zyx", (10, 600, 700), (1, 2, 4), 64)
    l4 = plan.levels[2]
    assert l4.origin == {"z": 2, "y": 100, "x": 200} and l4.origin["y"] % 4 == 0
    assert l4.extent == {"z": 1, "y": 64 * 4, "x": 64 * 4}
    assert l4.shape == (1, 64, 64) and l4.planned_planes == (2,) and l4.chunks == {(0, 0, 0)}
    l1 = plan.levels[0]
    # a chunk grid aligned to the box origin
    assert l1.origin == {"z": 2, "y": 100, "x": 200} and l1.chunks == {(0, 0, 0)}
    assert planned_bytes(plan, 2, 1) == 3 * 64 * 64 * 2


def test_unplanned_planes_between_planned_ones_have_no_chunks_and_overlaps_dedupe():
    s = lambda z, y0: {"plane": {"z": z}, "centre_nm": [0, 0], "levels": [1], "patch": 64, "starts": {"1": [y0, 0]}}
    plan = stage_plan({"train": [s(1, 0), s(4, 32), s(4, 40)]}, "zyx", (10, 600, 700), (1,), 64)
    l1 = plan.levels[0]
    assert l1.origin == {"z": 1, "y": 0, "x": 0} and l1.extent["z"] == 4 and l1.planned_planes == (1, 4)
    # plane 4 is box index 3; y 32..104 touches chunk rows 0 and 1
    assert l1.chunks == {(0, 0, 0), (3, 0, 0), (3, 1, 0)}


def test_stage_plan_spans_only_the_stored_axes():
    samples = {"train": [{"plane": {}, "centre_nm": [0, 0], "levels": [1, 2], "patch": 64,
                          "starts": {"1": [64, 0], "2": [40, 100]}}], "val": []}
    plan = stage_plan(samples, "yx", (600, 700), (1, 2), 64)
    l1, l2 = plan.levels
    assert l1.origin == {"y": 64, "x": 0} and l1.shape == (64, 64) and l1.chunks == {(0, 0)}
    assert l1.planned_planes == ()
    assert l2.origin == {"y": 80, "x": 200} and l2.extent == {"y": 128, "x": 128} and l2.chunks == {(0, 0)}
    with_channels = stage_plan(samples, "cyx", (3, 600, 700), (1, 2), 64).levels[0]
    assert with_channels.shape == (1, 64, 64) and with_channels.chunks == {(0, 0, 0)}
    with pytest.raises(ValueError, match="no samples planned"):
        stage_plan({"train": [], "val": []}, "yx", (600, 700), (1,), 64)


def test_plan_digest_golden():   # mia-workflows pins the same hex from its own plan_digest(rule_dict)
    rule = PlanRule(seed=0, patch=256, levels=(1, 2, 4), samples={"train": 1000, "val": 20, "test": 20},
                    split={"policy": "fractions", "fractions": {"train": 0.8, "val": 0.1, "test": 0.1}, "seed": 0},
                    channel_recipe=None)
    assert rule.digest() == "2827e3d249deb0a8bb8ecc10cb579ce0f37c9a4d2667f04af2914388bc04ae74"
