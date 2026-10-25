"""`harness stage`: a binding's arrays → one verified NGFF pyramid per raw volume, labels alongside (buffers spec §3)."""
import json
from pathlib import Path

import numpy as np

from .api import Array
from .binding import binding_hash, load_binding
from .io import IO, BudgetExceeded, HostNotAllowed
from .pyramid import block_mean, open_pyramid, stride, write_pyramid
from .run import open_arrays

S0_CHECKS = 8       # random s0 chunks re-read through the snippet
LEVEL_CHECKS = 4    # random blocks per coarser level recomputed from s0
SAMPLE_BOXES = 8    # random s0 patch boxes per channel that feed the intensity sample
SAMPLE_KEEP = 4096  # values kept per channel (sorted) in stage.json's intensity_sample


def _random_box(rng: np.random.Generator, size: dict[str, int], level_shape: tuple[int, ...], edge: int,
                fixed: dict | None = None) -> tuple[dict, int, int, int]:
    """A random plane over the c/z axes (`fixed` pins some of them) and a random y/x box of side ≤ edge."""
    plane = {a: int(rng.integers(size[a])) for a in size if a in "cz"}
    plane.update(fixed or {})
    ny, nx = level_shape[-2], level_shape[-1]
    n = min(edge, ny, nx)
    y0 = int(rng.integers(0, max(1, ny - n + 1)))
    x0 = int(rng.integers(0, max(1, nx - n + 1)))
    return plane, y0, x0, n


def intensity_sample(p, patch: int, rng: np.random.Generator) -> list[list[float]]:
    """Per channel, up to SAMPLE_KEEP level-0 values from SAMPLE_BOXES random patch boxes, sorted: what
    registration turns into normalisation bounds for `normalize.scope: volume|dataset` (buffers spec §3).
    `p` is an open Pyramid (axes, levels[0].shape, read)."""
    size = dict(zip(p.axes, p.levels[0].shape))
    out = []
    for c in range(size.get("c", 1)):
        values = []
        for _ in range(SAMPLE_BOXES):
            plane, y0, x0, n = _random_box(rng, size, p.levels[0].shape, patch, {"c": c} if "c" in size else None)
            values.append(np.asarray(p.read(0, plane | {"y": slice(y0, y0 + n), "x": slice(x0, x0 + n)})).ravel())
        pooled = np.concatenate(values)
        if pooled.size > SAMPLE_KEEP:
            pooled = rng.choice(pooled, SAMPLE_KEEP, replace=False)
        out.append(np.sort(pooled).tolist())
    return out


def _voxel_size(array: Array, fallback: dict[str, float] | None) -> dict[str, float] | None:
    """Per axis: what the file says, else the fallback from the caller (the record's value; mia-buffers adds 1 nm for
    the plane axis a 2D stack keeps). None when an axis has neither: the caller decides, not a guess."""
    spatial = [a for a in array.axes if a != "c"]
    merged = {**(fallback or {}), **(array.voxel_size_nm or {})}
    if all(a in merged for a in spatial):
        return {a: float(merged[a]) for a in spatial}
    return None


def _verify(array: Array, pyramid_dir: Path, patch: int, rng: np.random.Generator, name: str,
            errors: list[str]) -> dict[str, int]:
    """s0 chunks against a fresh snippet read; each coarser level against a fresh downsample of s0."""
    p = open_pyramid(pyramid_dir)
    down = block_mean if p.attrs["mia"]["kind"] == "raw" else stride
    size = dict(zip(p.axes, p.levels[0].shape))
    checks = {"s0_chunks": 0, "level_blocks": 0}

    def random_box(level_shape: tuple[int, ...], edge: int) -> tuple[dict, int, int, int]:
        return _random_box(rng, size, level_shape, edge)

    for _ in range(S0_CHECKS):
        plane, y0, x0, n = random_box(p.levels[0].shape, patch)
        region = plane | {"y": slice(y0, y0 + n), "x": slice(x0, x0 + n)}
        fresh = np.asarray(array.read(region)).reshape(n, n)
        if not np.array_equal(fresh, p.read(0, region)):
            errors.append(f"{name} s0 chunk at {region} differs from the source")
        checks["s0_chunks"] += 1
    for level in p.levels[1:]:
        f = level.factor
        for _ in range(LEVEL_CHECKS):
            plane, y0, x0, n = random_box(level.shape, patch)
            coarse = p.read(level.path and p.levels.index(level), plane | {"y": slice(y0, y0 + n), "x": slice(x0, x0 + n)})
            fine = p.read(0, plane | {"y": slice(y0 * f, (y0 + n) * f), "x": slice(x0 * f, (x0 + n) * f)})
            if not np.array_equal(down(fine, f), coarse):
                errors.append(f"{name} {level.path} block at {plane} y={y0} x={x0} differs from a fresh downsample")
            checks["level_blocks"] += 1
    return checks


