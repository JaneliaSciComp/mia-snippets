"""MuViT 2D sample from a level stack: img (L, C, P, P) float32 in [0, 1], bbox (L, 2, 2) nm, labels (L, P, P) int32."""
import numpy as np

from mia_harness.tensor_api import Sample, Stack, TensorSpec, bbox_of

SPEC_VERSION = 1
EPS = 1e-6


def describe(params: dict) -> TensorSpec:
    return TensorSpec({
        "img": (("L", "C", "P", "P"), "float32"),
        "bbox": (("L", 2, 2), "float32"),
        "label/*": (("L", "P", "P"), "int32"),
        "pixel_size": (("L", 2), "float32"),
    })


def _normalise(img: np.ndarray, params: dict) -> np.ndarray:
    method = params.get("normalize", "percentile")
    x = img.astype(np.float32)
    if method == "none":
        if np.issubdtype(img.dtype, np.integer):
            x = x / np.iinfo(img.dtype).max
        return x
    if method != "percentile":
        raise ValueError(f"normalize must be percentile or none, got {method!r}")
    lo, hi = np.percentile(x, params.get("range", [1, 99]), axis=(1, 2), keepdims=True)
    x = (x - lo) / np.maximum(hi - lo, EPS)
    return np.clip(x, 0.0, 1.0) if params.get("clip", True) else x


LUMINANCE = np.array([0.299, 0.587, 0.114], dtype=np.float32)[:, None, None]


def apply_recipe(img: np.ndarray, recipe: dict | None) -> np.ndarray:
    """One level's normalised (C, P, P) → the channels a plan's channel recipe asks for; None keeps them all."""
    if not recipe:
        return img
    op, count = recipe.get("op"), img.shape[0]
    if op == "channel":
        index = int(recipe.get("index", 0))
        if not 0 <= index < count:
            raise ValueError(f"channel {index} out of range for {count} channels")
        return img[index : index + 1]
    if op == "mean":
        return img.mean(axis=0, keepdims=True)
    if op == "replicate":
        return np.repeat(img[:1], 3, axis=0)
    if op in ("luminance", "rgb"):
        if count < 3:
            raise ValueError(f"{op} needs 3 channels, got {count}")
        return (img[:3] * LUMINANCE).sum(axis=0, keepdims=True) if op == "luminance" else img[:3]
    if op == "additive":  # channel i lights colour i mod 3; sum and clip
        out = np.zeros((3, *img.shape[1:]), dtype=img.dtype)
        for i in range(count):
            out[i % 3] += img[i]
        return np.clip(out, 0.0, 1.0)
    raise ValueError(f"unknown channel recipe op {op!r}")


def tensorize(stack: Stack, params: dict) -> Sample:
    levels = stack.levels
    recipe = params.get("channels")
    sample: Sample = {
        "img": np.stack([apply_recipe(_normalise(lv.img, params), recipe) for lv in levels]).astype(np.float32),
        "bbox": np.stack([bbox_of(lv, stack.patch) for lv in levels]).astype(np.float32),
        "pixel_size": np.asarray([lv.scale_nm for lv in levels], dtype=np.float32),
    }
    for key in levels[0].labels:
        if all(key in lv.labels for lv in levels):
            sample[f"label/{key}"] = np.stack([lv.labels[key] for lv in levels]).astype(np.int32)
    sample["meta"] = {**stack.meta, "levels": [lv.factor for lv in levels], "patch": stack.patch,
                      "plane": stack.plane, "centre_nm": list(stack.centre_nm),
                      "normalize": params.get("normalize", "percentile"), "channels": recipe}
    return sample
