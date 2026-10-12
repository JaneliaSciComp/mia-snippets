from pathlib import Path

import pytest
import yaml

from mia_harness.loader import catalog, load_snippet, withdrawn_names

SNAP = Path(__file__).parent          # the test snapshot: snippets/ + WITHDRAWN.yaml
OK_PY = "SPEC_VERSION = 1\n\ndef open_array(src, io):\n    return None\n"


def snippet(root, dirname, code=OK_PY, **meta):
    d = root / "snippets" / dirname
    d.mkdir(parents=True)
    (d / "snippet.yaml").write_text(yaml.safe_dump({"name": dirname, "spec_version": 1, "params": {}, **meta}))
    (d / "snippet.py").write_text(code)


def test_loads_a_snippet_without_touching_sys_path():
    import sys
    before = list(sys.path)
    mod = load_snippet(SNAP, "npy-local", withdrawn_names(SNAP))
    assert mod.SPEC_VERSION == 1 and callable(mod.open_array)
    assert mod.__name__.startswith("mia_snippet_npy_local_") and sys.path == before


def test_withdrawn_snippet_is_refused_before_import():
    assert "withdrawn-one" in withdrawn_names(SNAP)
    with pytest.raises(ValueError, match="withdrawn"):
        load_snippet(SNAP, "withdrawn-one", withdrawn_names(SNAP))


def test_spec_version_2_is_refused_before_import():
    with pytest.raises(ValueError, match="spec_version"):
        load_snippet(SNAP, "v2-only", withdrawn_names(SNAP))


@pytest.mark.parametrize("name, setup, match", [
    ("missing", lambda r: None, "snippet.yaml"),
    ("../snippets/x", lambda r: None, "name"),
    ("renamed", lambda r: snippet(r, "renamed", name="other"), "name"),
    ("no-open", lambda r: snippet(r, "no-open", code="SPEC_VERSION = 1\n"), "open_array"),
    ("py-v2", lambda r: snippet(r, "py-v2", code="SPEC_VERSION = 2\ndef open_array(s, i): pass\n"), "SPEC_VERSION"),
])
def test_bad_snippets_are_refused(tmp_path, name, setup, match):
    setup(tmp_path)
    with pytest.raises(ValueError, match=match):
        load_snippet(tmp_path, name, set())


def test_params_must_be_declared_and_required_ones_present(tmp_path):
    snippet(tmp_path, "p", params={"key": {"type": "str", "required": True}, "axes": {"type": "str"}})
    load_snippet(tmp_path, "p", set(), {"key": "/raw", "axes": "zyx"})
    with pytest.raises(ValueError, match="'bogus'"):
        load_snippet(tmp_path, "p", set(), {"key": "/raw", "bogus": 1})
    with pytest.raises(ValueError, match="'key'"):
        load_snippet(tmp_path, "p", set(), {"axes": "zyx"})


def test_catalog_hides_withdrawn_and_superseded(tmp_path):
    for n in ("old", "gone"):
        snippet(tmp_path, n)
    snippet(tmp_path, "old-2", supersedes="old")
    (tmp_path / "WITHDRAWN.yaml").write_text("- {name: gone, reason: wrong, at: 2026-10-11, by: me}\n")
    assert [m["name"] for m in catalog(tmp_path)] == ["old-2"]
    assert "withdrawn-one" not in {m["name"] for m in catalog(SNAP)}


def test_missing_withdrawn_yaml_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        withdrawn_names(tmp_path)
