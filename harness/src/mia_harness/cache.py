"""Per-dataset cache for whole-file fetches (SPEC §6.6): work_root/_cache/<record-id>/."""
import os
import re
from pathlib import Path
from typing import Callable


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


def _checked(key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError(f"cache key must be a sha256 hex digest, got {key!r}")
    return key
