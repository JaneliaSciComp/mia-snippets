import hashlib
import io as stdio
import json
import os
import time
import zipfile

import httpx
import pytest
from rangeserver import RangeServer, server  # noqa: F401  (fixture)

import mia_harness.io as io_module
from mia_harness.cache import DatasetCache
from mia_harness.io import BLOCK, IO, BudgetExceeded, HostNotAllowed, NeedsFetch, from_env
from mia_harness.meter import Meter

DATA = os.urandom(3 * BLOCK + 17)


@pytest.fixture
def make_io(tmp_path):
    made = []

    def make(cap=10 * BLOCK, hosts=("127.0.0.1",), writable=True):
        cache = DatasetCache(tmp_path / "shared", tmp_path / "private", writable)
        made.append(IO(cache, tmp_path / "staging", cap, set(hosts), meter=Meter(tmp_path / "transfers", timer=False)))
        return made[-1]

    yield make
    for i in made:
        i.close()


def totals(io, tmp_path):
    """What the io's meter wrote, added up per host, after closing it."""
    io.meter.close()
    out = {}
    for path in sorted((tmp_path / "transfers").glob("*.jsonl")):
        for row in map(json.loads, path.read_text().splitlines()):
            host = out.setdefault(row["host"], {"answers": {}, "recovered": {}, "final": {}})
            for name, value in row.items():
                if isinstance(value, dict):
                    for key, count in value.items():
                        host[name][key] = host[name].get(key, 0) + count
                elif name == "peak":
                    host[name] = max(host.get(name, 0), value)
                elif name not in ("minute", "host"):
                    host[name] = host.get(name, 0) + value
    return out


def zip_bytes(members, **kw):
    """members: name -> (bytes, compress_type)"""
    buf = stdio.BytesIO()
    with zipfile.ZipFile(buf, "w", **kw) as z:
        for name, (data, method) in members.items():
            z.writestr(name, data, compress_type=method)
    return buf.getvalue()


def test_range_reads_return_the_right_bytes_and_are_counted(server, make_io):
    server.files["/d.bin"] = DATA
    io = make_io()
    f = io.open(server.url("/d.bin"))
    f.seek(BLOCK + 10)
    assert f.read(100) == DATA[BLOCK + 10:BLOCK + 110]
    assert io.bytes_fetched == BLOCK
    f.seek(3 * BLOCK - 5)                       # spans block 2 and the 17-byte tail block
    assert f.read() == DATA[3 * BLOCK - 5:]
    assert io.bytes_fetched == 2 * BLOCK + 17
    assert len(server.gets()) == 2              # adjacent missing blocks share one request
    assert f.seek(0, 2) == len(DATA) and f.read(5) == b""


def test_second_read_comes_from_the_block_cache(server, make_io):
    server.files["/d.bin"] = DATA
    io = make_io()
    assert io.open(server.url("/d.bin")).read(50) == DATA[:50]
    before, gets = io.bytes_fetched, len(server.gets())
    assert io.open(server.url("/d.bin")).read(60) == DATA[:60]
    assert io.bytes_fetched == before and len(server.gets()) == gets


def test_going_over_the_cap_raises_before_the_request(server, make_io):
    server.files["/d.bin"] = DATA
    io = make_io(cap=BLOCK)
    f = io.open(server.url("/d.bin"))
    f.seek(BLOCK - 1)
    with pytest.raises(BudgetExceeded):
        f.read(2)                               # needs two blocks
    assert server.gets() == [] and io.bytes_fetched == 0
    with pytest.raises(BudgetExceeded):
        io.fetch(server.url("/d.bin"))


def test_validators_are_recorded(server, make_io):
    server.files["/d.bin"] = DATA
    io = make_io()
    io.open(server.url("/d.bin"))
    v = io.validators[server.url("/d.bin")]
    assert v["size"] == len(DATA)
    assert v["etag"].startswith('"') and v["last_modified"] == "Thu, 01 Jan 1970 00:00:00 GMT"


