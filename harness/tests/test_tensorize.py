import json
from pathlib import Path

import numpy as np
import pytest
from test_smoke import SNAP, write

from mia_harness import cli
from mia_harness.cache import DatasetCache
from mia_harness.io import IO
from mia_harness.loader import load_snippet
from mia_harness.stage import stage
from mia_harness.tensor_api import bbox_of, check_sample
from mia_harness.tensorize import read_stack, tensorize

TENSORIZERS = Path(__file__).resolve().parents[2]      # the repo: tensorizers/<name>/ + WITHDRAWN.yaml
TEST_TENSORIZERS = SNAP                                 # tests/: tensorizers/bad-shape + WITHDRAWN.yaml
D = {"plane": {"z": 1}, "centre_nm": [3000.0, 3500.0], "levels": [1, 2, 4], "patch": 64, "channel": None}


@pytest.fixture
def staged(tmp_path):
    b, r = write(tmp_path, (3, 600, 700), (3, 600, 700))
    io = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30, set())
    try:
        rep = stage(b, SNAP, r, io, levels=[1, 2, 4], patch=64, out=tmp_path / "out", voxel_size_nm={"z": 300.0, "y": 10.0, "x": 10.0})
    finally:
        io.close()
    assert rep["ok"], rep["errors"]
    return tmp_path / "out" / "v0.zarr"


def test_stack_is_co_centred_and_bbox_is_in_nm(staged):
    s = read_stack(staged, D)
    assert [lv.start_px for lv in s.levels] == [(268, 318), (118, 143), (43, 55)]
    np.testing.assert_allclose(bbox_of(s.levels[1], 64), [[118 * 20 + 5, 143 * 20 + 5], [(118 + 63) * 20 + 5, (143 + 63) * 20 + 5]])
    assert s.levels[0].img.shape == (1, 64, 64) and s.levels[0].labels["gt"].shape == (64, 64)
    assert s.levels[2].scale_nm == (40.0, 40.0) and s.levels[2].translation_nm == (15.0, 15.0)
    # the coarse patch really is the mean of the fine data at the same place
    fine = s.levels[0].img[0]
    assert s.levels[0].img.dtype == np.uint16 and fine.shape == (64, 64)


def test_edge_centre_clamps(staged):
    low = read_stack(staged, {**D, "centre_nm": [0.0, 0.0]})
    assert [lv.start_px for lv in low.levels] == [(0, 0)] * 3
    high = read_stack(staged, {**D, "centre_nm": [1e9, 1e9]})
    assert [lv.start_px for lv in high.levels] == [(536, 636), (236, 286), (86, 111)]
    np.testing.assert_allclose(bbox_of(high.levels[0], 64)[0], [5360.0, 6360.0])


def test_muvit_2d_sample(staged):
    out = tensorize(staged, D, TENSORIZERS, "muvit-2d", {})
    assert out["img"].shape == (3, 1, 64, 64) and out["img"].dtype == np.float32
    assert 0.0 <= float(out["img"].min()) and float(out["img"].max()) <= 1.0
    assert out["bbox"].shape == (3, 2, 2) and out["bbox"].dtype == np.float32
    assert out["label/gt"].dtype == np.int32 and out["label/gt"].shape == (3, 64, 64)
    assert out["pixel_size"].tolist() == [[10, 10], [20, 20], [40, 40]]
    assert out["meta"]["levels"] == [1, 2, 4] and out["meta"]["plane"] == {"z": 1}
    none = tensorize(staged, D, TENSORIZERS, "muvit-2d", {"normalize": "none"})
    np.testing.assert_allclose(none["img"][0, 0], read_stack(staged, D).levels[0].img[0] / 65535.0, rtol=1e-6)


def test_single_channel_descriptor(staged):
    out = tensorize(staged, {**D, "channel": 0}, TENSORIZERS, "muvit-2d", {})
    assert out["img"].shape == (3, 1, 64, 64)


def test_bad_tensorizer_fails_contract(staged):
    with pytest.raises(ValueError, match=r"img: expected shape \(3, 1, 64, 64\)"):
        tensorize(staged, D, TEST_TENSORIZERS, "bad-shape", {})


