import json
from pathlib import Path

import numpy as np
import pytest
import zarr
from test_pyramid import write_sparse
from test_smoke import SNAP, write
from rangeserver import server  # noqa: F401  (fixture)
from test_stage import VOXEL, plan_input, served

from mia_harness import cli
from mia_harness.cache import DatasetCache
from mia_harness.io import IO
from mia_harness.loader import load_snippet
from mia_harness.pyramid import NotStaged, block_mean, open_pyramid
from mia_harness.stage import stage
from mia_harness.tensor_api import bbox_of, check_sample
from mia_harness.tensorize import read_stack, tensorize

TENSORIZERS = Path(__file__).resolve().parents[2]      # the repo: tensorizers/<name>/ + WITHDRAWN.yaml
TEST_TENSORIZERS = SNAP                                 # tests/: tensorizers/bad-shape + WITHDRAWN.yaml
D = {"plane": {"z": 1}, "centre_nm": [3000.0, 3500.0], "levels": [1, 2, 4], "patch": 64, "channel": None}


@pytest.fixture
def staged(tmp_path, server):
    b, r = write(tmp_path, (3, 600, 700), (3, 600, 700))
    served(server, b)
    io = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30, {"127.0.0.1"})
    try:
        rep = stage(b, SNAP, r, io, plan=plan_input((1, 2, 4), 64, {"train": 4, "val": 1, "test": 1}), voxel=VOXEL,
                    out=tmp_path / "out")
    finally:
        io.close()
    assert rep["ok"], rep["errors"]
    return tmp_path / "out" / "v0.zarr"


@pytest.fixture
def planned(staged):
    """A descriptor for the first planned train sample of `staged`: the stage only wrote planned samples."""
    sample = zarr.open_group(staged / "raw", mode="r").attrs["mia"]["plan"]["samples"]["train"][0]
    return {**sample, "channel": None}


@pytest.fixture
def sparse_volume(tmp_path):
    return tmp_path / "v0.zarr", write_sparse(tmp_path / "v0.zarr")


@pytest.fixture
def staged_sparse(sparse_volume):
    return sparse_volume[0]


@pytest.fixture
def source(sparse_volume):
    return sparse_volume[1]


def test_stack_from_a_sparse_pyramid_matches_the_source(staged_sparse, source):
    s = read_stack(staged_sparse, D)
    p = open_pyramid(staged_sparse / "raw")
    for i, lv in enumerate(s.levels):
        region = p.locate(i, tuple(D["centre_nm"]), D["patch"], D["plane"])
        box = p.raw_index_box(i, region)
        fresh = source[1, box["y"][0]:box["y"][1], box["x"][0]:box["x"][1]]
        np.testing.assert_array_equal(lv.img[0], block_mean(fresh, lv.factor))
        assert lv.labels["gt"].shape == (64, 64)


def test_unplanned_descriptor_raises_not_staged(staged_sparse):
    with pytest.raises(NotStaged):
        read_stack(staged_sparse, {**D, "centre_nm": [5950.0, 6950.0]})


