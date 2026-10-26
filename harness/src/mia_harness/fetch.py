"""`harness fetch`: every URL of a binding into the raw cache, with a per-file outcome report (fetch.json)."""
from pathlib import Path

from .api import BudgetExceeded, HostNotAllowed, NeedsFetch
from .binding import load_binding
from .io import IO


def fetch(binding_path: Path, record_path: Path, io: IO) -> dict:
    """Fetch each URL once, in binding order. `record_path` is accepted for symmetry with `stage`; unused."""
    rep: dict = {"ok": False, "partial": False, "bytes_fetched": 0, "files": [], "errors": []}
    try:
        binding = load_binding(binding_path)
        urls = list(dict.fromkeys(u for b in [*binding.raw, *binding.labels] for u in b.urls))
        for i, url in enumerate(urls):
            before = io.bytes_fetched
            try:
                path = io.fetch(url)
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
                rep["files"].append({"url": url, "outcome": outcome, "bytes": path.stat().st_size, "key": path.name})
        rep["ok"] = "host_not_allowed" not in rep and all(f["outcome"] != "failed" for f in rep["files"])
    except Exception as e:      # an unreadable binding, or anything unforeseen: the report says what
        rep["ok"] = False
        rep["errors"].append(f"{type(e).__name__}: {e}")
    rep["bytes_fetched"] = io.bytes_fetched
    return rep
