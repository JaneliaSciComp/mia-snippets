import json

import yaml
from rangeserver import server  # noqa: F401  (fixture)
from test_io import make_io  # noqa: F401  (fixture)

from mia_harness import cli
from mia_harness.api import NeedsFetch
from mia_harness.fetch import fetch
from mia_harness.io import IO


def write_binding(tmp_path, raw_urls, label_urls):
    """Binding + record whose raw and label arrays carry the given URLs; returns (binding, record)."""
    def arr(urls):
        return {"snippet": "npy-local", "urls": urls, "params": {"path": "unused.npy"}}
    labels = [{**arr(urls), "pairs_with": 0, "label_key": f"gt{i}", "segmentation_type": "instance"}
              for i, urls in enumerate(label_urls and [label_urls] or [])]
    binding = tmp_path / "binding.yaml"
    binding.write_text(yaml.safe_dump({"record": "r", "record_commit": "0" * 40, "snippets_commit": "1" * 40,
                                       "raw": [arr(raw_urls)], "labels": labels, "reasons": []}))
    record = tmp_path / "record.yaml"
    record.write_text(yaml.safe_dump({"technical": {"arrays": []}}))
    return binding, record


def test_fetch_reports_fetched_then_cached(server, make_io, tmp_path):
    server.files["/a.bin"] = b"a" * 1000
    server.files["/b.bin"] = b"b" * 10
    binding, record = write_binding(tmp_path, [server.url("/a.bin")], [server.url("/b.bin")])
    first = fetch(binding, record, make_io())
    assert first["ok"] and not first["partial"] and first["bytes_fetched"] == 1010
    assert [(f["outcome"], f["bytes"]) for f in first["files"]] == [("fetched", 1000), ("fetched", 10)]
    again = fetch(binding, record, make_io())
    assert [f["outcome"] for f in again["files"]] == ["cached", "cached"] and again["bytes_fetched"] == 0
    assert again["files"][0]["key"] == first["files"][0]["key"]


def test_fetch_partial_on_byte_cap_and_failed_on_404(server, make_io, tmp_path):
    server.files["/a.bin"] = b"a" * 1000
    binding, record = write_binding(tmp_path, [server.url("/a.bin"), server.url("/missing.bin")], [])
    capped = fetch(binding, record, make_io(cap=10))
    assert capped["partial"] and [f["outcome"] for f in capped["files"]] == ["pending", "pending"]
    assert capped["errors"]
    full = fetch(binding, record, make_io())
    assert not full["ok"] and [f["outcome"] for f in full["files"]] == ["fetched", "failed"]
    assert "404" in full["files"][1]["reason"]


def test_fetch_stream_when_a_url_cannot_be_fetched_whole(server, make_io, tmp_path, monkeypatch):
    server.files["/a.bin"] = b"a" * 10
    binding, record = write_binding(tmp_path, [server.url("/a.bin"), "https://h/store.zarr/"], [])
    real = IO.fetch

    def fake_fetch(self, url):
        if url.endswith(".zarr/"):
            raise NeedsFetch("no Content-Length")
        return real(self, url)
    monkeypatch.setattr(IO, "fetch", fake_fetch)
    rep = fetch(binding, record, make_io())
    assert [f["outcome"] for f in rep["files"]] == ["fetched", "stream"] and rep["ok"] and not rep["partial"]
    assert rep["files"][1]["reason"] == "no Content-Length"


def test_fetch_host_not_allowed_stops_the_job(server, make_io, tmp_path):
    binding, record = write_binding(tmp_path, ["https://forbidden.example/x.bin", server.url("/a.bin")], [])
    rep = fetch(binding, record, make_io(hosts=("127.0.0.1",)))
    assert not rep["ok"] and rep["host_not_allowed"]["host"] == "forbidden.example" and rep["files"] == []


def test_fetch_cli_exit_codes(server, tmp_path, monkeypatch):
    server.files["/a.bin"] = b"a" * 1000
    binding, record = write_binding(tmp_path, [server.url("/a.bin")], [])
    monkeypatch.setenv("MIA_HARNESS_ALLOWED_HOSTS", "127.0.0.1")
    args = ["fetch", str(binding), "--record", str(record), "--work", str(tmp_path / "work"),
            "--out", str(tmp_path / "fetch.json")]
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", "10")
    assert cli.main(args) == 2 and json.loads((tmp_path / "fetch.json").read_text())["partial"]
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", "100000")
    assert cli.main(args) == 0 and json.loads((tmp_path / "fetch.json").read_text())["files"][0]["outcome"] == "fetched"