def test_stack_is_co_centred_and_bbox_is_in_nm(staged, planned):
    s = read_stack(staged, planned)
    p = open_pyramid(staged / "raw")
    for lv, info in zip(s.levels, p.levels):
        start = planned["starts"][str(lv.factor)]     # in the raw level grid; the array holds the box from its origin
        assert lv.start_px == (start[0] - info.origin["y"] // lv.factor, start[1] - info.origin["x"] // lv.factor)
    # bbox lands in the raw frame: raw-grid start × 16 nm + the half-pixel shift of factor 2
    y1, x1 = planned["starts"]["2"]
    np.testing.assert_allclose(bbox_of(s.levels[1], 64), [[y1 * 16 + 4, x1 * 16 + 4], [(y1 + 63) * 16 + 4, (x1 + 63) * 16 + 4]])
    assert s.levels[0].img.shape == (1, 64, 64) and s.levels[0].labels["gt"].shape == (64, 64)
    origin = p.levels[2].origin
    assert s.levels[2].scale_nm == (32.0, 32.0) and s.levels[2].translation_nm == ((origin["y"] + 1.5) * 8, (origin["x"] + 1.5) * 8)
    assert s.levels[0].img.dtype == np.uint16


def test_unplanned_edge_centres_are_not_staged(staged, planned):
    """Samples are drawn at least a coarsest half footprint from the edges, so the clamped edge patches were never
    written: the reader refuses them instead of serving fill values."""
    for centre in ([0.0, 0.0], [1e9, 1e9]):
        with pytest.raises(NotStaged):
            read_stack(staged, {**planned, "centre_nm": centre})


def test_muvit_2d_sample(staged, planned):
    out = tensorize(staged, planned, TENSORIZERS, "muvit-2d", {})
    assert out["img"].shape == (3, 1, 64, 64) and out["img"].dtype == np.float32
    assert 0.0 <= float(out["img"].min()) and float(out["img"].max()) <= 1.0
    assert out["bbox"].shape == (3, 2, 2) and out["bbox"].dtype == np.float32
    assert out["label/gt"].dtype == np.int32 and out["label/gt"].shape == (3, 64, 64)
    assert out["pixel_size"].tolist() == [[8, 8], [16, 16], [32, 32]]
    assert out["meta"]["levels"] == [1, 2, 4] and out["meta"]["plane"] == planned["plane"]
    none = tensorize(staged, planned, TENSORIZERS, "muvit-2d", {"normalize": "none"})
    np.testing.assert_allclose(none["img"][0, 0], read_stack(staged, planned).levels[0].img[0] / 65535.0, rtol=1e-6)


def test_single_channel_descriptor(staged, planned):
    out = tensorize(staged, {**planned, "channel": 0}, TENSORIZERS, "muvit-2d", {})
    assert out["img"].shape == (3, 1, 64, 64)


def test_bad_tensorizer_fails_contract(staged, planned):
    with pytest.raises(ValueError, match=r"img: expected shape \(3, 1, 64, 64\)"):
        tensorize(staged, planned, TEST_TENSORIZERS, "bad-shape", {})


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


def test_tensorize_cli(staged, planned, tmp_path):
    out = tmp_path / "sample.npz"
    code = cli.main(["tensorize", str(staged), "--tensorizers", str(TENSORIZERS), "--name", "muvit-2d",
                     "--descriptor", json.dumps(planned), "--out", str(out)])
    assert code == 0
    with np.load(out) as z:
        assert z["img"].shape == (3, 1, 64, 64) and z["label/gt"].shape == (3, 64, 64)
    assert json.loads(out.with_suffix(".json").read_text())["levels"] == [1, 2, 4]
    assert cli.main(["tensorize", str(staged), "--tensorizers", str(TEST_TENSORIZERS), "--name", "bad-shape",
                     "--descriptor", json.dumps(planned), "--out", str(tmp_path / "bad.npz")]) == 1


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


def test_channel_recipe_in_tensorize(staged, planned):
    plain = tensorize(staged, planned, TENSORIZERS, "muvit-2d", {})
    rep = tensorize(staged, planned, TENSORIZERS, "muvit-2d", {"channels": {"op": "replicate"}})
    assert rep["img"].shape == (3, 3, 64, 64) and rep["meta"]["channels"] == {"op": "replicate"}
    for c in range(3):
        np.testing.assert_array_equal(rep["img"][:, c], plain["img"][:, 0])
    add = tensorize(staged, planned, TENSORIZERS, "muvit-2d", {"channels": {"op": "additive"}})
    assert add["img"].shape == (3, 3, 64, 64) and add["img"][:, 1:].max() == 0.0


def test_recipe_channels():
    from mia_harness.tensor_api import recipe_channels

    assert recipe_channels(None, 4) == 4 and recipe_channels({"op": "mean"}, 4) == 1
    assert recipe_channels({"op": "additive"}, 2) == 3
    with pytest.raises(ValueError, match="unknown channel recipe op 'nope'"):
        recipe_channels({"op": "nope"}, 1)


def test_stats_bounds_replace_patch_percentiles():
    from mia_harness.tensor_api import LevelPatch, Stack

    img = np.full((2, 8, 8), 100, dtype=np.uint16)
    img[1] = 50
    lv = LevelPatch(1, img, {}, (10.0, 10.0), (0.0, 0.0), (0, 0))
    stack = Stack([lv], 8, {"z": 0}, (40.0, 40.0))
    mod = load_snippet(TENSORIZERS, "muvit-2d", {}, {}, kind="tensorizers")
    sample = mod.tensorize(stack, {"stats": {"lo": [0, 0], "hi": [200, 100]}, "scope": "volume"})
    assert np.allclose(sample["img"][0, 0], 0.5) and np.allclose(sample["img"][0, 1], 0.5)
    assert sample["meta"]["normalize_scope"] == "volume"
    patch_only = mod.tensorize(stack, {})
    assert np.allclose(patch_only["img"], 0.0) and patch_only["meta"]["normalize_scope"] == "patch"
    with pytest.raises(ValueError, match="channels"):
        mod.tensorize(stack, {"stats": {"lo": [0], "hi": [200]}})


def test_stats_and_scope_pass_the_loader_param_check(staged, planned):
    """The client sends `stats` and `scope` as params: tensorizer.yaml must declare them or load_snippet refuses."""
    out = tensorize(staged, planned, TENSORIZERS, "muvit-2d", {"stats": {"lo": [0], "hi": [65535]}, "scope": "volume"})
    assert out["meta"]["normalize_scope"] == "volume"
    np.testing.assert_allclose(out["img"][0, 0], read_stack(staged, planned).levels[0].img[0] / 65535.0, rtol=1e-6)
    none = tensorize(staged, planned, TENSORIZERS, "muvit-2d", {"stats": None, "scope": "patch"})  # a patch-scope descriptor
    assert none["meta"]["normalize_scope"] == "patch"
