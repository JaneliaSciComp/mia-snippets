import dataclasses
import json

import pytest

from mia_harness import plan
from mia_harness.plan import (PlanRule, derive_samples, load_plan, planes_per_split, planned_bytes, select_samples,
                              share_out, split_planes, stage_plan, start_index)
from mia_harness.sample import overlap_frac

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


COUNTS = {"train": 10, "val": 3, "test": 3}


def test_planes_per_split_is_zero_for_a_volume_under_the_footprint():
    assert planes_per_split(RULE, "r", 0, "zyx", (10, 600, 700)) == {"train": 6, "val": 2, "test": 2}
    assert planes_per_split(RULE, "r", 0, "yx", (200, 200)) == {"train": 0, "val": 0, "test": 0}   # footprint 256


def test_share_out_follows_planes_and_only_adds():
    planes = [{"train": 6, "val": 2, "test": 2}, {"train": 60, "val": 20, "test": 20},
              {"train": 0, "val": 0, "test": 0}]
    rule = PlanRule(**{**RULE.__dict__, "samples": {"train": 1100, "val": 3, "test": 3}})
    shares = share_out(rule, "r", planes)
    assert [sum(s[name] for s in shares) for name in ("train", "val", "test")] == [1100, 3, 3]
    assert shares[2] == {"train": 0, "val": 0, "test": 0}
    assert 60 <= shares[0]["train"] <= 140   # 1/11 of 1100
    more = share_out(PlanRule(**{**RULE.__dict__, "samples": {"train": 2200, "val": 3, "test": 3}}), "r", planes)
    assert all(m["train"] >= s["train"] for m, s in zip(more, shares))
    assert share_out(rule, "r", [planes[2]]) == [{"train": 0, "val": 0, "test": 0}]   # nothing fits: no crops


def test_derive_is_deterministic_and_a_prefix():
    a = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX, COUNTS)
    assert a == derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX, COUNTS)
    assert len(a["train"]) == 10 and set(a) == {"train", "val", "test"}
    more = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX, {**COUNTS, "train": 25})
    assert more["train"][:10] == a["train"] and len(more["train"]) == 25
    for s in a["train"]:
        assert set(s["starts"]) == {"1", "2", "4"}


def test_derived_planes_are_the_registry_split_of_the_volume_dir():
    """The registry splits `<record>/<volume path>` (v<i>.zarr); a sample on another split's plane would leak."""
    for volume in (0, 1):
        derived = derive_samples(RULE, "r", volume, "zyx", (10, 600, 700), VOX, COUNTS)
        by_split = split_planes(f"r/v{volume}.zarr", 10, RULE.split)
        for split, samples in derived.items():
            assert samples and all(s["plane"]["z"] in by_split[split] for s in samples)


def test_crops_are_random_inside_the_volume_on_their_splits_planes():
    wanted = {"train": 400, "val": 50, "test": 50}   # far over the 2x2 footprints a plane holds side by side
    planes = split_planes("r/v0.zarr", 10, RULE.split)
    drawn = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX, wanted)
    for split, samples in drawn.items():
        assert len(samples) == wanted[split]
        for sample in samples:
            assert sample["plane"]["z"] in planes[split]
            y, x = sample["starts"]["4"]
            assert 0 <= y <= (600 - 256) // 4 and 0 <= x <= (700 - 256) // 4
    assert len({tuple(s["starts"]["4"]) for s in drawn["train"]}) > 4   # positions, not four tiles


def test_a_volume_exactly_one_footprint_wide_holds_one_crop_at_zero():
    drawn = derive_samples(RULE, "r", 0, "yx", (256, 256), {"y": 10.0, "x": 10.0}, {"train": 5, "val": 5, "test": 5})
    # the one plane is in one split and has one position: five draws of it are one crop, the other splits get []
    assert sum(len(s) for s in drawn.values()) == 1
    assert all(s["starts"] == {"1": [96, 96], "2": [32, 32], "4": [0, 0]} for split in drawn.values() for s in split)


def test_a_position_drawn_twice_is_one_crop_and_a_larger_count_still_only_adds():
    """A 260x260 plane holds 2x2 positions (starts 0 and 4 on each axis): no list repeats one, however many draws."""
    few = derive_samples(RULE, "r", 0, "yx", (260, 260), {"y": 10.0, "x": 10.0}, {"train": 3, "val": 3, "test": 3})
    many = derive_samples(RULE, "r", 0, "yx", (260, 260), {"y": 10.0, "x": 10.0}, {"train": 99, "val": 99, "test": 99})
    for split, samples in many.items():
        keys = [tuple(s["starts"]["1"]) for s in samples]
        assert len(keys) == len(set(keys)) and len(keys) in (0, 4)
        assert samples[:len(few[split])] == few[split]


def test_2d_volume_has_no_plane_key():
    drawn = derive_samples(RULE, "r", 0, "yx", (600, 700), {"y": 10.0, "x": 10.0}, COUNTS)   # one plane, one split
    assert [s["plane"] for samples in drawn.values() for s in samples[:1]] == [{}]