def test_stored_member_by_range_deflated_member_by_fetch(server, make_io):
    a, b = os.urandom(5000), b"hello " * 10000
    server.files["/z.zip"] = zip_bytes({"a.bin": (a, zipfile.ZIP_STORED), "b.bin": (b, zipfile.ZIP_DEFLATED)})
    io = make_io()
    z = server.url("/z.zip")
    assert io.list(z) == ["a.bin", "b.bin"]
    f = io.open(z + "::a.bin")
    f.seek(100)
    assert f.read(200) == a[100:300] and f.read() == a[300:]
    with pytest.raises(NeedsFetch):
        io.open(z + "::b.bin")
    assert io.fetch(z + "::b.bin").read_bytes() == b
    assert io.fetch(z + "::a.bin").read_bytes() == a
    with pytest.raises(KeyError):
        io.open(z + "::missing")


def test_server_ignoring_range_raises_instead_of_downloading(server, make_io):
    server.files["/d.bin"] = DATA
    server.ignore_range = True
    io = make_io()
    with pytest.raises(NeedsFetch, match="ignored Range"):
        io.open(server.url("/d.bin")).read(10)
    assert io.bytes_fetched == 0
    assert io.fetch(server.url("/d.bin")).read_bytes() == DATA


def test_206_for_another_offset_raises(server, make_io):
    server.files["/d.bin"] = DATA
    server.range_shift = 1
    with pytest.raises(OSError, match=r"Content-Range 'bytes 1-.*' does not start at 0"):
        make_io().open(server.url("/d.bin")).read(10)


def test_validators_describe_the_identity_body(server, make_io):
    body = b"compressible " * 10000
    server.files["/t.txt"] = body
    server.gzip = True
    io = make_io()
    p = io.fetch(server.url("/t.txt"))
    assert p.read_bytes() == body
    v = io.validators[server.url("/t.txt")]
    assert v["size"] == len(body) and not v["etag"].endswith('-gzip"')


def test_zip64(server, make_io, monkeypatch):
    a, b = os.urandom(3000), b"zip64 " * 5000
    with monkeypatch.context() as m:            # force ZIP64 extras, EOCD64 record and locator on a small zip
        m.setattr(zipfile, "ZIP64_LIMIT", 0)
        m.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 0)
        data = zip_bytes({"a.bin": (a, zipfile.ZIP_STORED), "b.bin": (b, zipfile.ZIP_DEFLATED)})
    assert b"PK\x06\x06" in data and b"PK\x06\x07" in data
    server.files["/z64.zip"] = data
    io = make_io()
    z = server.url("/z64.zip")
    assert io.list(z) == ["a.bin", "b.bin"]
    assert io.open(z + "::a.bin").read() == a
    assert io.fetch(z + "::b.bin").read_bytes() == b


def test_member_named_dotdot_lands_on_a_safe_path(server, make_io, tmp_path):
    server.files["/z.zip"] = zip_bytes({"../evil": (b"x" * 100, zipfile.ZIP_DEFLATED)})
    io = make_io()
    p = io.fetch(server.url("/z.zip") + "::../evil")
    assert p.read_bytes() == b"x" * 100
    assert p.parent == tmp_path / "shared" and p.name != "evil"
    assert not any(q.name == "evil" for q in tmp_path.rglob("*"))


def test_redirect_to_a_disallowed_host_is_refused_before_the_request(server, make_io):
    server.files["/d.bin"] = DATA
    secret = f"http://localhost:{server.port}/secret"
    server.redirects["/r1"] = server.url("/r2")
    server.redirects["/r2"] = secret
    server.redirects["/ok"] = "/d.bin"
    io = make_io()
    with pytest.raises(HostNotAllowed) as e:
        io.open(server.url("/r1"))
    assert e.value.host == "localhost"
    assert e.value.chain == [server.url("/r1"), server.url("/r2"), secret]
    assert ("HEAD", "/secret") not in server.requests and ("GET", "/secret") not in server.requests
    server.redirects["/evil"] = "http://evil.invalid/"
    with pytest.raises(HostNotAllowed) as e:
        io.fetch(server.url("/evil"))
    assert e.value.host == "evil.invalid"
    assert io.open(server.url("/ok")).read(10) == DATA[:10]   # allowed hops are followed


