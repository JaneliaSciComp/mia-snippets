"""All snippet network I/O (SPEC §5.2): range files, whole-file fetches, byte cap, validators, host checks."""
import hashlib
import io
import itertools
import json
import os
import re
from contextlib import closing
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import urlsplit

import httpx

from . import remotezip
from .api import BudgetExceeded, HostNotAllowed, NeedsFetch
from .cache import DatasetCache

BLOCK = 1 << 20          # range reads are aligned to, and cached in, 1 MiB blocks
MAX_REDIRECTS = 10


class IO:
    def __init__(self, cache: DatasetCache, staging_dir: Path, byte_cap: int, allowed_hosts: set[str],
                 client: httpx.Client | None = None):
        self.cache, self.staging_dir, self.byte_cap = cache, Path(staging_dir), byte_cap
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self._own_client = client is None
        self._client = client or httpx.Client(follow_redirects=False, timeout=60)
        self.bytes_fetched = 0
        self.validators: dict[str, dict] = {}   # url -> {"etag", "last_modified", "size"}
        self._zips: dict[str, dict] = {}         # zip url -> members by name

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def open(self, url: str) -> BinaryIO:
        """Seekable file whose reads are range GETs, cached on disk in 1 MiB blocks."""
        url, member = _split(url)
        if member is None:
            if (local := self.cache.get(_key(url, self._head(url)))) is not None:   # fetched before: no network
                return open(local, "rb")
            return _RangeFile(self, url, 0, self._size(url))
        info = self._member(url, member)
        if info.compress_type == remotezip.ZIP_DEFLATED:
            raise NeedsFetch(f"{member!r} is compressed; use io.fetch")
        return _RangeFile(self, url, self._data_offset(url, info), info.file_size)

    def fetch(self, url: str) -> Path:
        """Local path of the whole file, through the dataset cache; a hit costs 0 bytes."""
        url, member = _split(url)
        if member is None:
            key = _key(url, self._head(url))
            return self.cache.get(key) or self.cache.put(key, lambda p: _write(p, self._get(url)))
        info = self._member(url, member)
        key = _key(f"{url}::{member}", self._head(url))

        def write(p: Path) -> None:
            offset = self._data_offset(url, info)
            remotezip.extract(self._get(url, offset, offset + info.compress_size), info, p)

        return self.cache.get(key, check=lambda p: remotezip.crc_ok(p, info)) or self.cache.put(key, write)

    def list(self, url: str) -> list[str]:
        """Member names of a zip."""
        url, member = _split(url)
        if member is not None:
            raise ValueError(f"io.list takes a zip url, not a member: {member!r}")
        return list(self._directory(url))

    # -- zip members

    # ponytail: member access needs Range; on a server that ignores it, even fetch(member) raises NeedsFetch.
    # The fallback is downloading the whole zip into the dataset cache and reading members locally.

    def _directory(self, url: str) -> dict:
        # ponytail: one central-directory tail read (a 1 MiB block) per zip per job, even when every member
        # is a cache hit; cache the CD bytes (keyed by validators) to make those hits free
        if url not in self._zips:
            self._zips[url] = remotezip.read_directory(_RangeFile(self, url, 0, self._size(url)))
        return self._zips[url]

    def _member(self, url: str, member: str):
        info = self._directory(url).get(member)
        if info is None:
            raise KeyError(f"no member {member!r} in {url}")
        return info

    def _data_offset(self, url: str, info) -> int:
        """One 30-byte range request for the local header, not a 1 MiB block."""
        return remotezip.data_offset(b"".join(self._get(url, info.header_offset, info.header_offset + 30)), info)

    # -- HTTP

    def _head(self, url: str) -> dict:
        # ponytail: HEAD only; if a host refuses HEAD, take size/validators from a ranged GET's Content-Range
        if url not in self.validators:
            with closing(self._send("HEAD", url)) as r:
                r.raise_for_status()
                size = r.headers.get("content-length")
                self.validators[url] = {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified"),
                                        "size": int(size) if size is not None else None}
        return self.validators[url]

    def _size(self, url: str) -> int:
        size = self._head(url)["size"]
        if size is None:
            raise NeedsFetch(f"{url} has no Content-Length; use io.fetch")
        return size

    def _get(self, url: str, start: int | None = None, end: int | None = None) -> Iterator[bytes]:
        """Body chunks of the whole file, or of bytes [start, end); counted against the cap."""
        size = self._head(url)["size"] if start is None else end - start
        if size is not None and self.bytes_fetched + size > self.byte_cap:
            raise BudgetExceeded(f"{url}: {size} more bytes would pass the cap "
                                 f"({self.bytes_fetched} of {self.byte_cap} used)")
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{end - 1}"
        with closing(self._send("GET", url, headers)) as r:
            r.raise_for_status()
            if start is not None and r.status_code != 206:
                raise NeedsFetch(f"{url}: server ignored Range (status {r.status_code}); "
                                 "io.fetch can get a whole file, but not a zip member")
            if start is not None:
                cr = r.headers.get("content-range", "")
                m = re.match(r"bytes (\d+)-", cr)
                if not m or int(m[1]) != start:
                    raise OSError(f"{url}: Content-Range {cr!r} does not start at {start}")
            got = 0
            for chunk in r.iter_bytes():
                got += len(chunk)
                self.bytes_fetched += len(chunk)
                if self.bytes_fetched > self.byte_cap:
                    raise BudgetExceeded(f"{url}: passed the cap of {self.byte_cap} bytes")
                yield chunk
            if size is not None and got != size:
                raise OSError(f"{url}: expected {size} bytes, got {got}")

    def _send(self, method: str, url: str, headers: dict | None = None) -> httpx.Response:
        """A streamed response; redirects are followed here so every hop's host is checked first."""
        # identity on every request, HEAD included, so validators and sizes describe the bytes we get
        headers = {**(headers or {}), "Accept-Encoding": "identity"}
        chain = [url]
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if parts.scheme not in ("http", "https") or host not in self.allowed_hosts:
                raise HostNotAllowed(host or url, chain)
            r = self._client.send(self._client.build_request(method, url, headers=headers),
                                  stream=True, follow_redirects=False)
            if not r.is_redirect:
                return r
            r.close()
            url = str(r.url.join(r.headers["location"]))
            chain.append(url)
        raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects: {' -> '.join(chain)}")

    # -- block cache (per job, under staging; distinct from the dataset cache)

    def _read(self, url: str, start: int, end: int) -> bytes:
        """Bytes [start, end) of url via 1 MiB blocks; adjacent missing blocks share one request."""
        size = self._size(url)
        d = self.staging_dir / "blocks" / _key(url, self._head(url))
        first, last = start // BLOCK, (end - 1) // BLOCK
        missing = [i for i in range(first, last + 1) if not (d / str(i)).is_file()]
        for _, run in itertools.groupby(enumerate(missing), lambda t: t[1] - t[0]):
            run = [i for _, i in run]
            data = b"".join(self._get(url, run[0] * BLOCK, min(size, (run[-1] + 1) * BLOCK)))
            d.mkdir(parents=True, exist_ok=True)
            for i in run:
                tmp = d / f".{i}.tmp"
                tmp.write_bytes(data[(i - run[0]) * BLOCK:(i - run[0] + 1) * BLOCK])
                os.replace(tmp, d / str(i))
        data = b"".join((d / str(i)).read_bytes() for i in range(first, last + 1))
        return data[start - first * BLOCK:end - first * BLOCK]


