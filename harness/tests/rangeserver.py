"""Threaded HTTP server for io tests: honours Range, serves ETag and Last-Modified, can redirect or ignore Range.

The stdlib SimpleHTTPRequestHandler ignores Range, hence this.
"""
import gzip
import hashlib
import re
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class RangeServer:
    def __init__(self):
        self.files: dict[str, bytes] = {}        # path -> body
        self.redirects: dict[str, str] = {}      # path -> Location (302)
        self.ignore_range = False                # answer 200 with the whole body
        self.refuse_head = 0                     # answer a HEAD for a file with this status (403: signed for GET)
        self.refuse_get = 0                      # ... and a GET for one with this
        self.range_shift = 0                     # answer a Range request this many bytes later than asked
        self.gzip = False                        # serve a gzip variant (own ETag) to clients that accept it
        self.fail = 0                            # answer this many more requests badly, as `fail_mode` says
        self.fail_mode = "truncate"              # "truncate": half the body, then close; "503", "429": that status
        self.fail_modes: list[str] = []          # while not empty, each request for a file takes its first item as its
        #                                          fail_mode ("ok": answered normally), before `fail` is looked at
        self.extra_headers: dict[str, str] = {}  # sent with every answer to a file's path (rate limit headers)
        self.requests: list[tuple[str, str]] = []  # (method, path), every request in order
        self.delay = 0.0                         # seconds each request takes before it is answered
        self.peak = 0                            # the most requests ever in flight at once
        self._active, self._lock = 0, threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.state = self
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.02,), daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_port

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def gets(self) -> list[str]:
        return [p for m, p in self.requests if m == "GET"]

    def heads(self) -> list[str]:
        return [p for m, p in self.requests if m == "HEAD"]

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._serve(body=False)

    def do_GET(self):
        self._serve(body=True)

    def _serve(self, body: bool):
        s = self.server.state
        with s._lock:
            s._active += 1
            s.peak = max(s.peak, s._active)
        try:
            time.sleep(s.delay)
            self._answer(body)
        finally:
            with s._lock:
                s._active -= 1

    def _answer(self, body: bool):
        s = self.server.state
        s.requests.append((self.command, self.path))
        if self.path in s.redirects:
            self.send_response(302)
            self.send_header("Location", s.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = s.files.get(self.path)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if refused := s.refuse_head if self.command == "HEAD" else s.refuse_get:
            self.send_response(refused)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        etag = hashlib.sha1(data).hexdigest()
        gz = s.gzip and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            data, etag = gzip.compress(data, mtime=0), etag + "-gzip"
        start, end, status = 0, len(data), 200
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if m and not s.ignore_range:
            start, end, status = int(m[1]), min(len(data), int(m[2]) + 1 if m[2] else len(data)), 206
            start, end = start + s.range_shift, min(len(data), end + s.range_shift)
        mode, failing = s.fail_mode, s.fail > 0 and (body or s.fail_mode != "truncate")   # a truncated HEAD has no body
        if s.fail_modes:
            mode = s.fail_modes.pop(0)
            failing = mode != "ok"
        elif failing:
            s.fail -= 1
        if failing:
            if mode != "truncate":
                status, start, end = int(mode), 0, 0
            self.send_response(status)
            for name, value in s.extra_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(end - start))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end - 1}/{len(data)}")
            self.end_headers()
            if body and status < 400:
                self.wfile.write(data[start:start + (end - start) // 2])
            return                                  # the connection closes: a body shorter than promised
        self.send_response(status)
        self.send_header("Content-Length", str(end - start))
        self.send_header("Accept-Ranges", "bytes")
        for name, value in s.extra_headers.items():
            self.send_header(name, value)
        self.send_header("ETag", f'"{etag}"')
        self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Last-Modified", formatdate(0, usegmt=True))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end - 1}/{len(data)}")
        self.end_headers()
        if body:
            try:
                self.wfile.write(data[start:end])
            except (BrokenPipeError, ConnectionResetError):  # client refused the body (the ignore_range test)
                pass


@pytest.fixture
def server():
    with RangeServer() as s:
        yield s