def test_ftp_and_other_hosts_are_refused(make_io):
    io = make_io()
    for call in (io.open, io.fetch, io.list):
        with pytest.raises(HostNotAllowed):
            call("ftp://x/y")
    with pytest.raises(HostNotAllowed) as e:
        io.open("s3://bucket/key.tif")          # rewritten to https://bucket.s3.amazonaws.com/key.tif
    assert e.value.host == "bucket.s3.amazonaws.com"
    assert e.value.chain == ["https://bucket.s3.amazonaws.com/key.tif"]
    with pytest.raises(HostNotAllowed):
        make_io(hosts=()).open("http://127.0.0.1/x")


def test_from_env(tmp_path, monkeypatch):
    for k in ("MIA_HARNESS_JOB_BYTES", "MIA_HARNESS_ALLOWED_HOSTS", "MIA_HARNESS_CACHE_DIR",
              "MIA_HARNESS_CACHE_WRITABLE", "MIA_HARNESS_TRANSFERS_DIR"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="MIA_HARNESS_JOB_BYTES"):
        from_env(tmp_path)
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", "1000")
    io = from_env(tmp_path)
    assert io.byte_cap == 1000 and io.allowed_hosts == set()
    assert io.cache.shared == io.cache.private == tmp_path / "cache-private" and not io.cache.writable
    assert io.meter.directory == tmp_path / "transfers"
    io.close()
    monkeypatch.setenv("MIA_HARNESS_TRANSFERS_DIR", str(tmp_path / "elsewhere"))
    io = from_env(tmp_path)
    assert io.meter.directory == tmp_path / "elsewhere"
    io.close()
    monkeypatch.delenv("MIA_HARNESS_TRANSFERS_DIR")
    monkeypatch.setenv("MIA_HARNESS_ALLOWED_HOSTS", "A.example.org, b.example.org")
    monkeypatch.setenv("MIA_HARNESS_CACHE_DIR", str(tmp_path / "shared"))
    monkeypatch.setenv("MIA_HARNESS_CACHE_WRITABLE", "1")
    io = from_env(tmp_path)
    assert io.allowed_hosts == {"a.example.org", "b.example.org"}
    assert io.cache.shared == tmp_path / "shared" and io.cache.writable
    assert io.staging_dir == tmp_path / "staging"
    io.close()


def test_readinto_fills_any_buffer_by_bytes(server, make_io):
    import numpy as np
    server.files["/d.bin"] = DATA
    f = make_io().open(server.url("/d.bin"))
    a = np.empty(10, np.uint16)                 # tifffile reads uncompressed pages straight into an array
    assert f.readinto(a) == 20 and a.tobytes() == DATA[:20]


def test_readinto_at_eof_is_short(server, make_io):
    import numpy as np
    server.files["/d.bin"] = DATA
    f = make_io().open(server.url("/d.bin"))
    f.seek(len(DATA) - 3)
    b = bytearray(b"\0" * 8)
    assert f.readinto(b) == 3 and bytes(b) == DATA[-3:] + b"\0" * 5
    assert f.readinto(np.empty(4, np.uint16)) == 0


def test_open_serves_a_fetched_file_locally(server, make_io):
    server.files["/a.bin"] = bytes(range(256)) * 1000
    io = make_io()
    io.fetch(server.url("/a.bin"))
    fetched = io.bytes_fetched
    before = len(server.requests)
    with io.open(server.url("/a.bin")) as f:
        f.seek(1000)
        assert f.read(3) == bytes([232, 233, 234])
    assert io.bytes_fetched == fetched
    assert [m for m, _ in server.requests[before:]] == ["HEAD"] or server.requests[before:] == []


# -- files.json manifest and manual drops (buffers spec §3)


