"""Opening a binding's arrays, `harness smoke` and `harness crop` (SPEC §6.1-6.3)."""
import json
import os
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

from .api import CROP, Array, Source, check_array
from .binding import ArrayBinding, Binding, load_binding
from .io import IO, BudgetExceeded, HostNotAllowed, _split
from .loader import load_snippet, withdrawn_names
from .png import content_ok, encode_label, stats, stretch_raw, write_png
from .sample import OVERLAP_MAX, Volume, candidates, capacity, collect, effective_n, overlap_frac
from .verify import box_of, load_existing


def open_arrays(binding: Binding, snapshot: Path, io: IO) -> tuple[list[Array], list[Array]]:
    """(raws, labels), each opened through its snippet from `snapshot` and passed through check_array."""
    withdrawn = withdrawn_names(snapshot)

    def open_one(b: ArrayBinding) -> Array:
        mod = load_snippet(snapshot, b.snippet, withdrawn, b.params)
        a = mod.open_array(Source(tuple(b.urls), dict(b.params)), io)
        check_array(a)
        return a

    return [open_one(b) for b in binding.raw], [open_one(b) for b in binding.labels]


def _sizes(a: Array) -> dict[str, int]:
    return dict(zip(a.axes, a.shape))


def _edge(a: Array, size: int) -> int:
    """The crop edge for a raw: `size`, or the raw's own y/x extent when it is smaller (whole-image crops)."""
    return min(size, _sizes(a)["y"], _sizes(a)["x"])


def _patch(a: Array, edge: int) -> np.ndarray:
    """An edge×edge patch at the origin of plane 0."""
    region = {"z": 0, "c": 0, "y": slice(0, edge), "x": slice(0, edge)}
    return a.read({k: v for k, v in region.items() if k in a.axes})


def smoke(binding_path: Path, snapshot: Path, record_path: Path, io: IO, *, size: int = CROP) -> dict:
    out = {"ok": False, "shapes": {"raw": [], "labels": []}, "warnings": [], "errors": [], "bytes_fetched": 0}
    err = out["errors"].append
    try:
        binding = load_binding(binding_path)
        record = yaml.safe_load(Path(record_path).read_text()) or {}
        # 1. open every array through its snippet (check_array runs in open_arrays)
        raws, labels = open_arrays(binding, Path(snapshot), io)
        out["shapes"] = {"raw": [list(a.shape) for a in raws], "labels": [list(a.shape) for a in labels]}
        # 2. dtype vs the record: records are inferred, so a mismatch only warns
        expected: dict[str, set[str]] = {}
        for r in (record.get("technical") or {}).get("arrays") or []:
            expected.setdefault(r.get("role"), set()).add(str(r.get("dtype")))
        for role, arrays in (("raw", raws), ("label", labels)):
            for i, a in enumerate(arrays):
                got = np.dtype(a.dtype).name
                if role not in expected:
                    out["warnings"].append(f"{role} {i}: record has no dtype for role {role!r}")
                elif got not in expected[role]:
                    out["warnings"].append(f"{role} {i}: dtype {got} is not the record's {sorted(expected[role])}")
        # 3. each label matches its raw on every axis but c
        for i, (lb, a) in enumerate(zip(binding.labels, labels)):
            want = {k: v for k, v in _sizes(raws[lb.pairs_with]).items() if k != "c"}
            got = {k: v for k, v in _sizes(a).items() if k != "c"}
            if got != want:
                err(f"label {i} {a.axes} {list(a.shape)} does not match raw {lb.pairs_with} "
                    f"{raws[lb.pairs_with].axes} {list(raws[lb.pairs_with].shape)} outside c")
        # 4. one patch from plane 0 of the first raw and its labels; no size verdict: whether a dataset serves a
        #    buffer is the buffer plan's decision (prep-and-buffering spec §12)
        if raws:
            edge = _edge(raws[0], size)
            for what, a in [("raw 0", raws[0])] + [(f"label {j}", labels[j])
                                                   for j, lb in enumerate(binding.labels) if lb.pairs_with == 0]:
                shape = _patch(a, edge).shape
                if shape != (edge, edge):
                    err(f"{what}: a {edge}x{edge} patch read back as {list(shape)}")
    except HostNotAllowed as e:
        err(str(e))
        out["host_not_allowed"] = {"host": e.host, "chain": e.chain}
    except Exception as e:   # snippet code can raise anything; smoke's job is to report it
        err(f"{type(e).__name__}: {e}")
    out["bytes_fetched"] = io.bytes_fetched
    out["ok"] = not out["errors"]
    return out


def _region(a: Array, plane: dict, y, x) -> dict:
    """Region of `a` at plane and (y, x); c defaults to 0 for a label with channels paired to a raw without."""
    return {k: v for k, v in {"c": 0, **plane, "y": y, "x": x}.items() if k in a.axes}


