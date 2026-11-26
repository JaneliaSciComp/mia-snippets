"""The filters a plan rule may name: what a sample must hold to be staged. A raw filter is given the sample's
full-resolution box, a label filter the labels over its whole footprint (`stage.keeper`)."""
from collections.abc import Callable
from typing import Literal

import numpy as np

from .png import content_ok


def labelled(labels: list[np.ndarray]) -> bool:
    """Whether the crop holds any annotation: a nonzero pixel, a single point being enough, in one of `labels`, the
    footprints of the labels paired with the sample's raw. A raw without labels holds none."""
    return any(np.asarray(label).any() for label in labels)


def content(raw: np.ndarray) -> bool:
    """Whether the raw box holds an image: the crop job's test, over every channel together."""
    return content_ok(raw)


# name → (the test, what it reads), in the order a sample is tested: a label box is read before the raw's, so a
# sample without annotation costs no raw read
FILTERS: dict[str, tuple[Callable[..., bool], Literal["label", "raw"]]] = {
    "labelled": (labelled, "label"),
    "content": (content, "raw"),
}


def ordered(filters: tuple[dict, ...]) -> list[tuple[str, Callable[..., bool], str, dict]]:
    """A rule's filters in testing order as (name, test, what it reads, parameters); an unknown name is an error."""
    named = {entry["name"]: {key: value for key, value in entry.items() if key != "name"} for entry in filters}
    for name in named:
        if name not in FILTERS:
            raise ValueError(f"unknown filter {name!r}")
    return [(name, *FILTERS[name], named[name]) for name in FILTERS if name in named]