def _manifest(tmp_path):
    return json.loads((tmp_path / "shared" / "files.json").read_text())


def test_manifest_entry_skips_head_and_serves_from_cache(server, make_io, tmp_path):
    server.files["/d.bin"] = DATA
    io1 = make_io()
    io1.fetch(server.url("/d.bin"))
    entry = _manifest(tmp_path)[server.url("/d.bin")]
    assert entry["source"] == "download" and entry["validators"]["size"] == len(DATA)
    heads = len(server.heads())
    io2 = make_io(hosts=())  # no host allowed: any request would raise HostNotAllowed
    assert io2.fetch(server.url("/d.bin")).read_bytes() == DATA and len(server.heads()) == heads


def test_range_reads_come_from_a_cached_whole_file(server, make_io):
    server.files["/d.bin"] = DATA
    io1 = make_io()
    io1.fetch(server.url("/d.bin"))
    io2 = make_io(hosts=())
    with io2.open(server.url("/d.bin")) as f:
        f.seek(BLOCK + 3)
        assert f.read(10) == DATA[BLOCK + 3:BLOCK + 13] and io2.bytes_fetched == 0
    assert io2._read(server.url("/d.bin"), BLOCK - 2, BLOCK + 2) == DATA[BLOCK - 2:BLOCK + 2]  # the block path too


def test_zip_member_from_a_dropped_archive_needs_no_network(make_io, tmp_path):
    data = zip_bytes({"m.tif": (b"x" * 100, zipfile.ZIP_STORED), "c.tif": (b"y" * 100, zipfile.ZIP_DEFLATED)})
    validators = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    url = "manual://rec/a.zip"
    key = hashlib.sha256(f"{url}\n{json.dumps(validators, sort_keys=True)}".encode()).hexdigest()
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / key).write_bytes(data)
    (tmp_path / "shared" / "files.json").write_text(json.dumps({url: {"validators": validators, "source": "manual"}}))
    io = make_io(hosts=())
    with io.open(url + "::m.tif") as member:
        assert member.read() == b"x" * 100
    assert io.fetch(url + "::c.tif").read_bytes() == b"y" * 100 and io.bytes_fetched == 0


def test_fetched_zip_members_are_served_offline_without_the_directory(server, make_io):
    """A stage job has no host and the raw cache a fetch filled: members resolve from it, never from the zip."""
    data = zip_bytes({"m.tif": (b"x" * 100, zipfile.ZIP_STORED), "c.tif": (b"y" * 100, zipfile.ZIP_DEFLATED)})
    server.files["/a.zip"] = data
    io1 = make_io()
    io1.fetch(server.url("/a.zip") + "::m.tif")
    io1.fetch(server.url("/a.zip") + "::c.tif")
    gets = len(server.gets())
    io2 = make_io(hosts=())
    with io2.open(server.url("/a.zip") + "::m.tif") as member:
        assert member.read() == b"x" * 100
    assert io2.fetch(server.url("/a.zip") + "::c.tif").read_bytes() == b"y" * 100
    assert io2.bytes_fetched == 0 and len(server.gets()) == gets


def test_manual_url_without_manifest_entry_says_so(make_io):
    with pytest.raises(FileNotFoundError, match="files.json"):
        make_io(hosts=()).fetch("manual://rec/a.zip")


def test_allowed_host_still_heads_so_upstream_changes_are_noticed(server, make_io, tmp_path):
    server.files["/d.bin"] = DATA
    make_io().fetch(server.url("/d.bin"))
    server.files["/d.bin"] = b"changed"            # new ETag: the manifest entry is stale
    assert make_io().fetch(server.url("/d.bin")).read_bytes() == b"changed"
    assert _manifest(tmp_path)[server.url("/d.bin")]["validators"]["size"] == 7


def test_read_only_cache_does_not_write_the_manifest(server, make_io, tmp_path):
    server.files["/d.bin"] = DATA
    make_io(writable=False).fetch(server.url("/d.bin"))
    assert not (tmp_path / "shared" / "files.json").exists()


