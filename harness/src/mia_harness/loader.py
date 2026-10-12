"""Snippets from a snapshot (SPEC §5.1, §5.3, §5.6). Nothing but snippets/<name>/snippet.py is ever imported."""
import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import yaml

from .api import SPEC_VERSION

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def _yaml(path: Path):
    return yaml.safe_load(Path(path).read_text())


def withdrawn_names(snapshot_dir: Path) -> set[str]:
    """Names in the snapshot's WITHDRAWN.yaml. A snapshot without one is refused (FileNotFoundError)."""
    return {w["name"] for w in _yaml(Path(snapshot_dir) / "WITHDRAWN.yaml") or []}


def catalog(snapshot_dir: Path) -> list[dict]:
    """Every snippet.yaml in the snapshot, minus withdrawn snippets and those another one supersedes."""
    snap = Path(snapshot_dir)
    metas = [_yaml(p) for p in sorted((snap / "snippets").glob("*/snippet.yaml"))]
    hidden = withdrawn_names(snap) | {m["supersedes"] for m in metas if m.get("supersedes")}
    return [m for m in metas if m["name"] not in hidden]


def load_snippet(snapshot_dir: Path, name: str, withdrawn: set[str], params: dict | None = None) -> ModuleType:
    """Import snippets/<name>/snippet.py from the snapshot only (spec_from_file_location, unique module name).
    Refuse (ValueError): withdrawn, missing snippet.yaml, spec_version != SPEC_VERSION, name mismatch, `params`
    not matching snippet.yaml's schema; after import, a module without SPEC_VERSION 1 and a callable open_array."""
    if not _SLUG.fullmatch(name):
        raise ValueError(f"bad snippet name {name!r}")
    if name in withdrawn:
        raise ValueError(f"snippet {name!r} is withdrawn")
    d = Path(snapshot_dir).resolve() / "snippets" / name
    for f in (d / "snippet.yaml", d / "snippet.py"):   # no symlink may lead out of snippets/<name>/
        if f.resolve() != f:
            raise ValueError(f"snippet {name!r}: {f.name} resolves outside snippets/{name}/ ({f.resolve()})")
    if not (d / "snippet.yaml").is_file():
        raise ValueError(f"snippet {name!r} has no snippet.yaml in {snapshot_dir}")
    meta = _yaml(d / "snippet.yaml")
    if meta.get("spec_version") != SPEC_VERSION:
        raise ValueError(f"snippet {name!r} has spec_version {meta.get('spec_version')!r}, harness speaks {SPEC_VERSION}")
    if meta.get("name") != name:
        raise ValueError(f"snippet directory {name!r} names itself {meta.get('name')!r}")
    _check_params(name, meta.get("params") or {}, params or {})

    code = d / "snippet.py"
    digest = hashlib.sha256(str(code).encode() + b"\0" + code.read_bytes()).hexdigest()[:16]
    modname = f"mia_snippet_{name.replace('-', '_')}_{digest}"
    if modname not in sys.modules:
        spec = importlib.util.spec_from_file_location(modname, code)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod      # the documented recipe; dataclasses and pickling look modules up here
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            del sys.modules[modname]
            raise
    mod = sys.modules[modname]
    if getattr(mod, "SPEC_VERSION", None) != SPEC_VERSION or not callable(getattr(mod, "open_array", None)):
        raise ValueError(f"snippet {name!r}: snippet.py needs SPEC_VERSION = {SPEC_VERSION} and an open_array")
    return mod


def _check_params(name: str, schema: dict, params: dict) -> None:
    # ponytail: keys only, no type checks; add them when a binding passes a wrongly typed param
    for k in params:
        if k not in schema:
            raise ValueError(f"snippet {name!r}: param {k!r} is not declared in snippet.yaml")
    for k, s in schema.items():
        if (s or {}).get("required") and k not in params:
            raise ValueError(f"snippet {name!r}: required param {k!r} is missing")
