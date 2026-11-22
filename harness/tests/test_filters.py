import numpy as np
import pytest

from mia_harness.filters import content, labelled, ordered


def box(annotated: int, shape=(20, 20)) -> np.ndarray:
    """A label box whose first `annotated` pixels are nonzero."""
    flat = np.zeros(int(np.prod(shape)), np.uint16)
    flat[:annotated] = 7
    return flat.reshape(shape)


def test_content_rejects_a_constant_patch_and_keeps_noise():
    assert not content(np.full((3, 64, 64), 255, np.uint8))    # blank slide, every channel full on
    assert content(np.random.default_rng(0).integers(0, 255, (3, 64, 64), dtype=np.uint8))


def test_labelled_threshold_is_inclusive():
    assert labelled([box(20)], min_fraction=0.05)        # 20 of 400 is exactly 5 %
    assert not labelled([box(19)], min_fraction=0.05)
    assert labelled([box(400)], min_fraction=1.0) and not labelled([box(399)], min_fraction=1.0)


def test_labelled_is_the_union_over_labels_and_channels():
    first, second = box(10), box(10)[::-1, ::-1].copy()   # 10 pixels each, at opposite corners
    assert not labelled([first], min_fraction=0.05)
    assert labelled([first, second], min_fraction=0.05)                  # two labels of one raw
    assert labelled([np.stack([first, second])], min_fraction=0.05)      # one label with a channel axis
    assert not labelled([np.stack([first, first])], min_fraction=0.05)   # the same pixels count once


def test_labelled_passes_a_volume_without_labels():
    assert labelled([], min_fraction=0.9)


def test_ordered_reads_labels_first_and_refuses_an_unknown_name():
    rule = ({"name": "content"}, {"name": "labelled", "min_fraction": 0.05})   # name order, as the rule has them
    assert [(name, reads, parameters) for name, _, reads, parameters in ordered(rule)] == [
        ("labelled", "label", {"min_fraction": 0.05}), ("content", "raw", {})]
    assert ordered(()) == []
    with pytest.raises(ValueError, match="unknown filter 'sharp'"):
        ordered(({"name": "sharp"},))