INDEX = ('<html><body><h1>Index of /d</h1><a href="?C=N;O=D">Name</a><a href="/">Parent Directory</a>'
         '<a href="sub/">sub/</a><a href="a%20b.tif">a b.tif</a><a href="z_0001.tif">z_0001.tif</a>'
         '<a href="z_0000.tif">z_0000.tif</a><a href="z_0000.tif">again</a></body></html>')


def test_list_reads_an_http_directory_index_once_and_expand_joins_the_names(server, make_io):
    server.files["/d/"] = INDEX.encode()
    io = make_io()
    d = server.url("/d/")
    assert io.list(d) == ["sub/", "a%20b.tif", "z_0001.tif", "z_0000.tif"]
    assert io.expand(d + "::z_*.tif") == [d + "z_0000.tif", d + "z_0001.tif"]
    assert io.expand(d + "::*") == [d + "a%20b.tif", d + "z_0000.tif", d + "z_0001.tif"]   # no subdirectories
    assert io.expand(d + "z_0000.tif") == [d + "z_0000.tif"]
    assert io.bytes_fetched == len(INDEX) and server.gets() == ["/d/"]
    with pytest.raises(ValueError):
        io.expand(d + "::*.png")
    with pytest.raises(ValueError):
        io.list(d + "::z_0000.tif")


def test_a_dropped_truncated_or_5xx_response_is_retried_and_charged_once(server, make_io, monkeypatch):
    import mia_harness.io as io_module
    monkeypatch.setattr(io_module, "RETRY_WAIT", (0, 0))
    server.files["/d.bin"], server.files["/e.bin"] = DATA, DATA
    io = make_io()
    server.fail = 1                                 # the first range GET is cut short
    assert io.open(server.url("/d.bin")).read(BLOCK) == DATA[:BLOCK]
    assert io.bytes_fetched == BLOCK and server.gets() == ["/d.bin"] * 2
    server.fail = 1
    io.open(server.url("/d.bin")).read(2 * BLOCK)   # block 1 is new: its GET is truncated once
    assert io.bytes_fetched == 2 * BLOCK and len(server.gets()) == 4
    server.fail, server.fail_mode = 2, "503"
    assert io.fetch(server.url("/e.bin")).read_bytes() == DATA
    assert io.bytes_fetched == 2 * BLOCK + len(DATA)
    assert server.requests[-4:] == [("HEAD", "/e.bin")] * 3 + [("GET", "/e.bin")]   # the HEAD is retried too
    server.files["/f.bin"], server.fail, server.fail_mode = DATA, 3, "truncate"
    with pytest.raises(httpx.RemoteProtocolError):       # three bad answers: the error is final
        io.open(server.url("/f.bin")).read(10)
    assert io.bytes_fetched == 2 * BLOCK + len(DATA)       # the failed attempts charged nothing
    with pytest.raises(httpx.HTTPStatusError):           # a 404 is not retried
        io.open(server.url("/missing")).read(10)
    assert server.heads().count("/missing") == 1


def test_a_long_read_is_fetched_in_pieces_and_only_a_lost_piece_is_asked_for_again(server, make_io, monkeypatch):
    import mia_harness.io as io_module
    monkeypatch.setattr(io_module, "RETRY_WAIT", (0, 0))
    monkeypatch.setattr(io_module, "PIECE", 2 * BLOCK)
    data = os.urandom(5 * BLOCK + 17)
    server.files["/long.bin"] = data
    io = make_io()
    server.fail = 1                                 # one piece's answer is cut short
    assert io.open(server.url("/long.bin")).read() == data
    assert len(server.gets()) == 4                  # three pieces (2 + 2 + the rest) and the lost one again
    assert io.bytes_fetched == len(data)            # the cut answer charged nothing
    small = make_io(cap=io.bytes_fetched - 1)
    small.staging_dir = small.staging_dir / "other"   # its own block cache
    with pytest.raises(BudgetExceeded):             # a read past the cap is refused whole, before any piece
        small.open(server.url("/long.bin")).read()
    assert len(server.gets()) == 4


