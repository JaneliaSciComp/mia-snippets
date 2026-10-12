"""`harness verify` (SPEC §6.4). Loads no snippet code: it reads only crop.json, the PNGs and report.json."""
import hashlib
import json
import os
import stat
from pathlib import Path

from .api import CROP
from .binding import Binding, load_binding
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


def _tree(d: Path) -> tuple[dict, list[str]]:
    """sha256 of every regular file under d by relative path, never following a link; anything that is not a
    regular file or directory is an error."""
    files, errs = {}, []

    def walk(p: str) -> None:
        with os.scandir(p) as it:
            for e in sorted(it, key=lambda e: e.name):
                rel = os.path.relpath(e.path, d)
                if e.is_symlink():
                    errs.append(f"{rel} is a symlink")
                elif e.is_dir(follow_symlinks=False):
                    walk(e.path)
                elif e.is_file(follow_symlinks=False):
                    files[rel] = _sha(Path(e.path))
                else:
                    errs.append(f"{rel} is not a regular file")

    walk(str(d))
    return files, errs


def _check(d: Path, binding: Binding, shapes: dict | None, files: dict) -> tuple[dict | None, list[str]]:
    """(crop.json if complete, this crop's own errors); the cross-crop checks are verify's. `files` is _tree's."""
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
    i = m["source"]["raw_index"]
    want = {lb.label_key: lb.segmentation_type for lb in binding.labels if lb.pairs_with == i}   # from the binding
    # the worker publishes every file listed, so a crop is exactly these files; missing ones fail below
    errs += [f"{f} is not a crop file" for f in sorted(set(files) - {"crop.json", "raw.png"}
                                                       - {f"labels/{k}.png" for k in want})]
    got = sorted(lb["label_key"] for lb in m["labels"])
    if got != sorted(want):
        err(f"crop.json labels {got} are not the binding's {sorted(want)} for raw {i}")
    for key, seg in want.items():
        p = d / "labels" / f"{key}.png"
        if not p.is_file():
            err(f"labels/{key}.png is missing")
            continue
        a, depth = read_png(p)
        bits = 8 if seg == "binary" else 16
        if a.shape != (CROP, CROP) or depth != bits:
            err(f"labels/{key}.png is {list(a.shape)} {depth}-bit, not {CROP}x{CROP} {bits}-bit")
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
    """verify.json; `errors` holds run-level failures (binding, report.json, manifest), which fail every crop."""
    crops_dir = Path(crops_dir)
    try:
        binding = load_binding(binding_path)
        report, shapes = crops_dir / "report.json", None
        if os.path.lexists(report):
            if not stat.S_ISREG(report.lstat().st_mode):
                raise ValueError("report.json is not a regular file")
            shapes = json.loads(report.read_text())["source_shapes"]
        theirs = [(e.get("crop_id"), e.get("record"), box_of(e), (e.get("files") or {}).get("raw.png"), True)
                  for e in load_existing(existing)]
        with os.scandir(crops_dir) as it:   # every directory or link in DIR is a crop; files (report.json) aren't
            entries = sorted((e for e in it if e.is_symlink() or e.is_dir(follow_symlinks=False)),
                             key=lambda e: e.name)
    except Exception as e:
        return {"crops": [], "ok_count": 0, "n": 0, "errors": [f"{type(e).__name__}: {e}"]}
    results, metas = [], []
    for e in entries:
        d, files, errs, m = Path(e.path), {}, [], None
        try:
            if e.is_symlink():
                errs.append(f"{e.name} is a symlink")
            else:
                files, errs = _tree(d)
            if not errs:
                m, errs = _check(d, binding, shapes, files)
        except Exception as ex:    # the crop job may have written anything
            m, errs = None, errs + [f"{type(ex).__name__}: {ex}"]
        results.append({"crop_id": e.name, "ok": False, "errors": errs, "files": files})
        metas.append(m)
    # ponytail: O(n^2) pairwise scan; index by (record, raw, plane) if a dataset passes a few thousand crops
    ours = [(r["crop_id"], m["record"], box_of(m), r["files"].get("raw.png"), False)
            for r, m in zip(results, metas) if m]
    for r, m in zip(results, metas):
        if m is None:
            continue
        sha, box = r["files"].get("raw.png"), box_of(m)
        for cid, record, obox, osha, published in ours + theirs:
            if cid == r["crop_id"]:     # itself, or this very crop already published
                if published and osha and osha != sha:
                    r["errors"].append(f"{cid} is already published with another raw.png")
                continue
            if osha and osha == sha:
                r["errors"].append(f"raw.png has the same checksum as {cid}")
            if record == m["record"] and overlap_frac(box, obox, CROP) > OVERLAP_MAX:
                r["errors"].append(f"overlaps {cid} by more than {OVERLAP_MAX:.0%}")
    for r in results:
        r["ok"] = not r["errors"]
    return {"crops": results, "ok_count": sum(r["ok"] for r in results), "n": len(results), "errors": []}
