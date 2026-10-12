"""`harness verify` (SPEC §6.4). Loads no snippet code: it reads only crop.json, the PNGs and report.json."""
import hashlib
import json
import re
from pathlib import Path

from .api import CROP
from .binding import LABEL_KEY, Binding, load_binding
from .png import content_ok, read_png
from .sample import OVERLAP_MAX, overlap_frac

REQUIRED = ("record", "record_commit", "licence", "source", "plane", "box", "voxel_size_nm", "extent_um",
            "source_dtype", "mapping", "stats", "labels", "snippets", "harness", "seed", "batch", "round_id",
            "crop_id", "bytes_fetched", "source_shape")


def box_of(m: dict) -> dict:
    """A crop.json or manifest entry as overlap_frac's {raw, plane, y, x}."""
    return {"raw": m["source"]["raw_index"], "plane": {k: v for k, v in m["plane"].items() if v is not None},
            "y": m["box"]["y"], "x": m["box"]["x"]}


def load_existing(path: Path | None) -> list[dict]:
    return json.loads(Path(path).read_text())["crops"] if path else []


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _check(d: Path, binding: Binding, shapes: dict | None) -> tuple[dict | None, list[str]]:
    """(crop.json if complete, this crop's own errors); the cross-crop checks are verify's."""
    m = json.loads((d / "crop.json").read_text())
    errs = [f"crop.json lacks {k!r}" for k in REQUIRED if k not in m]
    if errs:
        return None, errs
    err = errs.append
    if m["crop_id"] != d.name:
        err(f"crop.json says crop_id {m['crop_id']!r}")
    if not m["licence"]:
        err("crop.json has no licence")
    if not m["source"].get("validators"):
        err("crop.json has no source validators")
    raw, depth = read_png(d / "raw.png")
    if raw.shape != (CROP, CROP) or depth != 16:
        err(f"raw.png is {list(raw.shape)} {depth}-bit, not {CROP}x{CROP} 16-bit")
    elif not content_ok(raw):
        err("raw.png fails the content filter")
    for lb in m["labels"]:
        key = lb["label_key"]
        if not re.fullmatch(LABEL_KEY, key):
            err(f"bad label_key {key!r}")
            continue
        p = d / "labels" / f"{key}.png"
        if not p.is_file():
            err(f"labels/{key}.png is missing")
            continue
        a, depth = read_png(p)
        want = 8 if lb["mapping"]["kind"] == "binary" else 16
        if a.shape != (CROP, CROP) or depth != want:
            err(f"labels/{key}.png is {list(a.shape)} {depth}-bit, not {CROP}x{CROP} {want}-bit")
    i = m["source"]["raw_index"]
    if shapes is not None and shapes.get(str(i)) != m["source_shape"]:
        err(f"source_shape {m['source_shape']} is not report.json's {shapes.get(str(i))}")
    sizes = dict(zip(m["source_shape"]["axes"], m["source_shape"]["shape"]))
    b = m["box"]
    if not (b["h"] == b["w"] == CROP and 0 <= b["y"] <= sizes["y"] - CROP and 0 <= b["x"] <= sizes["x"] - CROP):
        err(f"box {b} is outside the source {sizes}")
    for k in "zc":
        v, n = m["plane"].get(k), sizes.get(k)
        if not (v is None and n is None or n is not None and isinstance(v, int) and 0 <= v < n):
            err(f"plane {k}={v} is outside the source {sizes}")
    if (m["record"], m["record_commit"]) != (binding.record, binding.record_commit):
        err(f"record {m['record']}@{m['record_commit']} is not the binding's")
    if any(s["commit"] != binding.snippets_commit for s in m["snippets"]):
        err(f"snippets commits {[s['commit'] for s in m['snippets']]} are not the binding's")
    if not (0 <= i < len(binding.raw) and m["source"]["urls"] == binding.raw[i].urls):
        err(f"raw {i} urls are not the binding's")
    return m, errs


def verify(crops_dir: Path, binding_path: Path, existing: Path | None = None) -> dict:
    crops_dir, binding = Path(crops_dir), load_binding(binding_path)
    report = crops_dir / "report.json"
    shapes = json.loads(report.read_text()).get("source_shapes") if report.is_file() else None
    results, metas = [], []
    for d in sorted(p for p in crops_dir.iterdir() if p.is_dir()):
        files = {str(p.relative_to(d)): _sha(p) for p in sorted(d.rglob("*")) if p.is_file()}
        try:
            m, errs = _check(d, binding, shapes)
        except Exception as e:    # the crop job may have written anything
            m, errs = None, [f"{type(e).__name__}: {e}"]
        results.append({"crop_id": d.name, "ok": False, "errors": errs, "files": files})
        metas.append(m)
    # ponytail: O(n^2) pairwise scan; index by (record, raw, plane) if a dataset passes a few thousand crops
    ours = [(r["crop_id"], m, r["files"].get("raw.png"), False) for r, m in zip(results, metas) if m]
    theirs = [(e.get("crop_id"), e, (e.get("files") or {}).get("raw.png"), True) for e in load_existing(existing)]
    for r, m in zip(results, metas):
        if m is None:
            continue
        sha = r["files"].get("raw.png")
        for cid, o, osha, published in ours + theirs:
            if cid == r["crop_id"]:     # itself, or this very crop already published
                if published and osha and osha != sha:
                    r["errors"].append(f"{cid} is already published with another raw.png")
                continue
            if osha and osha == sha:
                r["errors"].append(f"raw.png has the same checksum as {cid}")
            if o.get("record") == m["record"] and overlap_frac(box_of(m), box_of(o), CROP) > OVERLAP_MAX:
                r["errors"].append(f"overlaps {cid} by more than {OVERLAP_MAX:.0%}")
    for r in results:
        r["ok"] = not r["errors"]
    return {"crops": results, "ok_count": sum(r["ok"] for r in results), "n": len(results)}
