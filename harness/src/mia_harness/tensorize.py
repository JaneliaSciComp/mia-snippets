"""`harness tensorize`: a pyramid plus a crop descriptor → a Stack → a tensorizer → a checked Sample (buffers spec §4)."""
from pathlib import Path

import numpy as np

from .loader import load_snippet, withdrawn_names
from .pyramid import Pyramid, open_pyramid
from .tensor_api import LevelPatch, Sample, Stack, check_sample, recipe_channels


def _start(centre_nm: float, scale: float, shift: float, patch: int, n: int) -> int:
    if n < patch:
        raise ValueError(f"patch {patch} is larger than the level's extent {n}")
    return int(min(max(round((centre_nm - shift) / scale - patch / 2), 0), n - patch))


def _level_index(p: Pyramid, factor: int) -> int:
    for i, lv in enumerate(p.levels):
        if lv.factor == factor:
            return i
    raise ValueError(f"level {factor} is not in the pyramid (has {[lv.factor for lv in p.levels]})")


def _plane_index(p: Pyramid, level: int, plane: dict) -> dict:
    """z at this level from the descriptor's level-0 z, as the demo notebook does; other plane axes pass through."""
    out = {}
    if "z" in p.axes:
        zf = p.levels[level].scale_nm["z"] / p.levels[0].scale_nm["z"]
        nz = p.levels[level].shape[p.axes.index("z")]
        out["z"] = min(int(plane.get("z", 0) // zf), nz - 1)
    return out


def read_stack(pyramid_dir: Path, descriptor: dict) -> Stack:
    """The descriptor's patch at every requested level, co-centred on centre_nm, plus every label layer."""
    pyramid_dir = Path(pyramid_dir)
    raw = open_pyramid(pyramid_dir / "raw")
    labels = {d.name: open_pyramid(d) for d in sorted((pyramid_dir / "labels").glob("*")) if (d / "zarr.json").is_file()}
    patch, (cy, cx) = int(descriptor["patch"]), descriptor["centre_nm"]
    channel = descriptor.get("channel")
    levels = []
    for factor in descriptor["levels"]:
        i = _level_index(raw, factor)
        lv = raw.levels[i]
        ny, nx = lv.shape[raw.axes.index("y")], lv.shape[raw.axes.index("x")]
        y0 = _start(cy, lv.scale_nm["y"], lv.translation_nm["y"], patch, ny)
        x0 = _start(cx, lv.scale_nm["x"], lv.translation_nm["x"], patch, nx)
        box = {"y": slice(y0, y0 + patch), "x": slice(x0, x0 + patch)}
        plane = _plane_index(raw, i, descriptor.get("plane") or {})
        region = plane | box
        if "c" in raw.axes:
            region["c"] = slice(None) if channel is None else int(channel)
        img = np.asarray(raw.read(i, region))
        if img.ndim == 2:
            img = img[None]
        found = {}
        for key, lp in labels.items():
            j = _level_index(lp, factor)
            lregion = _plane_index(lp, j, descriptor.get("plane") or {}) | box
            if "c" in lp.axes:
                lregion["c"] = 0
            found[key] = np.asarray(lp.read(j, lregion))
        levels.append(LevelPatch(factor, img, found, (lv.scale_nm["y"], lv.scale_nm["x"]),
                                 (lv.translation_nm["y"], lv.translation_nm["x"]), (y0, x0)))
    meta = {"pyramid": str(pyramid_dir), "record": (raw.attrs.get("mia") or {}).get("record"), "descriptor": descriptor}
    return Stack(levels, patch, dict(descriptor.get("plane") or {}), (float(cy), float(cx)), meta)


def tensorize(pyramid_dir: Path, descriptor: dict, snapshot: Path, name: str, params: dict) -> Sample:
    """Run the named tensorizer from `snapshot` on the descriptor's stack and check its output against describe()."""
    mod = load_snippet(Path(snapshot), name, withdrawn_names(Path(snapshot)), params, kind="tensorizers")
    spec = mod.describe(params)
    stack = read_stack(pyramid_dir, descriptor)
    sample = mod.tensorize(stack, params)
    channels = recipe_channels(params.get("channels"), stack.levels[0].img.shape[0])
    check_sample(sample, spec, L=len(stack.levels), C=channels, P=stack.patch)
    return sample
