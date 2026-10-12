"""Opening a binding's arrays, and `harness smoke` (SPEC §6.1)."""
from pathlib import Path

import numpy as np
import yaml

from .api import CROP, Array, Source, check_array
from .binding import ArrayBinding, Binding, load_binding
from .io import IO, HostNotAllowed
from .loader import load_snippet, withdrawn_names


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


def _patch(a: Array) -> np.ndarray:
    """A CROP×CROP patch at the origin of plane 0."""
    region = {"z": 0, "c": 0, "y": slice(0, CROP), "x": slice(0, CROP)}
    return a.read({k: v for k, v in region.items() if k in a.axes})


def smoke(binding_path: Path, snapshot: Path, record_path: Path, io: IO) -> dict:
    out = {"ok": False, "eligible": False, "shapes": {"raw": [], "labels": []}, "warnings": [], "errors": [],
           "bytes_fetched": 0}
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
        # 4. eligibility
        eligible = [i for i, a in enumerate(raws) if _sizes(a)["y"] >= CROP and _sizes(a)["x"] >= CROP]
        out["eligible"] = bool(eligible)
        # 5. one patch from plane 0 of the first eligible raw and its labels
        if eligible:
            i = eligible[0]
            for what, a in [(f"raw {i}", raws[i])] + [(f"label {j}", labels[j])
                                                      for j, lb in enumerate(binding.labels) if lb.pairs_with == i]:
                shape = _patch(a).shape
                if shape != (CROP, CROP):
                    err(f"{what}: a {CROP}x{CROP} patch read back as {list(shape)}")
    except HostNotAllowed as e:
        err(str(e))
        out["host_not_allowed"] = {"host": e.host, "chain": e.chain}
    except Exception as e:   # snippet code can raise anything; smoke's job is to report it
        err(f"{type(e).__name__}: {e}")
    out["bytes_fetched"] = io.bytes_fetched
    out["ok"] = not out["errors"]
    return out