@pytest.fixture
def small_pieces(monkeypatch):
    """Pieces of one block and no wait between attempts, so a 3 MiB file is a large one."""
    import mia_harness.io as io_module
    monkeypatch.setattr(io_module, "RETRY_WAIT", (0, 0))
    monkeypatch.setattr(io_module, "PIECE", BLOCK)


def test_a_large_file_is_fetched_in_pieces_and_only_a_lost_piece_is_asked_for_again(server, make_io, small_pieces):
    server.files["/big.bin"], server.files["/other.bin"] = DATA, DATA
    io = make_io()
    server.fail = 1                                 # one piece's answer is cut short
    assert io.fetch(server.url("/big.bin")).read_bytes() == DATA
    assert len(server.gets()) == 5                  # four pieces (3 MiB and the 17-byte rest) and the lost one again
    assert io.bytes_fetched == len(DATA)
    with pytest.raises(BudgetExceeded):             # a file past the cap is refused whole, before any piece
        make_io(cap=2 * BLOCK).fetch(server.url("/other.bin"))
    assert len(server.gets()) == 5


def test_a_large_file_on_a_server_without_range_is_one_request(server, make_io, small_pieces):
    server.files["/big.bin"] = DATA
    server.ignore_range = True
    io = make_io()
    assert io.fetch(server.url("/big.bin")).read_bytes() == DATA and io.bytes_fetched == len(DATA)


def test_a_large_zip_member_is_fetched_in_pieces_stored_or_deflated(server, make_io, small_pieces, tmp_path):
    stored, deflated = os.urandom(2 * BLOCK + 5), os.urandom(2 * BLOCK + 7)   # random bytes do not deflate smaller
    server.files["/z.zip"] = zip_bytes({"s.bin": (stored, zipfile.ZIP_STORED), "d.bin": (deflated, zipfile.ZIP_DEFLATED)})
    z = server.url("/z.zip")
    io = make_io()
    io.list(z)
    for name, data in (("s.bin", stored), ("d.bin", deflated)):
        gets = len(server.gets())
        assert io.fetch(f"{z}::{name}").read_bytes() == data
        assert len(server.gets()) - gets == 4       # the member's local header, then three pieces
    assert [p.name for p in (tmp_path / "shared").iterdir() if p.name.startswith(".tmp-")] == []


def test_files_are_fetched_four_requests_at_a_time_whatever_their_size(server, make_io, small_pieces):
    bodies = [bytes([i]) * 1000 for i in range(10)] + [os.urandom(2 * BLOCK + 1) for _ in range(2)]
    for i, body in enumerate(bodies):
        server.files[f"/{i}.bin"] = body
    server.delay = 0.05
    io = make_io()
    paths = io.fetch_all([server.url(f"/{i}.bin") for i in range(len(bodies))])
    assert [p.read_bytes() for p in paths] == bodies and io.bytes_fetched == sum(map(len, bodies))
    assert 1 < server.peak <= 4


@pytest.fixture
def sleeps(monkeypatch):
    """The pauses the harness takes, recorded and not slept."""
    taken = []
    monkeypatch.setattr(time, "sleep", lambda seconds: taken.append(seconds) if seconds else None)
    return taken


def test_a_429_waits_as_long_as_the_server_says_and_asks_again(server, make_io, sleeps, monkeypatch):
    import mia_harness.io as io_module
    server.files["/d.bin"] = DATA
    server.fail, server.fail_mode, server.extra_headers = 5, "429", {"Retry-After": "7"}
    io = make_io()
    assert io.fetch(server.url("/d.bin")).read_bytes() == DATA      # more refusals than a dropped request gets tries
    assert sleeps == [7] * 5 and io.bytes_fetched == len(DATA)
    assert server.requests == [("HEAD", "/d.bin")] * 6 + [("GET", "/d.bin")]
    monkeypatch.setattr(io_module, "RATE_PATIENCE", 20)
    server.files["/e.bin"], server.fail = DATA, 5
    with pytest.raises(httpx.HTTPStatusError):                       # 7 + 7 s waited, a third wait would pass 20 s
        io.fetch(server.url("/e.bin"))
    assert server.heads().count("/e.bin") == 3