def test_check_sample_rejects_wrong_dtype_and_unknown_key():
    spec = load_snippet(TENSORIZERS, "muvit-2d", set(), {}, kind="tensorizers").describe({})
    good = {"img": np.zeros((2, 1, 8, 8), np.float32), "bbox": np.zeros((2, 2, 2), np.float32),
            "pixel_size": np.zeros((2, 2), np.float32), "meta": {}}
    check_sample(good, spec, L=2, C=1, P=8)
    with pytest.raises(ValueError, match="bbox: expected"):
        check_sample({**good, "bbox": np.zeros((2, 2, 2), np.float64)}, spec, L=2, C=1, P=8)
    with pytest.raises(ValueError, match="extra: not described"):
        check_sample({**good, "extra": np.zeros(1)}, spec, L=2, C=1, P=8)
    with pytest.raises(ValueError, match="missing"):
        check_sample({k: v for k, v in good.items() if k != "bbox"}, spec, L=2, C=1, P=8)


def test_loader_kind_tensorizers():
    mod = load_snippet(TENSORIZERS, "muvit-2d", set(), {}, kind="tensorizers")
    assert callable(mod.tensorize) and callable(mod.describe)
    with pytest.raises(ValueError):
        load_snippet(TENSORIZERS, "muvit-2d", set(), {})          # not a snippet
    with pytest.raises(ValueError, match="not declared"):
        load_snippet(TENSORIZERS, "muvit-2d", set(), {"bogus": 1}, kind="tensorizers")


def test_tensorize_cli(staged, tmp_path):
    out = tmp_path / "sample.npz"
    code = cli.main(["tensorize", str(staged), "--tensorizers", str(TENSORIZERS), "--name", "muvit-2d",
                     "--descriptor", json.dumps(D), "--out", str(out)])
    assert code == 0
    with np.load(out) as z:
        assert z["img"].shape == (3, 1, 64, 64) and z["label/gt"].shape == (3, 64, 64)
    assert json.loads(out.with_suffix(".json").read_text())["levels"] == [1, 2, 4]
    assert cli.main(["tensorize", str(staged), "--tensorizers", str(TEST_TENSORIZERS), "--name", "bad-shape",
                     "--descriptor", json.dumps(D), "--out", str(tmp_path / "bad.npz")]) == 1


def _muvit():
    import importlib.util

    path = TENSORIZERS / "tensorizers" / "muvit-2d" / "tensorizer.py"
    spec = importlib.util.spec_from_file_location("muvit_2d_tensorizer", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_channel_recipe_ops():
    apply = _muvit().apply_recipe
    rgb = np.stack([np.full((4, 4), v, np.float32) for v in (1.0, 0.5, 0.0)])
    assert apply(rgb, None) is rgb
    np.testing.assert_allclose(apply(rgb, {"op": "luminance"}), np.full((1, 4, 4), 0.299 + 0.5 * 0.587))
    np.testing.assert_allclose(apply(rgb, {"op": "mean"}), np.full((1, 4, 4), 0.5))
    assert apply(rgb, {"op": "rgb"}).shape == (3, 4, 4) and apply(rgb, {"op": "channel", "index": 2}).max() == 0.0
    five = np.stack([np.full((2, 2), 0.4, np.float32)] * 5)
    add = apply(five, {"op": "additive"})  # channels 0,3 -> R, 1,4 -> G, 2 -> B, clipped
    np.testing.assert_allclose(add[:, 0, 0], [0.8, 0.8, 0.4])
    with pytest.raises(ValueError, match="luminance needs 3 channels, got 1"):
        apply(rgb[:1], {"op": "luminance"})
    with pytest.raises(ValueError, match="channel 3 out of range for 3 channels"):
        apply(rgb, {"op": "channel", "index": 3})
    with pytest.raises(ValueError, match="unknown channel recipe op 'swizzle'"):
        apply(rgb, {"op": "swizzle"})


def test_channel_recipe_in_tensorize(staged):
    plain = tensorize(staged, D, TENSORIZERS, "muvit-2d", {})
    rep = tensorize(staged, D, TENSORIZERS, "muvit-2d", {"channels": {"op": "replicate"}})
    assert rep["img"].shape == (3, 3, 64, 64) and rep["meta"]["channels"] == {"op": "replicate"}
    for c in range(3):
        np.testing.assert_array_equal(rep["img"][:, c], plain["img"][:, 0])
    add = tensorize(staged, D, TENSORIZERS, "muvit-2d", {"channels": {"op": "additive"}})
    assert add["img"].shape == (3, 3, 64, 64) and add["img"][:, 1:].max() == 0.0


def test_recipe_channels():
    from mia_harness.tensor_api import recipe_channels

    assert recipe_channels(None, 4) == 4 and recipe_channels({"op": "mean"}, 4) == 1
    assert recipe_channels({"op": "additive"}, 2) == 3
    with pytest.raises(ValueError, match="unknown channel recipe op 'nope'"):
        recipe_channels({"op": "nope"}, 1)