def stage(binding_path: Path, snapshot: Path, record_path: Path, io: IO, *, levels: list[int], patch: int,
          out: Path, voxel_size_nm: dict[str, float] | None = None, seed: int = 0) -> dict:
    """Fetch every file once, open the arrays, write v<i>.zarr per raw with its labels, verify, write stage.json."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {"ok": False, "volumes": [], "checks": {"s0_chunks": 0, "level_blocks": 0}, "bytes_fetched": 0,
                 "errors": []}
    rng = np.random.default_rng(seed)
    try:
        binding = load_binding(binding_path)
        for b in [*binding.raw, *binding.labels]:
            for url in b.urls:
                io.fetch(url)       # into the shared dataset cache: the raw cache; io.open then reads it locally
        raws, labels = open_arrays(binding, Path(snapshot), io)
        provenance = {"record": binding.record, "record_commit": binding.record_commit,
                      "snippets_commit": binding.snippets_commit, "binding_hash": binding_hash(binding_path)}
        for i, raw in enumerate(raws):
            vox = _voxel_size(raw, voxel_size_nm)
            if vox is None:
                rep["errors"].append(f"raw {i}: voxel size unknown")
                continue
            vdir = out / f"v{i}.zarr"
            info = write_pyramid(raw, vdir / "raw", levels=levels, patch=patch, voxel_size_nm=vox, kind="raw", attrs=provenance)
            written = {}
            for lb, arr in zip(binding.labels, labels):
                if lb.pairs_with != i:
                    continue
                write_pyramid(arr, vdir / "labels" / lb.label_key, levels=levels, patch=patch, voxel_size_nm=vox, kind="label",
                              attrs={**provenance, "label_key": lb.label_key, "segmentation_type": lb.segmentation_type})
                written[lb.label_key] = f"labels/{lb.label_key}"
            verified = [(raw, vdir / "raw", f"v{i}")]
            verified += [(arr, vdir / "labels" / lb.label_key, f"v{i}/{lb.label_key}")
                         for lb, arr in zip(binding.labels, labels) if lb.pairs_with == i]
            for source, pdir, name in verified:
                for key, count in _verify(source, pdir, patch, rng, name, rep["errors"]).items():
                    rep["checks"][key] += count
            rep["volumes"].append({
                "path": vdir.name, "axes": info.axes, "shape": list(info.shape), "dtype": info.dtype,
                "access": getattr(raw, "access", None), "voxel_size_nm": vox,
                "levels": [{"path": lv.path, "factor": lv.factor, "shape": list(lv.shape), "scale_nm": lv.scale_nm,
                            "translation_nm": lv.translation_nm} for lv in info.levels],
                "labels": written,
                "intensity_sample": intensity_sample(open_pyramid(vdir / "raw"), patch, rng),
            })
    except HostNotAllowed as e:
        rep["errors"].append(str(e))
        rep["host_not_allowed"] = {"host": e.host, "chain": e.chain}
    except BudgetExceeded as e:
        rep["errors"].append(str(e))
    except Exception as e:      # snippet code can raise anything; the report says what
        rep["errors"].append(f"{type(e).__name__}: {e}")
    rep["bytes_fetched"] = io.bytes_fetched
    rep["ok"] = not rep["errors"]
    (out / "stage.json").write_text(json.dumps(rep, indent=2) + "\n")
    return rep