def test_a_429_without_a_usable_hint_backs_off(server, make_io, sleeps):
    server.files["/d.bin"] = DATA
    server.fail, server.fail_mode, server.extra_headers = 2, "429", {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
    assert make_io().fetch(server.url("/d.bin")).read_bytes() == DATA
    assert sleeps == [1.0, 4.0]


def test_requests_pause_until_the_reset_when_the_server_says_few_are_left(server, make_io, sleeps):
    server.files["/d.bin"] = DATA
    server.extra_headers = {"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": str(int(time.time()) + 30)}
    io = make_io()
    f = io.open(server.url("/d.bin"))               # the HEAD: nothing said yet, so no pause before it
    assert sleeps == []
    assert f.read(10) == DATA[:10]                  # before the GET: until the reset the HEAD's answer named
    assert len(sleeps) == 1 and 25 < sleeps[0] <= 30
    server.extra_headers = {"X-RateLimit-Remaining": "100", "X-RateLimit-Reset": "5"}
    f.seek(BLOCK)
    f.read(10)                                      # one pause per notice (the GET's own said 3 again) ...
    f.seek(2 * BLOCK)
    f.read(10)                                      # ... and none once plenty are left
    assert len(sleeps) == 2
    server.extra_headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "12"}   # a reset in seconds from now
    f.seek(3 * BLOCK)
    f.read(10)
    assert io.fetch(server.url("/d.bin")).read_bytes() == DATA
    assert sleeps[2:] == [12] or (len(sleeps) == 3 and 11 < sleeps[2] <= 12)


