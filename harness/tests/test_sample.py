from collections import Counter
from itertools import islice

from mia_harness.sample import CropSpec, Volume, candidates, capacity, collect, effective_n, overlap_frac, positions

V = Volume(0, "zyx", (2, 600, 600), "chunk")


def take(vols, seed, n, **kw):
    return list(islice(candidates(vols, seed, **kw), n))


def test_seed_determinism():
    assert take([V], "a", 50) == take([V], "a", 50)
    assert take([V], "a", 50) != take([V], "b", 50)


def test_candidates_in_bounds():
    for c in take([V], "a", 200):
        assert set(c.plane) == {"z"} and 0 <= c.plane["z"] < 2
        assert 0 <= c.y <= 600 - 256 and 0 <= c.x <= 600 - 256


def test_weighting():
    small, big = Volume(0, "yx", (355, 256), "chunk"), Volume(1, "yx", (1255, 256), "chunk")
    assert positions(big, 256) == 10 * positions(small, 256)
    got = Counter(c.raw for c in take([small, big], "w", 2000))
    assert 7 < got[1] / got[0] < 14


def test_per_plane_grouping():
    vol = Volume(0, "czyx", (3, 4, 600, 600), "plane")
    cs = take([vol], "g", 400, per_plane=4)
    for i in range(0, 400, 4):
        assert len({tuple(c.plane.items()) for c in cs[i:i + 4]}) == 1
    assert all(set(c.plane) == {"c", "z"} for c in cs)


def test_per_file_grouping():
    a, b = Volume(0, "yx", (600, 600), "file"), Volume(1, "yx", (600, 600), "file")
    cs = take([a, b], "f", 80, per_file=8)
    for i in range(0, 80, 8):
        assert len({c.raw for c in cs[i:i + 8]}) == 1


def test_capacity_and_effective_n():
    assert capacity([V], 256) == 8
    assert effective_n(16, 8) == 4 and effective_n(2, 8) == 2


def test_shared_budget_counts_by_kind():
    seen = []
    def reject(c, accepted):
        seen.append(c)
        return "content" if len(seen) % 2 else "overlap"
    acc, rej = collect(candidates([V], "s"), 4, reject)
    assert acc == [] and len(seen) == 40 and rej == {"content": 20, "overlap": 20}


def test_collect_accepts_up_to_n():
    acc, rej = collect(candidates([V], "s"), 3, lambda c, a: None)
    assert len(acc) == 3 and not rej


def test_overlap():
    a = {"raw": 0, "plane": {"z": 0}, "y": 0, "x": 0}
    def at(x, **kw): return {"raw": 0, "plane": {"z": 0}, "y": 0, "x": x} | kw
    assert abs(overlap_frac(a, at(179), 256) - 77 / 256) < 1e-9   # ~30%
    assert overlap_frac(a, at(205), 256) < 0.25 and abs(overlap_frac(a, at(205), 256) - 51 / 256) < 1e-9
    assert overlap_frac(a, at(0, raw=1), 256) == 0
    assert overlap_frac(a, at(0, plane={"z": 1}), 256) == 0
    assert overlap_frac(a, at(300), 256) == 0
    assert overlap_frac(a, at(0), 256) == 1


def test_cropspec_is_hashable():
    assert CropSpec(0, {}, 0, 0).size == 256
