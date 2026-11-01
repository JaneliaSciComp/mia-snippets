"""Per-dataset cache for whole-file fetches (SPEC §6.6): work_root/_cache/<record-id>/.

Beside the entries, `files.json` records each URL's validators (the HEAD answer the key was built from, or the
size and sha256 of a file a human dropped), so a later job finds the entry without the network (buffers spec §3).
"""
import json
import os
import re
from pathlib import Path
from typing import Callable

MANIFEST = "files.json"


class DatasetCache:
    """Entries are files named by key (a sha256 hex digest), so names never come from URLs or members.

    Jobs running reviewed code write `shared`; the others read it and write `private` instead.
    """

    def __init__(self, shared: Path, private: Path, writable: bool):
        self.shared, self.private, self.writable = Path(shared), Path(private), writable

    def get(self, key: str, check: Callable[[Path], bool] | None = None) -> Path | None:
        """Shared first, then private; `check` (e.g. a CRC32) skips entries that fail it."""
        for d in (self.shared, self.private):
            p = d / _checked(key)
            if p.is_file() and (check is None or check(p)):
                return p
        return None

    def put(self, key: str, write: Callable[[Path], None]) -> Path:
        """`write` fills a temp file next to the entry, which is then renamed into place."""
        dest = (self.shared if self.writable else self.private) / _checked(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".tmp-{key}-{os.getpid()}")   # created by `write`, so it gets umask permissions
        try:
            write(tmp)
            os.replace(tmp, dest)
            return dest
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


    def manifest_get(self, url: str) -> dict | None:
        """The validators files.json records for `url` (shared first, then private), or None."""
        for d in (self.shared, self.private):
            entry = self._manifest(d).get(url)
            if isinstance(entry, dict) and isinstance(entry.get("validators"), dict):
                return entry["validators"]
        return None

    def manifest_put(self, url: str, validators: dict, source: str) -> None:
        """Record `url`'s validators in the writable cache's files.json (atomic rewrite)."""
        d = self.shared if self.writable else self.private
        d.mkdir(parents=True, exist_ok=True)
        merged = {**self._manifest(d), url: {"validators": validators, "source": source}}
        tmp = d / f".{MANIFEST}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, d / MANIFEST)

    @staticmethod
    def _manifest(d: Path) -> dict:
        try:
            data = json.loads((d / MANIFEST).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def _checked(key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError(f"cache key must be a sha256 hex digest, got {key!r}")
    return key