def test_a_small_zip_member_is_one_request_header_and_data_together(server, make_io):
    stored, deflated = os.urandom(5000), b"hello " * 10000
    long_extra = zipfile.ZipInfo("x.bin")
    long_extra.extra = b"\xfe\xca" + (3000).to_bytes(2, "little") + bytes(3000)   # a local header longer than guessed
    buf = stdio.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("s.bin", stored, compress_type=zipfile.ZIP_STORED)
        z.writestr("d.bin", deflated, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(long_extra, stored, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("last.bin", deflated, compress_type=zipfile.ZIP_DEFLATED)       # ends near the zip's end
    server.files["/z.zip"] = buf.getvalue()
    z, io = server.url("/z.zip"), make_io()
    io.list(z)
    for name, data, requests in (("s.bin", stored, 1), ("d.bin", deflated, 1), ("last.bin", deflated, 1),
                                 ("x.bin", stored, 2)):
        gets = len(server.gets())
        assert io.fetch(f"{z}::{name}").read_bytes() == data
        assert len(server.gets()) - gets == requests, name


def test_a_host_that_refuses_head_is_asked_for_its_first_byte(server, make_io):
    """figshare and Dataverse redirect a download to storage whose URL is signed for GET alone: a HEAD there is
    answered 403, a GET is served. Seventeen datasets stopped on that 403 before a byte was read. The size and
    the validators come from a one-byte ranged GET, and reads and whole-file fetches go on as for any host."""
    server.files["/store/d.bin"] = DATA
    server.redirects["/files/1"] = "/store/d.bin"
    server.refuse_head = 403
    io = make_io()
    url = server.url("/files/1")
    with io.open(url) as fh:
        fh.seek(BLOCK + 5)
        assert fh.read(100) == DATA[BLOCK + 5:BLOCK + 105]
    assert io.validators[url]["size"] == len(DATA) and io.validators[url]["etag"]
    assert server.requests[:4] == [("HEAD", "/files/1"), ("HEAD", "/store/d.bin"), ("GET", "/files/1"),
                                   ("GET", "/store/d.bin")]
    assert io.bytes_fetched == BLOCK   # the first byte asked for the size is not read, so it is not charged
    assert io.fetch(server.url("/store/d.bin")).read_bytes() == DATA


def test_a_host_that_refuses_head_and_ignores_range_still_says_its_size(server, make_io):
    server.files["/d.bin"] = DATA
    server.refuse_head, server.ignore_range = 405, True
    io = make_io()
    assert io._size(server.url("/d.bin")) == len(DATA) and io.bytes_fetched == 0


def test_a_host_that_refuses_the_get_too_is_an_error(server, make_io):
    server.files["/d.bin"] = DATA
    server.refuse_head = server.refuse_get = 403
    with pytest.raises(httpx.HTTPStatusError) as refused:
        make_io().open(server.url("/d.bin"))
    assert refused.value.response.status_code == 403 and server.gets() == ["/d.bin"]


def test_requests_bytes_and_answers_are_counted_per_host(server, make_io, tmp_path):
    server.files["/d.bin"] = DATA
    io = make_io()
    io.open(server.url("/d.bin")).read(BLOCK)
    host = totals(io, tmp_path)["127.0.0.1"]
    assert host["requests"] == len(server.requests) == 2 and host["bytes"] == BLOCK
    assert host["answers"] == {"200": 1, "206": 1} and host["peak"] == 1 and host["connection_seconds"] > 0


def test_a_body_cut_short_is_truncated_and_no_206(server, make_io, tmp_path, monkeypatch):
    monkeypatch.setattr(io_module, "RETRY_WAIT", (0, 0))
    server.files["/d.bin"], server.fail = DATA, 1
    io = make_io()
    io.open(server.url("/d.bin")).read(BLOCK)
    host = totals(io, tmp_path)["127.0.0.1"]
    assert host["answers"] == {"200": 1, "truncated": 1, "206": 1}
    assert BLOCK < host["bytes"] <= BLOCK + BLOCK // 2 and io.bytes_fetched == BLOCK   # delivered, not kept


def test_a_redirect_hop_is_an_attempt_at_the_host_that_answered_it(server, make_io, tmp_path):
    server.files["/d.bin"] = DATA
    server.redirects["/r.bin"] = f"http://localhost:{server.port}/d.bin"
    io = make_io(hosts=("127.0.0.1", "localhost"))
    assert io.fetch(server.url("/r.bin")).read_bytes() == DATA
    by = totals(io, tmp_path)
    assert set(by["127.0.0.1"]["answers"]) == {"302"} and by["127.0.0.1"]["bytes"] == 0
    assert by["localhost"]["bytes"] == len(DATA)


def test_counts_are_exact_with_four_requests_at_once(server, make_io, small_pieces, tmp_path):
    names = [f"/{i}.bin" for i in range(6)]
    server.files.update(dict.fromkeys(names, DATA))
    server.delay = 0.02
    io = make_io(cap=100 * BLOCK)
    io.fetch_all([server.url(name) for name in names])
    host = totals(io, tmp_path)["127.0.0.1"]
    assert host["requests"] == len(server.requests) == sum(host["answers"].values())
    assert host["bytes"] == 6 * len(DATA) and 1 < host["peak"] <= 4


def test_a_refused_connection_is_counted_as_connect(make_io, tmp_path, monkeypatch):
    monkeypatch.setattr(io_module, "RETRY_WAIT", (0, 0))
    with RangeServer() as quiet:
        url = quiet.url("/d.bin")             # the port is free again once the server is gone
    io = make_io()
    with pytest.raises(httpx.ConnectError):
        io.open(url)
    assert totals(io, tmp_path)["127.0.0.1"]["answers"] == {"connect": 3}


def test_the_pause_for_a_nearly_used_window_is_an_asked_wait(server, make_io, sleeps, tmp_path):
    server.files["/d.bin"] = DATA
    server.extra_headers = {"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": str(int(time.time()) + 30)}
    io = make_io()
    io.open(server.url("/d.bin")).read(10)
    host = totals(io, tmp_path)["127.0.0.1"]
    assert 25 < host["asked_wait_seconds"] <= 30 and host["backoff_seconds"] == 0
