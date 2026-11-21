"""All snippet network I/O (SPEC §5.2): range files, whole-file fetches, byte cap, validators, host checks."""
from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, TypeVar
from urllib.parse import urlsplit

import httpx

from . import remotezip
from .api import BudgetExceeded, HostNotAllowed, NeedsFetch
from .cache import DatasetCache
from .meter import Meter, failed

BLOCK = 1 << 20          # range reads are aligned to, and cached in, 1 MiB blocks
PIECE = 8 * BLOCK        # anything longer (a run of missing blocks, a file, a zip member) is asked for in pieces
PARALLEL = 4             # requests a job has in flight at once: a slow host limits each connection, not the client
MAX_REDIRECTS = 10
HEAD_REFUSED = (403, 405)   # what a host answers a HEAD it does not take, though it serves the file to a GET
GLOB_CHARS = "*?["
RETRY_WAIT = (1.0, 4.0, 16.0)   # seconds before each further attempt of a request a server dropped
RATE_RESERVE = 8         # requests left in a host's rate limit window at which a job waits for the window's reset
RATE_WAIT_MAX = 120.0    # the longest one wait for a rate limit, whatever the server asks for
RATE_PATIENCE = 900.0    # seconds one request waits on 429s in all before the 429 is final
HEADER_SLACK = 1024      # bytes asked for past a member's local header as guessed: its extra field is not in the directory
S = TypeVar("S")
T = TypeVar("T")


