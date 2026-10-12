import io as stdio
import zipfile

import pytest
from rangeserver import server  # noqa: F401  (fixture)

from mia_harness.cache import DatasetCache
from mia_harness.io import IO

KEY = "0" * 64


def test_put_and_get(tmp_path):
    c = DatasetCache(tmp_path / "s", tmp_path / "p", writable=True)
    assert c.get(KEY) is None
    p = c.put(KEY, lambda t: t.write_bytes(b"abc"))
    assert p == tmp_path / "s" / KEY and c.get(KEY) == p and p.read_bytes() == b"abc"
    assert [q.name for q in p.parent.iterdir()] == [KEY]          # no temp file left behind


def test_read_only_shared_writes_private_and_shared_wins(tmp_path):
    ro = DatasetCache(tmp_path / "s", tmp_path / "p", writable=False)
    p = ro.put(KEY, lambda t: t.write_bytes(b"private"))
    assert p == tmp_path / "p" / KEY and not (tmp_path / "s").exists()
    DatasetCache(tmp_path / "s", tmp_path / "p", writable=True).put(KEY, lambda t: t.write_bytes(b"shared"))
    assert ro.get(KEY).read_bytes() == b"shared"
    assert ro.get(KEY, check=lambda q: q.read_bytes() == b"private") == p


def test_failed_write_leaves_nothing(tmp_path):
    c = DatasetCache(tmp_path / "s", tmp_path / "p", writable=True)

    def boom(t):
        t.write_bytes(b"half")
        raise OSError("disk")

    with pytest.raises(OSError):
        c.put(KEY, boom)
    assert list((tmp_path / "s").iterdir()) == []


def test_keys_must_be_hashes(tmp_path):
    c = DatasetCache(tmp_path / "s", tmp_path / "p", writable=True)
    with pytest.raises(ValueError):
        c.put("../x", lambda t: t.write_bytes(b""))


def make_io(tmp_path, job, writable=True):
    cache = DatasetCache(tmp_path / "shared", tmp_path / job / "private", writable)
    return IO(cache, tmp_path / job / "staging", 10**8, {"127.0.0.1"})


def test_second_fetch_hits_and_changed_validators_miss(server, tmp_path):
    server.files["/f.bin"] = b"one" * 1000
    url = server.url("/f.bin")
    with_io = make_io(tmp_path, "j1")
    assert with_io.fetch(url).read_bytes() == b"one" * 1000 and with_io.bytes_fetched == 3000
    with_io.close()
    again = make_io(tmp_path, "j2")                # another job, same dataset cache
    assert again.fetch(url).read_bytes() == b"one" * 1000 and again.bytes_fetched == 0
    again.close()
    server.files["/f.bin"] = b"two" * 1000         # new ETag
    changed = make_io(tmp_path, "j3")
    assert changed.fetch(url).read_bytes() == b"two" * 1000 and changed.bytes_fetched == 3000
    changed.close()


def test_corrupted_member_fails_crc_and_is_refetched(server, tmp_path):
    buf = stdio.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m.bin", b"member" * 1000, compress_type=zipfile.ZIP_DEFLATED)
    server.files["/z.zip"] = buf.getvalue()
    url = server.url("/z.zip") + "::m.bin"
    io = make_io(tmp_path, "j1")
    p = io.fetch(url)
    hit = io.bytes_fetched
    assert io.fetch(url) == p and io.bytes_fetched == hit       # hit, CRC re-checked, 0 bytes
    p.write_bytes(b"x" * 6000)
    assert io.fetch(url).read_bytes() == b"member" * 1000
    assert io.bytes_fetched > hit
    io.close()


def test_read_only_job_writes_nothing_shared(server, tmp_path):
    server.files["/f.bin"] = b"abc" * 100
    io = make_io(tmp_path, "j1", writable=False)
    p = io.fetch(server.url("/f.bin"))
    assert p.parent == tmp_path / "j1" / "private" and not (tmp_path / "shared").exists()
    io.close()
