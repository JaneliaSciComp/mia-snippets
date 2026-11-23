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


def test_labelled_needs_one_annotated_pixel():
    assert labelled([box(1)]) and labelled([box(400)])    # a single point is annotation
    assert not labelled([box(0)])


def test_labelled_is_the_union_over_labels_and_channels():
    assert labelled([box(0), box(1)])                     # two labels of one raw
    assert labelled([np.stack([box(0), box(1)])])         # one label with a channel axis
    assert not labelled([box(0), np.stack([box(0), box(0)])])


def test_labelled_rejects_a_volume_without_labels():
    assert not labelled([])


def test_ordered_reads_labels_first_and_refuses_an_unknown_name():
    rule = ({"name": "content"}, {"name": "labelled"})   # name order, as the rule has them
    assert [(name, reads, parameters) for name, _, reads, parameters in ordered(rule)] == [
        ("labelled", "label", {}), ("content", "raw", {})]
    assert ordered(()) == []
    with pytest.raises(ValueError, match="unknown filter 'sharp'"):
        ordered(({"name": "sharp"},))
