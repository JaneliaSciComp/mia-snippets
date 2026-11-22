"""The filters a plan rule may name: what a sample's full-resolution box must hold to be staged."""
from collections.abc import Callable
from typing import Literal

import numpy as np

from .png import content_ok


def labelled(labels: list[np.ndarray], *, min_fraction: float) -> bool:
    """Whether at least `min_fraction` of the box's pixels are annotated: nonzero in any of `labels`, the label
    boxes paired with the sample's raw, on any of their channels. A raw without labels passes."""
    if not labels:
        return True
    annotated = np.any([np.asarray(label).reshape(-1, *np.shape(label)[-2:]).any(axis=0) for label in labels], axis=0)
    return float(annotated.mean()) >= min_fraction


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