class _RangeFile(io.RawIOBase):
    """Bytes [offset, offset + length) of url: a whole file, or a stored zip member's data."""

    def __init__(self, owner: IO, url: str, offset: int, length: int):
        self._io, self._url, self._offset, self._length, self._pos = owner, url, offset, length, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = 0) -> int:
        pos += (0, self._pos, self._length)[whence]
        if pos < 0:
            raise ValueError("negative seek position")
        self._pos = pos
        return pos

    def read(self, n: int | None = -1) -> bytes:
        end = self._length if n is None or n < 0 else min(self._length, self._pos + n)
        if end <= self._pos:
            return b""
        data = self._io._read(self._url, self._offset + self._pos, self._offset + end)
        self._pos = end
        return data

    def readall(self) -> bytes:
        return self.read()

    def readinto(self, b) -> int:
        m = memoryview(b).cast("B")     # len and slicing in bytes, whatever b's item type (numpy arrays)
        data = self.read(len(m))
        m[:len(data)] = data
        return len(data)


def _split(url: str) -> tuple[str, str | None]:
    """(file url, zip member or None); `s3://bucket/key` becomes https://bucket.s3.amazonaws.com/key."""
    if url.startswith("s3://"):
        bucket, _, key = url[5:].partition("/")
        url = f"https://{bucket}.s3.amazonaws.com/{key}"   # public buckets only
    path = url.find("/", url.find("://") + 3)               # "::" in the path, not in an IPv6 host
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


def _write(path: Path, chunks: Iterator[bytes]) -> None:
    with open(path, "wb") as fh:
        fh.writelines(chunks)


def _key(url: str, validators: dict) -> str:
    return hashlib.sha256(f"{url}\n{json.dumps(validators, sort_keys=True)}".encode()).hexdigest()


def from_env(work: Path) -> IO:
    """The job's IO from the worker's MIA_HARNESS_* variables; `work` is the job's workspace."""
    env = os.environ
    if not env.get("MIA_HARNESS_JOB_BYTES"):
        raise RuntimeError("MIA_HARNESS_JOB_BYTES is not set: every job needs a byte cap")
    work = Path(work)
    private = work / "cache-private"
    shared = Path(env["MIA_HARNESS_CACHE_DIR"]) if env.get("MIA_HARNESS_CACHE_DIR") else private
    hosts = {h.strip() for h in env.get("MIA_HARNESS_ALLOWED_HOSTS", "").split(",") if h.strip()}
    writable = env.get("MIA_HARNESS_CACHE_WRITABLE", "0") == "1"
    return IO(DatasetCache(shared, private, writable), work / "staging", int(env["MIA_HARNESS_JOB_BYTES"]), hosts)