def test_crops_of_a_split_may_overlap():
    """One plane per split holds 2x2 footprints side by side; 50 crops on it overlap, each finer crop inside its
    coarsest footprint."""
    dense = PlanRule(seed=0, patch=64, levels=(1, 2, 4), samples={"train": 50, "val": 50},
                     split={"policy": "fractions", "fractions": {"train": 0.5, "val": 0.5}, "seed": 0},
                     channel_recipe=None)
    drawn = derive_samples(dense, "r", 0, "zyx", (2, 512, 512), VOX, dense.samples)
    for split, samples in drawn.items():
        assert 40 < len(samples) <= 50   # a position drawn twice is listed once
        boxes = [{"raw": 0, "plane": s["plane"], "y": s["starts"]["4"][0] * 4, "x": s["starts"]["4"][1] * 4}
                 for s in samples]
        assert any(overlap_frac(a, b, 256) > 0 for i, a in enumerate(boxes) for b in boxes[i + 1:])
        for s in samples:   # a finer crop lies inside the coarsest footprint
            (y4, x4), (y1, x1) = s["starts"]["4"], s["starts"]["1"]
            assert 4 * y4 <= y1 and y1 + 64 <= 4 * y4 + 256 and 4 * x4 <= x1 and x1 + 64 <= 4 * x4 + 256


def test_another_seed_draws_other_crops():
    drawn = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX, COUNTS)
    other = derive_samples(PlanRule(**{**RULE.__dict__, "seed": 1}), "r", 0, "zyx", (10, 600, 700), VOX, COUNTS)
    assert other["train"] != drawn["train"]


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


FILTERS = ({"name": "content"}, {"name": "labelled", "min_fraction": 0.05})


def golden_rule(**more) -> PlanRule:
    return PlanRule(seed=0, patch=256, levels=(1, 2, 4), samples={"train": 1000, "val": 20, "test": 20},
                    split={"policy": "fractions", "fractions": {"train": 0.8, "val": 0.1, "test": 0.1}, "seed": 0},
                    channel_recipe=None, **more)


def test_a_rule_without_filters_documents_none():   # so its digest and its recorded parameters stay what they were
    assert "filters" not in golden_rule().document()
    assert golden_rule().digest() == "2827e3d249deb0a8bb8ecc10cb579ce0f37c9a4d2667f04af2914388bc04ae74"


def test_plan_digest_with_filters_golden():   # mia-workflows pins the same hex from its own plan_digest(rule_dict)
    rule = golden_rule(filters=FILTERS)
    assert rule.document()["filters"] == list(FILTERS)
    assert rule.digest() == "38da38f37fee0b1dc5f08cd3b544eab367376ece8c262b1aed2e2b8b2b016ce8"


def test_load_plan_reads_filters(tmp_path):
    rule = {"seed": 0, "patch": 64, "levels": [1, 2], "samples": {"train": 1}, "split": {}, "channel_recipe": None}
    path = tmp_path / "plan-rule.json"
    path.write_text(json.dumps({"artifact_id": "a", "levels_key": "k", "rule": rule}))
    assert load_plan(path).rule.filters == ()
    path.write_text(json.dumps({"artifact_id": "a", "levels_key": "k", "rule": {**rule, "filters": list(FILTERS)}}))
    assert load_plan(path).rule.filters == FILTERS


def select(counts, keep=None, shape=(40, 2000, 2000), rule=RULE):
    return select_samples(rule, "r", 0, "zyx", shape, VOX, counts, keep)


def test_select_without_keep_is_derive():
    counts = {"train": 10, "val": 3, "test": 3}
    samples, selection = select(counts)
    assert samples == derive_samples(RULE, "r", 0, "zyx", (40, 2000, 2000), VOX, counts)
    assert selection["train"] == {"wanted": 10, "staged": 10, "draws": 10, "rejected": {}}


def test_keep_rejections_are_redrawn_until_the_count():
    calls = []

    def every_other(sample):
        calls.append(sample)
        return "content" if len(calls) % 2 else None

    samples, selection = select({"train": 10}, every_other)
    assert len(samples["train"]) == 10
    assert selection["train"] == {"wanted": 10, "staged": 10, "draws": 20, "rejected": {"content": 10}}
    assert samples["train"] == calls[1::2]   # the kept ones, in the order they were drawn


def test_the_search_gives_up_after_a_run_of_rejections(monkeypatch):
    monkeypatch.setattr(plan, "GIVE_UP", 7)
    seen = []

    def third_only(sample):
        seen.append(sample)
        return None if len(seen) == 3 else "labelled"

    samples, selection = select({"train": 5}, third_only)
    assert len(samples["train"]) == 1 and len(seen) == 3 + 7   # the run starts again after the kept one
    assert selection["train"] == {"wanted": 5, "staged": 1, "draws": 10, "rejected": {"labelled": 9}}


def test_a_volume_with_one_position_ends(monkeypatch):
    monkeypatch.setattr(plan, "GIVE_UP", 50)
    calls = []
    samples, selection = select({"train": 4}, lambda sample: calls.append(sample), shape=(1, 256, 256),
                                rule=dataclasses.replace(RULE, split={**RULE.split, "fractions": {"train": 1.0}}))
    assert len(samples["train"]) == 1 and len(calls) == 1   # a repeat is rejected unread
    assert selection["train"] == {"wanted": 4, "staged": 1, "draws": 51, "rejected": {"repeat": 50}}


def test_a_larger_count_extends_a_smaller_under_a_keep():
    def keep(sample):   # depends on the sample alone, as a filter does
        return "content" if sample["starts"]["1"][0] % 3 == 0 else None

    few, many = select({"train": 6}, keep)[0]["train"], select({"train": 20}, keep)[0]["train"]
    assert len(few) == 6 and many[:6] == few


def test_a_split_without_a_plane_wants_none():
    samples, selection = select({"train": 4, "val": 4, "test": 4}, lambda sample: None, shape=(1, 2000, 2000))
    assert sum(len(v) for v in samples.values()) == 4
    assert sorted(split for split, counts in selection.items() if counts["wanted"]) == sorted(
        split for split, kept in samples.items() if kept)
