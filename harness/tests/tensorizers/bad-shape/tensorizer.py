"""TEST ONLY: describes a MuViT sample but returns img with one level missing."""
import numpy as np

from mia_harness.tensor_api import Sample, Stack, TensorSpec

SPEC_VERSION = 1


def describe(params: dict) -> TensorSpec:
    return TensorSpec({"img": (("L", "C", "P", "P"), "float32"), "bbox": (("L", 2, 2), "float32")})


def tensorize(stack: Stack, params: dict) -> Sample:
    img = np.stack([lv.img for lv in stack.levels[1:]]).astype(np.float32)
    return {"img": img, "bbox": np.zeros((len(stack.levels), 2, 2), np.float32), "meta": {}}
