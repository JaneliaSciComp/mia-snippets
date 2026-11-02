from mia_harness.plan import PlanRule, capacity, derive_samples, split_planes, start_index

RULE = PlanRule(seed=0, patch=64, levels=(1, 2, 4), samples={"train": 10, "val": 3, "test": 3},
                split={"policy": "fractions", "fractions": {"train": 0.6, "val": 0.2, "test": 0.2}, "seed": 0},
                channel_recipe=None)
VOX = {"z": 300.0, "y": 10.0, "x": 10.0}


def test_digest_is_stable_and_record_local():
    assert RULE.digest() == PlanRule(**{**RULE.__dict__}).digest()
    assert RULE.digest() != PlanRule(**{**RULE.__dict__, "seed": 1}).digest()


def test_split_planes_golden():   # part B asserts the same numbers in tests/test_buffers_register.py
    assert split_planes("r/v0", 10, RULE.split) == {"train": [0, 1, 2, 4, 5, 9], "val": [6, 8], "test": [3, 7]}


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
        assert set(s["starts"]) == {"1", "2", "4"} and s["plane"]["z"] in split_planes("r/v0", 10, RULE.split)["train"]


def test_capacity_caps_and_eval_rejects_overlap():
    assert capacity(600, 700, 10, 64, (1, 2, 4)) == 2 * 2 * 10
    tiny = derive_samples(RULE, "r", 0, "yx", (256, 256), {"y": 10.0, "x": 10.0})
    assert sum(len(v) for v in tiny.values()) == 1   # one plane, one position: one sample in one split
    evals = derive_samples(RULE, "r", 0, "zyx", (10, 600, 700), VOX)["val"]
    assert len({(s["plane"]["z"], tuple(s["starts"]["4"])) for s in evals}) == len(evals)


def test_2d_volume_has_no_plane_key():
    assert derive_samples(RULE, "r", 0, "yx", (600, 700), {"y": 10.0, "x": 10.0})["train"][0]["plane"] == {}