class IO:
    def __init__(self, cache: DatasetCache, staging_dir: Path, byte_cap: int, allowed_hosts: set[str],
                 client: httpx.Client | None = None, meter: Meter | None = None):
        self.cache, self.staging_dir, self.byte_cap = cache, Path(staging_dir), byte_cap
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self._own_client = client is None
        self._client = client or httpx.Client(follow_redirects=False, timeout=60)
        self.meter = meter or Meter()            # what each host was asked and answered, per minute
        self.bytes_fetched = 0
        self._counting = threading.Lock()        # pieces and files are fetched on threads
        self._listing = threading.Lock()         # one thread reads a zip's directory or an index page
        self._slots = threading.BoundedSemaphore(PARALLEL)
        self.validators: dict[str, dict] = {}   # url -> {"etag", "last_modified", "size"}
        self._zips: dict[str, dict] = {}         # zip url -> members by name
        self._dirs: dict[str, list[str]] = {}    # directory url -> the names its index links to
        self._resume: dict[str, float] = {}      # host -> the time its rate limit window resets, when nearly used up
        self._request = threading.local()        # `failures`: this thread's request's failed attempts

    def close(self) -> None:
        if self._own_client:
            self._client.close()
        self.meter.close()

    def open(self, url: str) -> BinaryIO:
        """Seekable file whose reads are range GETs, cached on disk in 1 MiB blocks; a long read is several
        requests at once."""
        url, member = _split(url)
        if member is None:
            if (local := self._local(url)) is not None:   # fetched before, or dropped by hand: no network
                return open(local, "rb")
            return _RangeFile(self, url, 0, self._size(url))
        if self._offline(url) and (local := self.cache.get(_member_key(url, member, self._head(url)))) is not None:
            return open(local, "rb")   # a fetch extracted it: no directory read, which an offline job cannot make
        info = self._member(url, member)
        if info.compress_type == remotezip.ZIP_DEFLATED:
            raise NeedsFetch(f"{member!r} is compressed; use io.fetch")
        return _RangeFile(self, url, self._data_offset(url, info), info.file_size)

    def fetch(self, url: str) -> Path:
        """Local path of the whole file, through the dataset cache; a hit costs 0 bytes. A file or zip member
        longer than PIECE comes by range requests, several at once."""
        url, member = _split(url)
        if member is None:
            key = _key(url, self._head(url))
            return self.cache.get(key) or self.cache.put(key, lambda p: self._whole(url, p))
        key = _member_key(url, member, self._head(url))
        if self._offline(url) and (local := self.cache.get(key)) is not None:
            return local   # written whole and CRC-checked by the fetch; the zip's directory is out of reach anyway
        info = self._member(url, member)

        def write(p: Path) -> None:
            if info.compress_size <= PIECE or self._local(url) is not None:
                self._retry(lambda: remotezip.extract(self._member_bytes(url, info), info, p))
                return
            offset = self._data_offset(url, info)
            end = offset + info.compress_size
            stored = p.with_name(f"{p.name}.stored")   # the member as the zip holds it; inflated and checked from here
            try:
                self._download(url, offset, end, stored)
                with open(stored, "rb") as fh:
                    remotezip.extract(iter(lambda: fh.read(BLOCK), b""), info, p)
            finally:
                stored.unlink(missing_ok=True)

        return self.cache.get(key, check=lambda p: remotezip.crc_ok(p, info)) or self.cache.put(key, write)

    def fetch_all(self, urls: list[str]) -> list[Path]:
        """`fetch` of each url, side by side; the paths in the urls' order."""
        return self._together(self.fetch, urls)

    def list(self, url: str) -> list[str]:
        """The entries of a container: a zip's member names, or the names an HTTP directory index (a url ending
        in `/`) links to, subdirectories with their trailing `/`."""
        url, member = _split(url)
        if member is not None:
            raise ValueError(f"io.list takes a zip or directory url, not a member: {member!r}")
        return list(self._index(url)) if url.endswith("/") else list(self._directory(url))

    def expand(self, url: str) -> list[str]:
        """A `<zip>::<glob>` url as its matching member urls, a `<dir>/::<glob>` url as the matching files' plain
        urls, sorted by name (fnmatch syntax; `*` crosses `/`); any other url as itself."""
        container, member = _split(url)
        if member is None or not any(ch in member for ch in GLOB_CHARS):
            return [url]
        names = sorted(n for n in self.list(container) if not n.endswith("/") and fnmatchcase(n, member))
        if not names:
            raise ValueError(f"no member of {container} matches {member!r}")
        return [container + n if container.endswith("/") else f"{container}::{n}" for n in names]

    # -- directory indexes

    def _index(self, url: str) -> list[str]:
        """The names an HTTP directory index links to, from the dataset cache when a fetch stored them (an offline
        stage job expands a glob from it), else one GET of the page, stored for the next job."""
        with self._listing:
            if url not in self._dirs:
                key = _key(f"{url}::", self._head(url))   # the listing's entry, under the directory's validators
                if (local := self.cache.get(key)) is None:
                    names = _index_names(self._retry(lambda: b"".join(self._get(url))).decode("utf-8", "replace"))
                    local = self.cache.put(key, lambda p: p.write_text(json.dumps(names)))
                self._dirs[url] = json.loads(local.read_text())
            return self._dirs[url]

    # -- zip members

    # ponytail: member access needs Range; on a server that ignores it, even fetch(member) raises NeedsFetch.
    # The fallback is downloading the whole zip into the dataset cache and reading members locally.

    def _directory(self, url: str) -> dict:
        """Members by name, from the dataset cache when a fetch stored the directory there (an offline stage job
        can expand a member glob from it), else one tail read of the zip, stored for the next job."""
        with self._listing:
            if url not in self._zips:
                key = _key(f"{url}::", self._head(url))   # the directory's entry, under the zip's validators
                if (local := self.cache.get(key)) is not None:
                    self._zips[url] = remotezip.directory_from_json(local.read_bytes())
                else:
                    members = remotezip.read_directory(_RangeFile(self, url, 0, self._size(url)))
                    self.cache.put(key, lambda p: p.write_bytes(remotezip.directory_to_json(members)))
                    self._zips[url] = members
            return self._zips[url]

    def note_fetched(self, url: str) -> None:
        """Record in files.json that every member `url` names (`<zip>::<member or glob>`) is in the cache, so a
        worker can tell a zip whose validators are known from one whose members are there."""
        zip_url, member = _split(url)
        if member is not None and self.cache.writable:
            self.cache.manifest_put(f"{zip_url}::{member}", self._head(zip_url), "members")

    def _member(self, url: str, member: str):
        info = self._directory(url).get(member)
        if info is None:
            raise KeyError(f"no member {member!r} in {url}")
        return info

    def _data_offset(self, url: str, info) -> int:
        """One 30-byte range request for the local header, not a 1 MiB block."""
        head = self._retry(lambda: b"".join(self._bytes(url, info.header_offset, info.header_offset + 30)))
        return remotezip.data_offset(head, info)

    def _member_bytes(self, url: str, info) -> Iterator[bytes]:
        """The member's bytes as the zip holds them, its local header and data in one range request: a host that
        limits requests counts each. A header longer than guessed costs a second request for the data."""
        start = info.header_offset
        end = min(self._size(url), start + 30 + len(info.filename.encode()) + HEADER_SLACK + info.compress_size)
        chunks = self._bytes(url, start, end)
        head = b""
        for chunk in chunks:
            head += chunk
            if len(head) >= 30:
                break
        drop, left = remotezip.data_offset(head[:30], info) - start, info.compress_size
        if drop + left > end - start:
            chunks.close()
            yield from self._bytes(url, start + drop, start + drop + left)
            return
        for chunk in itertools.chain([head], chunks):   # to the end, so a body cut short is noticed
            chunk, drop = chunk[drop:], max(0, drop - len(chunk))
            if chunk and left:
                yield chunk[:left]
                left -= len(chunk[:left])

    def _local(self, url: str) -> Path | None:
        """The whole file in the dataset cache (fetched before, or dropped by hand), or None."""
        return self.cache.get(_key(url, self._head(url)))

    def _offline(self, url: str) -> bool:
        """No request may go to this URL: a manual:// one, or a host the job is not allowed (a stage from the raw
        cache). It resolves from files.json and the cache entries alone."""
        return url.startswith("manual://") or (urlsplit(url).hostname or "").lower() not in self.allowed_hosts

    def _bytes(self, url: str, start: int, end: int) -> Iterator[bytes]:
        """Bytes [start, end): from the cached whole file when there is one, else a range GET."""
        if (local := self._local(url)) is not None:
            with open(local, "rb") as fh:
                fh.seek(start)
                yield fh.read(end - start)
            return
        yield from self._get(url, start, end)

    # -- HTTP

    def _retry(self, attempt: Callable[[], T]) -> T:
        """`attempt` again after a failure, by the first of these rules that the failure fits:
        1. A 404 or 410 is final at once, whatever else the answer carries.
        2. An answer that names a wait (a Retry-After above 0, on any status; for a 429 also X-RateLimit-Reset),
           or a 429 (Zenodo's rate limit is per address, so every job of a deployment shares it), is asked again
           after the wait it names, at most RATE_WAIT_MAX each and RATE_PATIENCE seconds in all; a 429 that names
           none waits as rule 4 does.
        3. Any other status under 500 is asked once more after the last RETRY_WAIT, and is final at its second.
        4. A 5xx, a dropped connection, a timeout or a body cut short (EBI does all of these, to about one request
           in seven, often several in a row) is asked again after each RETRY_WAIT in turn, then is final.
        Anything else (the cap, a host, Range ignored) is final. The repeat of rule 3, the waits of rule 4 and the
        patience of rule 2 are the request's own, used up over all its attempts in whatever order they fail.
        When it ends, each failed attempt is counted once more as recovered (served) or final (not)."""
        # ponytail: the whole request again, not a resume; what is lost is one piece at most (PIECE)
        outer, failures = getattr(self._request, "failures", None), []
        self._request.failures = failures
        waits, refusals, waited, repeated, served = iter(RETRY_WAIT), 0, 0.0, False, False
        try:
            while True:
                seen = len(failures)
                try:
                    result = attempt()
                    served = True
                    return result
                except (httpx.TransportError, httpx.HTTPStatusError, OSError) as e:
                    status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                    named, asked = 0.0, False
                    if status in (404, 410):
                        raise
                    if status is not None:
                        headers = e.response.headers
                        named = _until(headers.get("retry-after")) or (
                            _until(headers.get("x-ratelimit-reset")) if status == 429 else 0.0)
                    if status == 429 or named:
                        pause, asked = named or RETRY_WAIT[min(refusals, len(RETRY_WAIT) - 1)], bool(named)
                        refusals, waited = refusals + 1, waited + pause
                        if waited > RATE_PATIENCE:
                            raise
                    elif status is not None and status < 500:
                        if repeated:
                            raise
                        repeated, pause = True, RETRY_WAIT[-1]
                    else:
                        pause = next(waits, None)
                        if pause is None:
                            raise
                    if len(failures) > seen:   # a failure that was no attempt (a local OSError) counts no wait
                        self.meter.waited(failures[-1][0], pause, asked)
                time.sleep(pause)
        finally:
            self._request.failures = outer
            self.meter.settled(failures, served)

    def _head(self, url: str) -> dict:
        """The URL's validators: asked of the host when it is allowed (so a changed upstream file is noticed),
        recorded in files.json for later; otherwise files.json, which is how an offline job (no hosts: a dataset a
        human dropped, or a re-stage from the cache) and every `manual://` URL resolve."""
        if url not in self.validators:
            if self._offline(url) and (known := self.cache.manifest_get(url)) is not None:
                self.validators[url] = known
            elif url.startswith("manual://"):
                raise FileNotFoundError(f"{url}: not in the raw cache's files.json; a human has to drop this file "
                                        "(buffers spec §3)")
            else:   # for a host that is not allowed, _send raises HostNotAllowed
                self.validators[url] = self._retry(lambda: self._head_once(url))
                if self.cache.writable:
                    self.cache.manifest_put(url, self.validators[url], "download")
        return self.validators[url]

    def _head_once(self, url: str) -> dict:
        """The validators from a HEAD request. A host that refuses HEAD (403 from storage whose redirect is signed
        for GET alone, as figshare's and Dataverse's are; 405) is asked for its first byte instead: the
        Content-Range of that answer gives the size, and its body is not read."""
        with self._slots, self._answer("HEAD", url) as (r, _):
            if r.status_code not in HEAD_REFUSED:
                r.raise_for_status()
                return _validators(r, r.headers.get("content-length"))
        with self._slots, self._answer("GET", url, {"Range": "bytes=0-0"}) as (r, _):
            r.raise_for_status()
            if r.status_code == 206:
                total = re.fullmatch(r"bytes \d+-\d+/(\d+)", r.headers.get("content-range", ""))
                return _validators(r, total[1] if total else None)
            return _validators(r, r.headers.get("content-length"))   # Range ignored: the whole file's length

    def _size(self, url: str) -> int:
        size = self._head(url)["size"]
        if size is None:
            raise NeedsFetch(f"{url} has no Content-Length; use io.fetch")
        return size

    def _get(self, url: str, start: int | None = None, end: int | None = None) -> Iterator[bytes]:
        """Body chunks of the whole file, or of bytes [start, end); counted against the cap. A body the server
        drops or cuts short is not charged: nothing of it is kept."""
        size = self._head(url)["size"] if start is None else end - start
        self._afford(url, size or 0)
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-{end - 1}"
        with self._slots, self._answer("GET", url, headers) as (r, host):
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
            try:
                for chunk in r.iter_bytes():
                    got += len(chunk)
                    self.meter.received(host, len(chunk))
                    self._charge(len(chunk))
                    if self.bytes_fetched > self.byte_cap:
                        raise BudgetExceeded(f"{url}: passed the cap of {self.byte_cap} bytes")
                    yield chunk
                if size is not None and got != size:
                    raise OSError(f"{url}: expected {size} bytes, got {got}")
            except (httpx.TransportError, OSError):
                self._charge(-got)
                raise

    def _charge(self, count: int) -> None:
        with self._counting:
            self.bytes_fetched += count

    def _afford(self, url: str, count: int) -> None:
        """Refuse, before anything is asked for, what would pass the byte cap."""
        if self.bytes_fetched + count > self.byte_cap:
            raise BudgetExceeded(f"{url}: {count} more bytes would pass the cap "
                                 f"({self.bytes_fetched} of {self.byte_cap} used)")

    def _together(self, work: Callable[[S], T], items: list[S]) -> list[T]:
        """`work` on each item, on PARALLEL threads when there are several; `_slots` bounds the requests in flight,
        however such calls nest. An item that fails for good fails the call; those not yet started are dropped."""
        if len(items) < 2:
            return [work(item) for item in items]
        with ThreadPoolExecutor(PARALLEL) as pool:
            return list(pool.map(work, items))

    # -- whole files and long ranges

    def _whole(self, url: str, path: Path) -> None:
        """The file at url into `path`: by pieces when it is longer than one and the server answers Range, else
        as one request."""
        size = self._head(url)["size"]
        if size is not None and size > PIECE:
            try:
                return self._download(url, 0, size, path)
            except NeedsFetch:
                pass
        self._retry(lambda: _write(path, self._get(url)))

    def _download(self, url: str, start: int, end: int, path: Path) -> None:
        """Bytes [start, end) of url into `path`, in range requests of PIECE, each retried on its own."""
        self._afford(url, end - start)
        with open(path, "wb") as fh:
            fh.truncate(end - start)

        def keep(offset: int) -> None:
            def attempt() -> None:
                with open(path, "r+b") as fh:
                    fh.seek(offset - start)
                    fh.writelines(self._get(url, offset, min(end, offset + PIECE)))
            self._retry(attempt)

        self._together(keep, list(range(start, end, PIECE)))

    def _send(self, method: str, url: str, headers: dict | None = None) -> tuple[httpx.Response, str]:
        """A streamed response and the host it was metered under; redirects are followed here so every hop's host
        is checked first."""
        # identity on every request, HEAD included, so validators and sizes describe the bytes we get
        headers = {**(headers or {}), "Accept-Encoding": "identity"}
        chain = [url]
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if parts.scheme not in ("http", "https") or host not in self.allowed_hosts:
                raise HostNotAllowed(host or url, chain)
            if (pause := self._resume.pop(host, 0.0) - time.time()) > 0:
                self.meter.waited(host, pause, asked=True)
                time.sleep(pause)   # the host's last answer said its rate limit window is nearly used up
            self.meter.sent(host)
            try:
                r = self._client.send(self._client.build_request(method, url, headers=headers),
                                      stream=True, follow_redirects=False)
            except BaseException as error:
                self._ended(host, _unanswered(error))
                raise
            remaining = r.headers.get("x-ratelimit-remaining", "")
            if remaining.isdigit() and int(remaining) <= RATE_RESERVE:
                # the count is the host's, over every job at this address: jobs slow down together, unasked
                self._resume[host] = time.time() + _until(r.headers.get("x-ratelimit-reset"))
            if not r.is_redirect:
                return r, host   # `_answer` ends the attempt when its block does
            r.close()
            self._ended(host, str(r.status_code))
            url = str(r.url.join(r.headers["location"]))
            chain.append(url)
        raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects: {' -> '.join(chain)}")

    @contextmanager
    def _answer(self, method: str, url: str, headers: dict | None = None) -> Iterator[tuple[httpx.Response, str]]:
        """The response of `_send` and its host, closed when the block ends; the meter hears how the attempt ended."""
        r, host = self._send(method, url, headers)
        outcome = str(r.status_code)
        try:
            yield r, host
        except BaseException as error:
            outcome = _failed_body(error, outcome)
            raise
        finally:
            r.close()
            self._ended(host, outcome)

    def _ended(self, host: str, outcome: str) -> None:
        """An attempt closed: the meter hears it, and a failed one is kept for the request's end, or counted as
        final at once when no request is being retried."""
        self.meter.ended(host, outcome)
        if failed(outcome):
            if (failures := getattr(self._request, "failures", None)) is not None:
                failures.append((host, outcome))
            else:
                self.meter.settled([(host, outcome)], served=False)

    # -- block cache (per job, under staging; distinct from the dataset cache)

    def _read(self, url: str, start: int, end: int) -> bytes:
        """Bytes [start, end) of url via 1 MiB blocks. Adjacent missing blocks share one request up to PIECE;
        a longer run is several requests at once, each piece retried and kept on its own. A whole file already
        in the dataset cache (fetched before, or dropped by hand) is read in place."""
        size = self._size(url)
        if self._local(url) is not None:
            return b"".join(self._bytes(url, start, end))
        d = self.staging_dir / "blocks" / _key(url, self._head(url))
        first, last = start // BLOCK, (end - 1) // BLOCK
        missing = [i for i in range(first, last + 1) if not (d / str(i)).is_file()]
        self._afford(url, sum(min(size, (i + 1) * BLOCK) - i * BLOCK for i in missing))
        runs = ([i for _, i in run] for _, run in itertools.groupby(enumerate(missing), lambda t: t[1] - t[0]))
        pieces = [run[k:k + PIECE // BLOCK] for run in runs for k in range(0, len(run), PIECE // BLOCK)]

        def keep(piece: list[int]) -> None:
            data = self._retry(lambda: b"".join(self._get(url, piece[0] * BLOCK, min(size, (piece[-1] + 1) * BLOCK))))
            for i in piece:
                tmp = d / f".{i}.tmp"
                tmp.write_bytes(data[(i - piece[0]) * BLOCK:(i - piece[0] + 1) * BLOCK])
                os.replace(tmp, d / str(i))

        if pieces:
            d.mkdir(parents=True, exist_ok=True)
            self._together(keep, pieces)   # a piece that fails for good fails the read; the others' blocks stay
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


def _unanswered(error: BaseException) -> str:
    """The outcome of an attempt that got no status: `timeout`, `connect` or `dropped`."""
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    return "connect" if isinstance(error, httpx.ConnectError) else "dropped"


def _failed_body(error: BaseException, status: str) -> str:
    """The outcome of an attempt whose block failed: the kind of failure of a body, else the status as text."""
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, (httpx.RemoteProtocolError, OSError)):
        return "truncated"
    return "dropped" if isinstance(error, httpx.TransportError) else status


def _validators(r: httpx.Response, size: str | None) -> dict:
    """What identifies the file behind an answer: its ETag, its Last-Modified and its size in bytes."""
    return {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified"),
            "size": int(size) if size is not None else None}


def _until(value: str | None) -> float:
    """Seconds to wait from a Retry-After or X-RateLimit-Reset header: seconds from now, or an epoch time (Zenodo
    and GitHub send the reset so); at most RATE_WAIT_MAX, and 0 for a header that is missing or anything else."""
    # ponytail: an epoch reset is compared with this host's clock; use the answer's Date header if clocks drift
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    if seconds > 1e9:
        seconds -= time.time()
    return min(max(seconds, 0.0), RATE_WAIT_MAX)


def _split(url: str) -> tuple[str, str | None]:
    """(file url, zip member or None); `s3://bucket/key` becomes https://bucket.s3.amazonaws.com/key."""
    if url.startswith("s3://"):
        bucket, _, key = url[5:].partition("/")
        url = f"https://{bucket}.s3.amazonaws.com/{key}"   # public buckets only
    path = url.find("/", url.find("://") + 3)               # "::" in the path, not in an IPv6 host
    i = url.find("::", path) if path >= 0 else -1
    return (url, None) if i < 0 else (url[:i], url[i + 2:])


def _index_names(html: str) -> list[str]:
    """The relative hrefs of an index page, in page order, once each: not the parent link, the sort links or
    anything absolute. Names stay percent-encoded, so `<dir url> + name` is the file's url."""
    # ponytail: Apache and nginx autoindex pages; a JSON listing or an S3 bucket index needs its own parser
    hrefs = re.findall(r'<a\s[^>]*?href="([^"]*)"', html, re.I)
    return list(dict.fromkeys(h for h in hrefs if h and not h.startswith(("?", "/", "#", ".")) and "://" not in h))


def _write(path: Path, chunks: Iterator[bytes]) -> None:
    with open(path, "wb") as fh:
        fh.writelines(chunks)


def _key(url: str, validators: dict) -> str:
    return hashlib.sha256(f"{url}\n{json.dumps(validators, sort_keys=True)}".encode()).hexdigest()


def _member_key(url: str, member: str, validators: dict) -> str:
    """A zip member's cache key: the member URL under the zip's validators."""
    return _key(f"{url}::{member}", validators)


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
    transfers = Path(env["MIA_HARNESS_TRANSFERS_DIR"]) if env.get("MIA_HARNESS_TRANSFERS_DIR") else work / "transfers"
    return IO(DatasetCache(shared, private, writable), work / "staging", int(env["MIA_HARNESS_JOB_BYTES"]), hosts,
              meter=Meter(transfers))
