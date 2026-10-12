"""PR rules for immutable snippets (SPEC 5.5). Standalone: stdlib + pyyaml, nothing from mia_harness."""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

import yaml

BANNED_MODULES = {"socket", "subprocess", "requests", "urllib", "http", "httpx", "fsspec", "ftplib",
                  "pickle", "shelve", "ctypes", "importlib"}
HARNESS_API = "mia_harness.api"     # the only harness module a snippet may import (SPEC 2)
BANNED_CALLS = re.compile(r"os\.(system|popen|exec\w*|spawn\w*)|__import__|importlib\.import_module")


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.insert(0, node.attr)
        node = node.value
    return ".".join([node.id, *parts]) if isinstance(node, ast.Name) else ""


class _Static(ast.NodeVisitor):
    def __init__(self):
        self.errors, self.spec_version, self.open_array = [], False, False

    def visit_Import(self, n):
        for a in n.names:
            root = a.name.split(".")[0]
            if root in BANNED_MODULES | {"snippets"} or (root == "mia_harness" and a.name != HARNESS_API):
                self.errors.append(f"line {n.lineno}: import of {a.name}")

    def visit_ImportFrom(self, n):
        root = (n.module or "").split(".")[0]
        bad_os = root == "os" and any(re.fullmatch(r"\*|system|popen|exec\w*|spawn\w*", a.name) for a in n.names)
        bad_harness = root == "mia_harness" and n.module != HARNESS_API and (
            n.module != "mia_harness" or any(a.name != "api" for a in n.names))
        if n.level or root == "snippets" or root in BANNED_MODULES or bad_os or bad_harness:
            self.errors.append(f"line {n.lineno}: import from {'.' * n.level}{n.module or ''}")

    def visit_Call(self, n):
        if BANNED_CALLS.fullmatch(_dotted(n.func)):
            self.errors.append(f"line {n.lineno}: call to {_dotted(n.func)}")
        for k in n.keywords:
            if k.arg == "allow_pickle" and isinstance(k.value, ast.Constant) and k.value.value is True:
                self.errors.append(f"line {n.lineno}: allow_pickle=True")
        self.generic_visit(n)


def _static_errors(src: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"snippet.py does not parse: {e}"]
    v = _Static()
    v.visit(tree)
    errs = v.errors
    if not any(isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SPEC_VERSION" for t in s.targets)
               and isinstance(s.value, ast.Constant) and s.value.value == 1 for s in tree.body):
        errs.append("snippet.py needs a module-level SPEC_VERSION = 1")
    if not any(isinstance(s, ast.FunctionDef) and s.name == "open_array" for s in tree.body):
        errs.append("snippet.py needs a module-level open_array function")
    return errs


def _root() -> Path:
    # not __file__: CI runs a copy of this script taken from the base ref, outside the checkout
    return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True,
                               text=True).stdout.strip())


def _allowlist(text: str | None = None) -> set[str]:
    return set((text if text is not None else (_root() / "harness" / "allowlist.txt").read_text()).split())


def _meta_errors(name: str, text: str, paths: set[str], allow: set[str]) -> list[str]:
    try:
        meta = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"snippet.yaml is not valid YAML: {e}"]
    if not isinstance(meta, dict):
        return ["snippet.yaml must be a mapping"]
    errs = []
    if meta.get("name") != name:
        errs.append(f"snippet.yaml name {meta.get('name')!r} must equal the directory {name!r}")
    if meta.get("spec_version") != 1:
        errs.append(f"snippet.yaml spec_version must be 1, got {meta.get('spec_version')!r}")
    req, tested = meta.get("requires") or [], meta.get("tested_on") or []
    if not isinstance(req, list) or not all(isinstance(r, str) for r in req) or not set(req) <= allow:
        errs.append(f"snippet.yaml requires {req!r} is not within the allowlist {sorted(allow)}")
    if not isinstance(tested, list) or not all(isinstance(t, str) for t in tested):
        return errs + [f"snippet.yaml tested_on must be a list of record ids, got {tested!r}"]
    for t in tested:
        if f"snippets/{name}/examples/{t}.yaml" not in paths:
            errs.append(f"tested_on {t!r} has no examples/{t}.yaml")
    return errs


def check(changed: list[tuple[str, str]], author: str, existing_snippets: set[str], maintainers: set[str],
          read: Callable[[str], str], allowlist: set[str] | None = None) -> list[str]:
    """changed = [(status A|M|D|R, path)]. Returns error strings (empty = pass)."""
    allow = _allowlist() if allowlist is None else allowlist
    errs, new = [], {}
    maintainers = {m.lower() for m in maintainers}
    author = author.lower()
    for status, path in changed:
        parts = path.split("/")
        name = parts[1] if parts[0] == "snippets" and len(parts) > 2 else None
        if name in existing_snippets:
            errs.append(f"{status} {path}: existing snippet {name!r} is immutable")
        elif name and status == "A":
            new.setdefault(name, set()).add(path)
        elif name:
            errs.append(f"{status} {path}: only additions are allowed")
        elif author not in maintainers:
            errs.append(f"{status} {path}: only maintainers may change files outside snippets/")
    if author not in maintainers and len(new) > 1:
        errs.append(f"a PR may add one snippet, found {sorted(new)}")
    for name, paths in sorted(new.items()):
        for need in ("snippet.py", "snippet.yaml"):
            if f"snippets/{name}/{need}" not in paths:
                errs.append(f"snippets/{name}/ is missing {need}")
        if f"snippets/{name}/snippet.yaml" in paths:
            errs += [f"{name}: {e}" for e in _meta_errors(name, read(f"snippets/{name}/snippet.yaml"), paths, allow)]
        if f"snippets/{name}/snippet.py" in paths:
            errs += [f"{name}: {e}" for e in _static_errors(read(f"snippets/{name}/snippet.py"))]
    return errs


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=_root(), check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="ref the PR merges into (default origin/main). When given, the maintainers and "
                    "the allowlist are read from it, so a PR cannot loosen its own check; when omitted (local "
                    "runs only), they are read from the working tree.")
    ap.add_argument("--author", required=True)
    a = ap.parse_args(argv)
    base = a.base or "origin/main"
    root = _root()
    config = (lambda p: _git("show", f"{a.base}:{p}")) if a.base else (lambda p: (root / p).read_text())
    changed = []
    for line in _git("-c", "core.quotepath=off", "diff", "--name-status", f"{base}...HEAD").splitlines():
        status, *paths = line.split("\t")
        if status[0] in "RC":   # a rename is a delete of the old path plus an add of the new
            changed += ([("D", paths[0])] if status[0] == "R" else []) + [("A", paths[1])]
        else:
            changed.append((status[0], paths[0]))
    existing = {p.split("/")[1] for p in _git("-c", "core.quotepath=off", "ls-tree", "--name-only", base, "snippets/").split()}
    lines = config("ci/maintainers.txt").splitlines()
    maintainers = {l.strip() for l in lines if l.strip() and not l.startswith("#")}
    errs = check(changed, a.author, existing, maintainers, lambda p: (root / p).read_text(),
                 _allowlist(config("harness/allowlist.txt")))
    for e in errs:
        print(f"error: {e}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
