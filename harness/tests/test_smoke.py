import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness import cli
from mia_harness.cache import DatasetCache
from mia_harness.io import IO
from mia_harness.run import smoke

SNAP = Path(__file__).parent          # the test snapshot: snippets/ + WITHDRAWN.yaml


def write(tmp_path, raw_shape, label_shape=None, dtype="uint16"):
    """Binding + record for one npy-local raw (and label); returns (binding, record)."""
    def arr(name, shape):
        p = tmp_path / f"{name}.npy"
        np.save(p, np.arange(np.prod(shape), dtype=np.uint16).reshape(shape))
        return {"snippet": "npy-local", "urls": [], "params": {"path": str(p)}}
    labels = [] if label_shape is None else [{**arr("label", label_shape), "pairs_with": 0,
                                              "label_key": "gt", "segmentation_type": "instance"}]
    b = tmp_path / "binding.yaml"
    b.write_text(yaml.safe_dump({"record": "r", "record_commit": "0" * 40, "snippets_commit": "1" * 40,
                                 "raw": [arr("raw", raw_shape)], "labels": labels, "reasons": []}))
    r = tmp_path / "record.yaml"
    r.write_text(yaml.safe_dump({"technical": {"arrays": [{"role": "raw", "axes": "zyx", "shape": list(raw_shape),
                                                           "dtype": dtype},
                                                          {"role": "label", "dtype": dtype}]}}))
    return b, r


def run(tmp_path, io, *a, **kw):
    b, r = write(tmp_path, *a, **kw)
    return smoke(b, SNAP, r, io)


@pytest.fixture
def io(tmp_path):
    i = IO(DatasetCache(tmp_path / "shared", tmp_path / "private", True), tmp_path / "staging", 1 << 30,
           {"127.0.0.1"})
    yield i
    i.close()


def test_300_square_is_ok_and_eligible(tmp_path, io):
    out = run(tmp_path, io, (2, 300, 300), (2, 300, 300))
    assert out == {"ok": True, "eligible": True, "shapes": {"raw": [[2, 300, 300]], "labels": [[2, 300, 300]]},
                   "warnings": [], "errors": [], "bytes_fetched": 0}


def test_200_square_is_ok_but_not_eligible(tmp_path, io):
    out = run(tmp_path, io, (200, 200))
    assert out["ok"] and not out["eligible"] and out["shapes"]["raw"] == [[200, 200]]


def test_mismatched_label_shape_is_an_error(tmp_path, io):
    out = run(tmp_path, io, (2, 300, 300), (2, 300, 299))
    assert not out["ok"] and "label 0" in out["errors"][0]


def test_record_dtype_mismatch_is_only_a_warning(tmp_path, io):
    out = run(tmp_path, io, (300, 300), dtype="float32")
    assert out["ok"] and "float32" in out["warnings"][0]


def test_reads_through_io_and_counts_bytes(tmp_path, io, server):
    buf = tmp_path / "s.npy"
    np.save(buf, np.zeros((256, 256), np.uint8))
    server.files["/a.npy"] = buf.read_bytes()
    b, r = write(tmp_path, (256, 256))
    d = yaml.safe_load(b.read_text())
    d["raw"][0].update(urls=[server.url("/a.npy")], params={})
    b.write_text(yaml.safe_dump(d))
    out = smoke(b, SNAP, r, io)
    assert out["ok"] and out["eligible"] and out["bytes_fetched"] == len(buf.read_bytes())


def test_cli_redirect_to_disallowed_host_exits_1_with_host_not_allowed(tmp_path, server, monkeypatch):
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", str(1 << 20))
    monkeypatch.setenv("MIA_HARNESS_ALLOWED_HOSTS", "127.0.0.1")
    server.redirects["/a.npy"] = "http://evil.invalid/a.npy"
    b, r = write(tmp_path, (300, 300))
    d = yaml.safe_load(b.read_text())
    d["raw"][0].update(urls=[server.url("/a.npy")], params={})
    b.write_text(yaml.safe_dump(d))
    out = tmp_path / "smoke.json"
    code = cli.main(["smoke", str(b), "--snippets", str(SNAP), "--record", str(r), "--out", str(out),
                     "--work", str(tmp_path)])
    res = json.loads(out.read_text())
    assert code == 1 and not res["ok"]
    assert res["host_not_allowed"] == {"host": "evil.invalid", "chain": [server.url("/a.npy"),
                                                                        "http://evil.invalid/a.npy"]}


def test_cli_withdrawn_snippet_fails_and_usage_errors_exit_2(tmp_path, monkeypatch):
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", "1")
    b, r = write(tmp_path, (300, 300))
    b.write_text(b.read_text().replace("npy-local", "withdrawn-one"))
    out = tmp_path / "smoke.json"
    assert cli.main(["smoke", str(b), "--snippets", str(SNAP), "--record", str(r), "--out", str(out),
                     "--work", str(tmp_path)]) == 1
    assert "is withdrawn" in json.loads(out.read_text())["errors"][0]
    with pytest.raises(SystemExit, match="2"):
        cli.main(["crop", str(b), "--n", "3"])


def test_record_without_the_role_says_so(tmp_path, io):
    b, r = write(tmp_path, (300, 300))
    r.write_text("technical: {arrays: []}\n")
    out = smoke(b, SNAP, r, io)
    assert out["ok"] and out["warnings"] == ["raw 0: record has no dtype for role 'raw'"]
