import numpy as np
import pytest

from mia_harness.png import content_ok, encode_label, read_png, stats, stretch_raw, write_png

rng = np.random.default_rng(0)


@pytest.mark.parametrize("dt", [np.uint8, np.uint16, np.int16, np.float32])
def test_stretch_dtypes(dt):
    a = (rng.random((100, 100)) * 1000).astype(dt)
    out, m = stretch_raw(a)
    assert out.dtype == np.uint16 and out.shape == a.shape
    assert (out == stretch_raw(a)[0]).all()
    assert m["kind"] == "stretch" and m["source_dtype"] == str(np.dtype(dt)) and m["percentiles"] == [0.1, 99.9]
    assert m["lo"] == np.percentile(a, 0.1) and m["hi"] == np.percentile(a, 99.9)
    assert out.min() == 0 and out.max() == 65535
    assert m["nonfinite"] == {"nan": 0, "posinf": 0, "neginf": 0}


def test_stretch_exact_clip():
    a = np.arange(10000, dtype=np.float32).reshape(100, 100)
    out, m = stretch_raw(a)
    lo, hi = m["lo"], m["hi"]
    assert (a <= lo).sum() == 10 and (a >= hi).sum() == 10
    assert (out[a <= lo] == 0).all() and (out[a >= hi] == 65535).all()


def test_hot_pixel_does_not_darken():
    a = rng.random((100, 100)).astype(np.float32)
    ref = stretch_raw(a)[0].astype(float).mean()
    a[5, 5] = 1e9
    assert stretch_raw(a)[0].astype(float).mean() > 0.9 * ref


def test_nonfinite():
    a = rng.random((50, 50)).astype(np.float32)
    a[0, 0], a[0, 1], a[0, 2], a[0, 3] = np.nan, np.inf, -np.inf, np.nan
    out, m = stretch_raw(a)
    assert m["nonfinite"] == {"nan": 2, "posinf": 1, "neginf": 1}
    assert out[0, 0] == 0 and out[0, 3] == 0 and out[0, 2] == 0 and out[0, 1] == 65535


def test_stretch_constant_raises():
    with pytest.raises(ValueError):
        stretch_raw(np.zeros((8, 8), np.uint8))


def test_labels():
    a = np.array([[0, 255], [255, 0]], np.uint8)
    out, bd, m = encode_label(a, "binary")
    assert out.dtype == np.uint8 and out.tolist() == [[0, 1], [1, 0]] and bd == 8
    out, bd, m = encode_label(np.array([[0, 300]], np.int32), "instance")
    assert out.dtype == np.uint16 and bd == 16 and out.tolist() == [[0, 300]] and m["kind"] == "identity"
    big = np.array([[0, 70000, 5, 70000, 9]], np.int64)
    out, bd, m = encode_label(big, "instance")
    assert out.tolist() == [[0, 3, 1, 3, 2]] and m["kind"] == "renumbered"
    assert encode_label(np.array([[3, 7]], np.int64), "semantic")[0].dtype == np.uint16
    with pytest.raises(ValueError):
        encode_label(np.array([[70000]], np.int64), "semantic")
    with pytest.raises(ValueError):
        encode_label(np.array([[-1]], np.int16), "semantic")


def test_roundtrip(tmp_path):
    for i, a in enumerate([stretch_raw(rng.random((64, 64)).astype(np.float32))[0],
                           encode_label(np.eye(64, dtype=np.uint8) * 255, "binary")[0],
                           encode_label(np.eye(64, dtype=np.int32) * 40000, "instance")[0]]):

        bd = 8 if a.dtype == np.uint8 else 16
        write_png(tmp_path / f"{i}.png", a, bd)
        b, bd2 = read_png(tmp_path / f"{i}.png")
        assert bd2 == bd and b.dtype == a.dtype and (a == b).all()
    rgb = rng.integers(0, 255, (16, 16, 3), dtype=np.uint8)
    write_png(tmp_path / "rgb.png", rgb, 8)
    b, bd = read_png(tmp_path / "rgb.png")
    assert bd == 8 and (b == rgb).all()


def test_stats():
    a = np.arange(101, dtype=np.float32)
    a[0] = np.nan
    s = stats(a)
    assert s["min"] == 1 and s["max"] == 100 and set(s) == {"min", "p1", "p50", "p99", "max"}


def test_content_ok():
    assert content_ok(rng.random((64, 64)).astype(np.float32))
    assert not content_ok(np.full((64, 64), 7, np.uint8))
    z = np.zeros(1000, np.uint8); z[:40] = np.arange(40) + 1
    assert not content_ok(z)                      # 96% zeros
    n = rng.random(1000).astype(np.float32); n[:100] = np.nan
    assert not content_ok(n)                      # 10% NaN
