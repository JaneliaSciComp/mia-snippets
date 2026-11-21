"""A meter of what a harness process asks of each host and what comes back, per host and clock minute."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


def failed(outcome: str) -> bool:
    """Whether an attempt's outcome is a failure: anything but a 2xx or 3xx status text."""
    return outcome[:1] not in ("2", "3") or not outcome.isdigit()


def _empty_minute() -> dict:
    return {"requests": 0, "connection_seconds": 0.0, "peak": 0, "bytes": 0, "answers": Counter(),
            "recovered": Counter(), "final": Counter(), "asked_wait_seconds": 0.0, "backoff_seconds": 0.0}


class Meter:
    """Counts attempts, open time, bytes, outcomes and waits per host and minute, and appends each ended minute
    to `<directory>/<pid>-<start in ms>.jsonl`. A written minute is never changed. With no directory it does nothing."""

    def __init__(self, directory: Path | None = None, clock: Callable[[], float] = time.time, timer: bool = True):
        self.directory = directory
        self._clock = clock
        self._lock = threading.Lock()
        self._minutes: dict[int, dict[str, dict]] = {}   # minute -> host -> fields
        self._open: dict[str, int] = {}                  # host -> attempts open now
        self._integrated: dict[str, float] = {}          # host -> time up to which open time is counted
        self._written = -1 << 62                         # the last minute written (or passed over by a tick)
        self._file = None
        self._broken = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._name = f"{os.getpid()}-{int(time.time() * 1000)}.jsonl"
        if directory is not None and timer:
            self._thread = threading.Thread(target=self._run, name="meter", daemon=True)
            self._thread.start()

    def sent(self, host: str) -> None:
        """An attempt goes out to the host: one more request, one more open."""
        if self.directory is None:
            return
        with self._lock:
            self._open.setdefault(host, 0)
            fields = self._at(host)
            self._open[host] += 1
            fields["requests"] += 1
            fields["peak"] = max(fields["peak"], self._open[host])

    def received(self, host: str, count: int) -> None:
        """Body bytes arrived from the host."""
        if self.directory is None:
            return
        with self._lock:
            self._at(host)["bytes"] += count

    def ended(self, host: str, outcome: str) -> None:
        """An attempt closed with this outcome: one more answer, one fewer open."""
        if self.directory is None:
            return
        with self._lock:
            fields = self._at(host)
            fields["answers"][outcome] += 1
            self._open[host] = max(0, self._open.get(host, 0) - 1)

    def waited(self, host: str, seconds: float, asked: bool) -> None:
        """A sleep is about to begin because of the host: one it named (asked) or one on the job's own schedule."""
        if self.directory is None:
            return
        with self._lock:
            self._at(host)["asked_wait_seconds" if asked else "backoff_seconds"] += seconds

    def settled(self, failures: list[tuple[str, str]], served: bool) -> None:
        """A request ended, served or not: each (host, outcome) of its failed attempts is counted once more."""
        if self.directory is None:
            return
        with self._lock:
            for host, outcome in failures:
                self._at(host)["recovered" if served else "final"][outcome] += 1

    def tick(self) -> None:
        """Write every minute that has ended."""
        if self.directory is None:
            return
        with self._lock:
            self._integrate()
            current = max(int(self._clock() // 60), self._written + 1)
            self._flush(current)
            self._written = current - 1

    def close(self) -> None:
        """Write the open minute too and stop the timer. Harmless a second time."""
        if self.directory is None:
            return
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        with self._lock:
            self._integrate()
            if self._minutes:
                last = max(self._minutes)
                self._flush(last + 1)
                self._written = last
            if self._file is not None:
                self._file.close()
                self._file = None

    def _run(self) -> None:
        while not self._stop.wait(max(0.05, 60 - self._clock() % 60)):
            self.tick()

    def _minute(self) -> int:
        return max(int(self._clock() // 60), self._written + 1)

    def _at(self, host: str) -> dict:
        """The fields of the host in the minute of now, after counting open time up to now."""
        self._integrate()
        return self._minutes.setdefault(self._minute(), {}).setdefault(host, _empty_minute())

    def _integrate(self) -> None:
        """Add open attempts times elapsed seconds to each minute between each host's last count and now."""
        now = self._clock()
        for host, count in self._open.items():
            last = self._integrated.get(host, now)
            self._integrated[host] = max(last, now)
            at = last
            while at < now:
                minute = int(at // 60)
                end = min(now, (minute + 1) * 60)
                if count:
                    fields = self._minutes.setdefault(max(minute, self._written + 1), {}).setdefault(
                        host, _empty_minute())
                    fields["connection_seconds"] += count * (end - at)
                    fields["peak"] = max(fields["peak"], count)
                at = end

    def _flush(self, before: int) -> None:
        """Append every held minute earlier than `before`, oldest first, then forget it."""
        for minute in sorted(m for m in self._minutes if m < before):
            for host, fields in sorted(self._minutes.pop(minute).items()):
                row = {"minute": minute, "host": host, **fields,
                       "connection_seconds": round(fields["connection_seconds"], 3)}
                if any(row[name] for name in row if name not in ("minute", "host")):
                    self._append(row)

    def _append(self, row: dict) -> None:
        if self._broken:
            return
        try:
            if self._file is None:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._file = open(self.directory / self._name, "a", encoding="utf-8")
            self._file.write(json.dumps(row) + "\n")
            self._file.flush()
        except (OSError, ValueError) as error:
            self._broken = True
            log.warning("meter stopped writing to %s: %s", self.directory, error)