def crop(binding_path: Path, snapshot: Path, record_path: Path, io: IO, *, n: int, seed: str, round_id: str,
         batch: int, out: Path, existing: Path | None = None, size: int = CROP) -> dict:
    """Sample and write crops to out/<crop_id>/ and out/report.json; returns the report. Crops made before a
    BudgetExceeded, HostNotAllowed or snippet error are kept."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rep = {"requested_n": n, "n": 0, "capacity": 0, "made": 0, "rejected_content": 0, "rejected_overlap": 0,
           "bytes_fetched": 0, "source_shapes": {}, "errors": []}
    rej = Counter()
    try:
        binding = load_binding(binding_path)
        record = yaml.safe_load(Path(record_path).read_text()) or {}
        raws, labels = open_arrays(binding, Path(snapshot), io)
        rep["source_shapes"] = {str(i): {"axes": a.axes, "shape": list(a.shape)} for i, a in enumerate(raws)}
        vols = [Volume(i, a.axes, tuple(a.shape), a.access) for i, a in enumerate(raws)]
        edges = {i: _edge(a, size) for i, a in enumerate(raws)}   # a raw smaller than size: whole-image crops
        rep["capacity"] = capacity(vols, edges)
        rep["n"] = effective_n(n, rep["capacity"])
        prior = [box_of(e) for e in load_existing(existing) if e["record"] == binding.record]
        last = {}   # plane-access raws: the last plane read, {"key": (raw, plane), "plane": array}

        def patch_of(c) -> np.ndarray:
            a = raws[c.raw]
            if a.access == "plane":
                key = (c.raw, sorted(c.plane.items()))
                if last.get("key") != key:
                    last.update(key=key, plane=np.asarray(a.read(_region(a, c.plane, slice(None), slice(None)))))
                p = last["plane"][c.y:c.y + c.size, c.x:c.x + c.size]
            else:   # chunk; and file, which the snippet staged at open
                p = np.asarray(a.read(_region(a, c.plane, slice(c.y, c.y + c.size), slice(c.x, c.x + c.size))))
            if p.shape != (c.size, c.size):
                raise ValueError(f"raw {c.raw}: a {c.size}x{c.size} patch read back as {list(p.shape)}")
            return p

        def judge(c, acc) -> str | None:
            box = {"raw": c.raw, "plane": c.plane, "y": c.y, "x": c.x}
            if any(overlap_frac(box, o, c.size) > OVERLAP_MAX for o in prior + [vars(a) for a in acc]):
                return "overlap"
            patch = patch_of(c)
            if not content_ok(patch):
                return "content"
            paired = [(lb, labels[j]) for j, lb in enumerate(binding.labels) if lb.pairs_with == c.raw]
            try:
                encoded = []
                for lb, la in paired:
                    lp = np.asarray(la.read(_region(la, c.plane, slice(c.y, c.y + c.size), slice(c.x, c.x + c.size))))
                    if lp.shape != (c.size, c.size):
                        raise ValueError(f"read back as {list(lp.shape)}")
                    encoded.append(encode_label(lp, lb.segmentation_type))
            except (BudgetExceeded, HostNotAllowed):
                raise
            except Exception as e:   # this crop is lost, the run goes on
                rep["errors"].append(f"raw {c.raw} plane {c.plane} y={c.y} x={c.x}: label {lb.label_key}: "
                                     f"{type(e).__name__}: {e}")
                return "label"
            crop_id = f"{round_id}-b{batch}-{len(acc):03d}"
            d = out / crop_id
            (d / "labels").mkdir(parents=True, exist_ok=True)
            img, mapping = stretch_raw(patch)
            write_png(d / "raw.png", img, 16)
            for (lb, _), (lab, depth, _) in zip(paired, encoded):
                write_png(d / "labels" / f"{lb.label_key}.png", lab, depth)
            rb, a = binding.raw[c.raw], raws[c.raw]
            vs = {k: float(v) for k, v in a.voxel_size_nm.items()} if a.voxel_size_nm else None
            meta = {
                "record": binding.record, "record_commit": binding.record_commit,
                "licence": (record.get("license") or {}).get("spdx"),
                "source": {"raw_index": c.raw, "urls": rb.urls,
                           "validators": {u: io.validators[_split(u)[0]] for u in rb.urls
                                          if _split(u)[0] in io.validators}},
                "plane": {"z": c.plane.get("z"), "c": c.plane.get("c")},
                "box": {"y": c.y, "x": c.x, "h": c.size, "w": c.size},
                "voxel_size_nm": vs,
                "extent_um": {"h": c.size * vs["y"] / 1000, "w": c.size * vs["x"] / 1000}
                if vs and "y" in vs and "x" in vs else None,
                "source_dtype": np.dtype(a.dtype).name, "mapping": mapping, "stats": stats(patch),
                "labels": [{"label_key": lb.label_key, "segmentation_type": lb.segmentation_type, "mapping": m}
                           for (lb, _), (_, _, m) in zip(paired, encoded)],
                "snippets": [{"name": b.snippet, "commit": binding.snippets_commit}
                             for b in [rb] + [lb for lb, _ in paired]],
                "harness": {"commit": os.environ.get("MIA_HARNESS_COMMIT"),
                            "env_digest": os.environ.get("MIA_HARNESS_ENV_DIGEST")},
                "seed": seed, "batch": batch, "round_id": round_id, "crop_id": crop_id,
                "bytes_fetched": io.bytes_fetched, "source_shape": {"axes": a.axes, "shape": list(a.shape)},
            }
            (d / "crop.json").write_text(json.dumps(meta, indent=2) + "\n")
            rep["made"] += 1
            return None

        def reject(c, acc):
            kind = judge(c, acc)
            rej.update([kind] if kind else [])
            return kind

        collect(candidates(vols, seed, size=edges), rep["n"], reject)
    except HostNotAllowed as e:
        rep["errors"].append(str(e))
        rep["host_not_allowed"] = {"host": e.host, "chain": e.chain}
    except Exception as e:   # BudgetExceeded, and whatever snippet code raises
        rep["errors"].append(f"{type(e).__name__}: {e}")
    rep.update(rejected_content=rej["content"], rejected_overlap=rej["overlap"], bytes_fetched=io.bytes_fetched)
    (out / "report.json").write_text(json.dumps(rep, indent=2) + "\n")
    return rep
