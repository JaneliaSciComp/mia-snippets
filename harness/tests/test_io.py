import io as stdio
import os
import zipfile

import pytest
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness.cache import DatasetCache
from mia_harness.io import BLOCK, IO, BudgetExceeded, HostNotAllowed, NeedsFetch, from_env

DATA = os.urandom(3 * BLOCK + 17)


@pytest.fixture
def make_io(tmp_path):
    made = []

    def make(cap=10 * BLOCK, hosts=("127.0.0.1",), writable=True):
        cache = DatasetCache(tmp_path / "shared", tmp_path / "private", writable)
        made.append(IO(cache, tmp_path / "staging", cap, set(hosts)))
        return made[-1]

    yield make
    for i in made:
        i.close()


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
              "MIA_HARNESS_CACHE_WRITABLE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="MIA_HARNESS_JOB_BYTES"):
        from_env(tmp_path)
    monkeypatch.setenv("MIA_HARNESS_JOB_BYTES", "1000")
    io = from_env(tmp_path)
    assert io.byte_cap == 1000 and io.allowed_hosts == set()
    assert io.cache.shared == io.cache.private == tmp_path / "cache-private" and not io.cache.writable
    io.close()
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
