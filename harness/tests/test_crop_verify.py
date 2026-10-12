"""harness crop, verify and preview end to end on npy-local, served over HTTP so io records validators."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml
from rangeserver import RangeServer

from mia_harness import cli
from mia_harness.png import read_png

SNAP = Path(__file__).parent
IDS = [f"i12-e99-b1-{i:03d}" for i in range(4)]


def env(mp, cap=1 << 30):
    mp.setenv("MIA_HARNESS_JOB_BYTES", str(cap))
    mp.setenv("MIA_HARNESS_ALLOWED_HOSTS", "127.0.0.1")
    mp.setenv("MIA_HARNESS_COMMIT", "c" * 40)
    mp.setenv("MIA_HARNESS_ENV_DIGEST", "d" * 64)


def save(tmp, name, a):
    p = tmp / f"{name}.npy"
    np.save(p, a)
    return p.read_bytes()


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    """(tmp, binding, record): a 2x600x600 uint16 raw and an instance label of 40 px squares, over HTTP."""
    tmp = tmp_path_factory.mktemp("ds")
    yy, xx = np.mgrid[:600, :600]
    with RangeServer() as srv:
        srv.files["/raw.npy"] = save(tmp, "raw", np.random.default_rng(0).integers(0, 4000, (2, 600, 600), np.uint16))
        srv.files["/gt.npy"] = save(tmp, "gt", np.stack([(yy // 40) * 16 + xx // 40] * 2).astype(np.uint16))
        b, r = tmp / "binding.yaml", tmp / "record.yaml"
        b.write_text(yaml.safe_dump({
            "record": "r", "record_commit": "0" * 40, "snippets_commit": "1" * 40,
            "raw": [{"snippet": "npy-local", "urls": [srv.url("/raw.npy")], "params": {}}],
            "labels": [{"snippet": "npy-local", "urls": [srv.url("/gt.npy")], "params": {}, "pairs_with": 0,
                        "label_key": "gt", "segmentation_type": "instance"}],
            "reasons": []}))
        r.write_text(yaml.safe_dump({"license": {"spdx": "CC-BY-4.0"}}))
        yield tmp, b, r


def crop(ds, name, n=16, *extra):
    tmp, b, r = ds
    out = tmp / name
    code = cli.main(["crop", str(b), "--snippets", str(SNAP), "--record", str(r), "--n", str(n), "--seed", "s1",
                     "--round", "i12-e99", "--batch", "1", "--out", str(out), "--work", str(tmp / f"work-{name}"),
                     *extra])
    return code, out, json.loads((out / "report.json").read_text())


@pytest.fixture(scope="module")
def made(ds):
    with pytest.MonkeyPatch.context() as mp:
        env(mp)
        return crop(ds, "out")


def verify(ds, d, existing=None):
    out = d.parent / f"{d.name}-verify.json"
    args = ["verify", str(d), "--binding", str(ds[1]), "--out", str(out)]
    if existing:
        args += ["--existing", str(existing)]
    return cli.main(args), json.loads(out.read_text())


def meta(d, cid):
    return json.loads((d / cid / "crop.json").read_text())


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest(tmp, *entries):
    p = tmp / "manifest.json"
    p.write_text(json.dumps({"crops": list(entries), "count": len(entries)}))
    return p


def test_crop_makes_half_the_capacity_with_full_provenance(made):
    code, out, rep = made
    assert code == 0
    assert {k: rep[k] for k in ("requested_n", "n", "capacity", "made", "errors")} == \
        {"requested_n": 16, "n": 4, "capacity": 8, "made": 4, "errors": []}
    assert rep["source_shapes"] == {"0": {"axes": "zyx", "shape": [2, 600, 600]}}
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == IDS
    m = meta(out, IDS[0])
    assert m["licence"] == "CC-BY-4.0" and m["crop_id"] == IDS[0] and m["round_id"] == "i12-e99"
    assert m["harness"] == {"commit": "c" * 40, "env_digest": "d" * 64}
    assert m["snippets"] == [{"name": "npy-local", "commit": "1" * 40}] * 2
    assert set(m["source"]["validators"]) == set(m["source"]["urls"]) and m["source"]["raw_index"] == 0
    assert m["box"]["h"] == m["box"]["w"] == 256 and m["plane"]["c"] is None and m["plane"]["z"] in (0, 1)
    assert m["mapping"]["kind"] == "stretch" and m["source_dtype"] == "uint16" and m["extent_um"] is None
    assert m["labels"] == [{"label_key": "gt", "segmentation_type": "instance", "mapping": {"kind": "identity"}}]
    assert m["source_shape"] == {"axes": "zyx", "shape": [2, 600, 600]} and m["bytes_fetched"] > 0


def test_same_seed_gives_byte_identical_pngs(ds, made, monkeypatch):
    env(monkeypatch)
    _, again, _ = crop(ds, "again")
    pngs = sorted(p.relative_to(made[1]) for p in made[1].rglob("*.png"))
    assert len(pngs) == 8 and pngs == sorted(p.relative_to(again) for p in again.rglob("*.png"))
    assert all((made[1] / p).read_bytes() == (again / p).read_bytes() for p in pngs)


def test_verify_passes_and_lists_checksums(ds, made):
    code, res = verify(ds, made[1])
    assert code == 0 and res["ok_count"] == res["n"] == 4
    c = res["crops"][0]
    assert c["errors"] == [] and set(c["files"]) == {"crop.json", "raw.png", "labels/gt.png"}
    assert c["files"]["raw.png"] == sha(made[1] / IDS[0] / "raw.png")


def test_tampered_png_fails_that_crop(ds, made, tmp_path):
    d = shutil.copytree(made[1], tmp_path / "out")
    p = d / IDS[1] / "raw.png"
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 0xFF
    p.write_bytes(bytes(b))
    code, res = verify(ds, d)
    assert code == 1 and res["ok_count"] == 3 and [c["ok"] for c in res["crops"]] == [True, False, True, True]


def test_duplicate_checksum_under_another_crop_id_fails(ds, made, tmp_path):
    m = meta(made[1], IDS[0])
    other = {**m, "crop_id": "i1-e1-b1-000", "source": {**m["source"], "raw_index": 7}, "withdrawn": False,
             "files": {"raw.png": sha(made[1] / IDS[0] / "raw.png")}}
    code, res = verify(ds, made[1], manifest(tmp_path, other))
    assert code == 1 and [c["ok"] for c in res["crops"]] == [False, True, True, True]
    assert "i1-e1-b1-000" in res["crops"][0]["errors"][0]


def test_same_crop_id_and_checksum_in_existing_counts_as_published(ds, made, tmp_path):
    entries = [{**meta(made[1], c), "withdrawn": False, "files": {"raw.png": sha(made[1] / c / "raw.png")}}
               for c in IDS]
    code, res = verify(ds, made[1], manifest(tmp_path, *entries))
    assert code == 0 and res["ok_count"] == 4


def test_crop_avoids_existing_crops(ds, made, monkeypatch, tmp_path):
    env(monkeypatch)
    m = manifest(tmp_path, *[{**meta(made[1], c), "withdrawn": False} for c in IDS])
    code, out, rep = crop(ds, "second", 16, "--existing", str(m))
    assert code == 0 and rep["made"] >= 1 and rep["rejected_overlap"] > made[2]["rejected_overlap"]
    assert verify(ds, out, m)[0] == 0   # the same seed would have redrawn the first boxes


def test_preview_draws_label_boundaries(made, tmp_path):
    out = tmp_path / "preview"
    assert cli.main(["preview", str(made[1]), "--out", str(out)]) == 0
    assert sorted(p.parent.name for p in out.glob("*/overlay.png")) == IDS
    ov, depth = read_png(out / IDS[0] / "overlay.png")
    raw = read_png(made[1] / IDS[0] / "raw.png")[0] >> 8
    lab = read_png(made[1] / IDS[0] / "labels" / "gt.png")[0]
    assert depth == 8 and ov.shape == (256, 256, 3)
    edge = np.argwhere((lab[:, :-1] != lab[:, 1:]) & (lab[:, :-1] != 0))[0]
    inside = np.argwhere((lab[1:-1, 1:-1] == lab[:-2, 1:-1]) & (lab[1:-1, 1:-1] == lab[2:, 1:-1])
                         & (lab[1:-1, 1:-1] == lab[1:-1, :-2]) & (lab[1:-1, 1:-1] == lab[1:-1, 2:]))[0] + 1
    assert list(ov[tuple(edge)]) != [raw[tuple(edge)]] * 3
    assert list(ov[tuple(inside)]) == [raw[tuple(inside)]] * 3


def test_byte_cap_makes_nothing_and_reports_budget_exceeded(ds, monkeypatch):
    env(monkeypatch, cap=1024)
    code, out, rep = crop(ds, "capped")
    assert code == 1 and rep["made"] == 0 and "BudgetExceeded" in rep["errors"][0]
    assert not [p for p in out.iterdir() if p.is_dir()]


def test_no_eligible_raw_writes_a_report_and_exits_1(tmp_path, monkeypatch):
    env(monkeypatch)
    p = tmp_path / "small.npy"
    np.save(p, np.zeros((200, 200), np.uint16))
    b = tmp_path / "binding.yaml"
    b.write_text(yaml.safe_dump({"record": "r", "record_commit": "0", "snippets_commit": "1", "reasons": [],
                                 "raw": [{"snippet": "npy-local", "urls": [], "params": {"path": str(p)}}],
                                 "labels": []}))
    (tmp_path / "record.yaml").write_text("{}\n")
    code = cli.main(["crop", str(b), "--snippets", str(SNAP), "--record", str(tmp_path / "record.yaml"), "--n", "4",
                     "--seed", "s", "--round", "i1-e1", "--batch", "1", "--out", str(tmp_path / "out"),
                     "--work", str(tmp_path)])
    rep = json.loads((tmp_path / "out" / "report.json").read_text())
    assert code == 1 and rep["made"] == rep["capacity"] == 0 and "ineligible" in rep["errors"][0]


def test_verify_and_preview_cannot_load_snippet_code():
    code = ("import sys, mia_harness.verify, mia_harness.preview; "
            "sys.exit(any(m in sys.modules for m in ('mia_harness.loader', 'mia_harness.run')))")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
