"""Threaded HTTP server for io tests: honours Range, serves ETag and Last-Modified, can redirect or ignore Range.

The stdlib SimpleHTTPRequestHandler ignores Range, hence this.
"""
import hashlib
import re
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class RangeServer:
    def __init__(self):
        self.files: dict[str, bytes] = {}        # path -> body
        self.redirects: dict[str, str] = {}      # path -> Location (302)
        self.ignore_range = False                # answer 200 with the whole body
        self.requests: list[tuple[str, str]] = []  # (method, path), every request in order
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
        start, end, status = 0, len(data), 200
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if m and not s.ignore_range:
            start, end, status = int(m[1]), min(len(data), int(m[2]) + 1 if m[2] else len(data)), 206
        self.send_response(status)
        self.send_header("Content-Length", str(end - start))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", f'"{hashlib.sha1(data).hexdigest()}"')
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
