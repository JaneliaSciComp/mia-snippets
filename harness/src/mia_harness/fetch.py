"""`harness fetch`: every URL of a binding into the raw cache, with a per-file outcome report (fetch.json).

A `<zip>::<glob>` url is expanded against the zip's directory and every matching member fetched; the url is one
report entry, and once all its members are in, files.json records it (`note_fetched`), which is what tells a
worker the members are cached rather than merely the zip's validators known."""
from fnmatch import fnmatchcase
from pathlib import Path

from .api import BudgetExceeded, HostNotAllowed, NeedsFetch
from .binding import load_binding
from .io import IO, _split


def fetch(binding_path: Path, record_path: Path, io: IO) -> dict:
    """Fetch each URL once, in binding order. `record_path` is accepted for symmetry with `stage`; unused."""
    rep: dict = {"ok": False, "partial": False, "bytes_fetched": 0, "files": [], "errors": []}
    try:
        binding = load_binding(binding_path)
        urls = list(dict.fromkeys(u for b in [*binding.raw, *binding.labels] for u in b.urls))
        for i, url in enumerate(urls):
            before = io.bytes_fetched
            try:
                size = 0
                for piece in expand(url, io):
                    size += io.fetch(piece).stat().st_size
                io.note_fetched(url)
            except NeedsFetch as e:
                rep["files"].append({"url": url, "outcome": "stream", "reason": str(e)})
            except BudgetExceeded as e:
                rep["files"] += [{"url": u, "outcome": "pending"} for u in urls[i:]]
                rep["partial"] = True
                rep["errors"].append(str(e))
                break
            except HostNotAllowed as e:
                rep["host_not_allowed"] = {"host": e.host, "chain": e.chain}
                rep["errors"].append(str(e))
                break
            except Exception as e:      # one bad URL does not stop the others
                rep["files"].append({"url": url, "outcome": "failed", "reason": f"{type(e).__name__}: {e}"})
            else:
                outcome = "cached" if io.bytes_fetched == before else "fetched"
                rep["files"].append({"url": url, "outcome": outcome, "bytes": size})
        rep["ok"] = "host_not_allowed" not in rep and all(f["outcome"] != "failed" for f in rep["files"])
    except Exception as e:      # an unreadable binding, or anything unforeseen: the report says what
        rep["ok"] = False
        rep["errors"].append(f"{type(e).__name__}: {e}")
    rep["bytes_fetched"] = io.bytes_fetched
    return rep


GLOB_CHARS = "*?["


def expand(url: str, io: IO) -> list[str]:
    """A `<zip>::<glob>` url as its matching member urls, sorted by name (the snippets expand the same way);
    any other url as itself."""
    zip_url, member = _split(url)
    if member is None or not any(ch in member for ch in GLOB_CHARS):
        return [url]
    names = sorted(n for n in io.list(zip_url) if fnmatchcase(n, member))
    if not names:
        raise ValueError(f"no member of {zip_url} matches {member!r}")
    return [f"{zip_url}::{n}" for n in names]
