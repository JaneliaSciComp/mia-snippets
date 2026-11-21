"""The meter: per host and clock minute, what a harness process asked of a host and what came back."""
import json
import re
import threading
import time

from mia_harness.meter import Meter, failed


class Clock:
    def __init__(self, now=6000.0):        # 6000 s is the start of minute 100
        self.now = now

    def __call__(self):
        return self.now


def rows(directory):
    paths = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    return [json.loads(line) for path in paths for line in path.read_text().splitlines()]


def test_a_minute_holds_what_happened_in_it(tmp_path):
    clock = Clock()
    m = Meter(tmp_path / "t", clock, timer=False)
    m.sent("a.org")
    clock.now += 10
    m.received("a.org", 500)
    clock.now += 10
    m.ended("a.org", "206")
    m.sent("a.org")
    m.sent("a.org")
    clock.now += 5
    m.ended("a.org", "503")
    m.ended("a.org", "timeout")
    m.waited("a.org", 7, asked=True)
    m.waited("a.org", 4, asked=False)
    m.close()
    assert rows(tmp_path / "t") == [{
        "minute": 100, "host": "a.org", "requests": 3, "connection_seconds": 30.0, "peak": 2, "bytes": 500,
        "answers": {"206": 1, "503": 1, "timeout": 1}, "recovered": {}, "final": {},
        "asked_wait_seconds": 7.0, "backoff_seconds": 4.0}]


def test_an_attempt_open_across_a_boundary_gives_each_minute_its_seconds(tmp_path):
    clock = Clock(6050.0)
    m = Meter(tmp_path / "t", clock, timer=False)
    m.sent("a.org")
    clock.now = 6130.0
    m.tick()                                   # minutes 100 and 101 are on disk while the attempt is open
    written = rows(tmp_path / "t")
    assert [(r["minute"], r["requests"], r["connection_seconds"], r["peak"], r["answers"]) for r in written] == [
        (100, 1, 10.0, 1, {}), (101, 0, 60.0, 1, {})]
    m.received("a.org", 9)
    m.ended("a.org", "200")
    m.close()
    after = rows(tmp_path / "t")
    assert after[:2] == written                # a written minute is never changed
    assert (after[2]["minute"], after[2]["requests"], after[2]["connection_seconds"], after[2]["bytes"],
            after[2]["answers"]) == (102, 0, 10.0, 9, {"200": 1})


def test_recovered_and_final_are_written_in_the_minute_the_request_ends(tmp_path):
    clock = Clock()
    m = Meter(tmp_path / "t", clock, timer=False)
    m.sent("a.org")
    m.ended("a.org", "403")
    clock.now += 60
    m.sent("a.org")
    m.ended("a.org", "503")
    clock.now += 60
    m.sent("a.org")
    m.ended("a.org", "200")
    m.settled([("a.org", "403"), ("a.org", "503")], served=True)
    m.settled([("b.org", "dropped")], served=False)
    m.close()
    by = {(r["minute"], r["host"]): r for r in rows(tmp_path / "t")}
    assert by[(100, "a.org")]["answers"] == {"403": 1} and by[(100, "a.org")]["recovered"] == {}
    assert by[(102, "a.org")]["recovered"] == {"403": 1, "503": 1}
    assert by[(102, "b.org")]["final"] == {"dropped": 1} and by[(102, "b.org")]["requests"] == 0


def test_the_clock_going_back_changes_no_written_minute(tmp_path):
    clock = Clock()
    m = Meter(tmp_path / "t", clock, timer=False)
    m.sent("a.org")
    m.ended("a.org", "200")
    clock.now = 6060.0
    m.tick()
    clock.now = 5990.0                         # back into minute 99
    m.sent("a.org")
    m.ended("a.org", "200")
    m.close()
    assert [r["minute"] for r in rows(tmp_path / "t")] == [100, 101]


def test_a_directory_that_cannot_be_written_harms_nothing(tmp_path, caplog):
    (tmp_path / "t").write_text("a file where the directory should be")
    m = Meter(tmp_path / "t", Clock(), timer=False)
    m.sent("a.org")
    m.ended("a.org", "200")
    m.close()
    m.close()
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_the_timer_writes_a_closed_minute_while_the_process_runs(tmp_path):
    clock = Clock(6059.9)
    m = Meter(tmp_path / "t", clock)           # its timer first waits 0.1 s
    m.sent("a.org")
    m.ended("a.org", "200")
    clock.now = 6061.0
    deadline = time.time() + 5
    while not rows(tmp_path / "t") and time.time() < deadline:
        time.sleep(0.05)
    assert [r["minute"] for r in rows(tmp_path / "t")] == [100]
    m.close()


def test_a_meter_without_a_directory_counts_nothing(tmp_path):
    threads = threading.active_count()
    m = Meter()
    m.sent("a.org")
    m.received("a.org", 5)
    m.ended("a.org", "200")
    m.waited("a.org", 1, asked=True)
    m.settled([("a.org", "503")], served=True)
    m.tick()
    m.close()
    assert threading.active_count() == threads
    assert list(tmp_path.iterdir()) == []


def test_each_process_writes_its_own_file(tmp_path):
    for _ in range(2):
        m = Meter(tmp_path / "t", Clock(), timer=False)
        m.sent("a.org")
        m.close()
        time.sleep(0.01)                       # the file name holds the start in milliseconds
    names = [path.name for path in (tmp_path / "t").glob("*.jsonl")]
    assert len(names) == 2 and all(re.fullmatch(r"\d+-\d+\.jsonl", name) for name in names)


def test_failed_is_any_outcome_but_2xx_and_3xx():
    assert not any(failed(outcome) for outcome in ("200", "206", "302"))
    assert all(failed(outcome) for outcome in ("403", "503", "timeout", "truncated"))
