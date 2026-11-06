import json
import sqlite3
import subprocess
import sys

import pytest

from agent_tools import run_store
from agent_tools.chair_read_cache import StampCache

ROWS = [{"initiative": "i1", "task_id": "t1", "state": "open", "needs": ["a"]}]


class FakeRunner:
    """Stands in for subprocess.run: a probe prints `stamp`, a full read prints `rows`, every call is recorded."""

    def __init__(self):
        self.stamp = [1, "2026-11-06T01:00:00Z"]
        self.rows = ROWS
        self.probe_returncode = 0
        self.probe_stdout = None
        self.calls = []

    def __call__(self, argv, **kwargs):
        probe = argv[1] == "-c" and "hashlib" in argv[2]
        self.calls.append(("probe" if probe else "full", argv))
        if probe:
            out = json.dumps(self.stamp) if self.probe_stdout is None else self.probe_stdout
            return subprocess.CompletedProcess(argv, self.probe_returncode, out, "")
        return subprocess.CompletedProcess(argv, 0, "".join(json.dumps(r) + "\n" for r in self.rows), "")

    def count(self, which):
        return sum(1 for name, _ in self.calls if name == which)


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(run_store, "_harness_python", lambda: "/h/python")
    monkeypatch.setattr(run_store, "_store_url", lambda runs_dir: "sqlite:////s/cox.db")
    monkeypatch.setattr(run_store.subprocess, "run", fake)
    return fake


READERS = {
    "work_items": lambda cache, **kw: run_store.work_items("/runs", cache=cache, **kw),
    "queue": lambda cache, **kw: run_store.read_queue("/runs", cache=cache, **kw),
}


@pytest.fixture(params=sorted(READERS))
def read(request):
    return READERS[request.param]


def test_an_unchanged_stamp_skips_the_full_read(runner, read):
    cache = StampCache(4)
    first, second = read(cache), read(cache)
    assert (runner.count("probe"), runner.count("full")) == (2, 1)
    assert first == second


def test_a_changed_greatest_updated_at_reads_again(runner, read):
    cache = StampCache(4)
    read(cache)
    runner.stamp = [1, "2026-11-06T02:00:00Z"]
    read(cache)
    assert runner.count("full") == 2


def test_a_changed_count_reads_again(runner, read):
    cache = StampCache(4)
    read(cache)
    runner.stamp = [2, "2026-11-06T01:00:00Z"]
    read(cache)
    assert runner.count("full") == 2


@pytest.mark.parametrize("failure", [{"probe_returncode": 1}, {"probe_stdout": ""}, {"probe_stdout": "not json"}])
def test_a_failed_probe_reads_in_full_and_stores_nothing(runner, read, failure):
    for name, value in failure.items():
        setattr(runner, name, value)
    cache = StampCache(4)
    assert read(cache) == read(cache)
    assert (runner.count("full"), len(cache)) == (2, 0)


def test_a_probe_that_cannot_launch_reads_in_full(monkeypatch, runner, read):
    def launch(argv, **kwargs):
        if argv[1] == "-c" and "hashlib" in argv[2]:
            raise OSError("no such file")
        return runner(argv, **kwargs)

    monkeypatch.setattr(run_store.subprocess, "run", launch)
    cache = StampCache(4)
    assert [r["task_id"] for r in read(cache)] == ["t1"]
    assert len(cache) == 0


def test_a_failed_full_read_is_not_stored_under_a_good_stamp(monkeypatch, runner, read):
    ok = runner

    def failing(argv, **kwargs):
        done = ok(argv, **kwargs)
        return done if argv[1] == "-c" and "hashlib" in argv[2] else subprocess.CompletedProcess(argv, 2, "", "boom")

    monkeypatch.setattr(run_store.subprocess, "run", failing)
    cache = StampCache(4)
    assert read(cache) == []
    assert len(cache) == 0
    monkeypatch.setattr(run_store.subprocess, "run", ok)
    assert [r["task_id"] for r in read(cache)] == ["t1"]


def test_a_caller_that_mutates_a_row_cannot_change_the_stored_rows(runner, read):
    cache = StampCache(4)
    first = read(cache)
    first[0]["state"] = "poisoned"
    first[0]["needs"].append("b")
    first.append({"extra": 1})
    again = read(cache)
    assert runner.count("full") == 1
    assert [r["state"] for r in again] == ["open"]
    assert again[0]["needs"] == ["a"]


def test_without_a_cache_no_probe_runs(runner, read):
    read(None)
    assert (runner.count("probe"), runner.count("full")) == (0, 1)


def test_the_work_items_key_includes_the_initiative(runner):
    cache = StampCache(4)
    run_store.work_items("/runs", "i1", cache=cache)
    run_store.work_items("/runs", "i2", cache=cache)
    run_store.work_items("/runs", "i1", cache=cache)
    assert runner.count("full") == 2
    assert len(cache) == 2


def test_the_queue_key_includes_the_initiative_and_the_kind(runner):
    cache = StampCache(8)
    for initiative, kind in [(None, None), ("i1", None), (None, "task"), ("i1", "task"), ("i1", "task")]:
        run_store.read_queue("/runs", initiative, kind, cache=cache)
    assert runner.count("full") == 4
    assert len(cache) == 4


def test_the_two_readers_do_not_share_an_entry(runner):
    cache = StampCache(4)
    run_store.work_items("/runs", cache=cache)
    run_store.read_queue("/runs", cache=cache)
    assert runner.count("full") == 2


def test_the_stamp_probe_binds_filters_as_arguments_not_sql():
    argv = run_store._queue_stamp_argv("py", "sqlite:///x", "i'; DROP TABLE work_items; --", "task")
    assert argv[-2:] == ["i'; DROP TABLE work_items; --", "task"]
    assert "DROP" not in argv[2]
    assert "('initiative', 'kind')" in argv[2]
    assert "('kind',)" in run_store._queue_stamp_argv("py", "u", None, "task")[2]
    assert "name in ()" in run_store._work_items_stamp_argv("py", "u", None)[2]


@pytest.mark.parametrize(
    ("stdout", "expected_count"),
    [
        ('[3, "2026-11-06"]', 3),
        ("[0, null]", 0),
        ("[]", None),
        ("", None),
        ("garbage", None),
        ('{"n": 1}', None),
        ('["3", "x"]', None),
        ("[true, 1]", None),
    ],
)
def test_stamp_from_reads_a_count_led_json_list(stdout, expected_count):
    stamp = run_store._stamp_from(stdout)
    assert (None if stamp is None else stamp[0]) == expected_count


def test_stamp_from_differs_when_only_the_aggregate_differs():
    assert run_store._stamp_from('[1, "a"]') != run_store._stamp_from('[1, "b"]')
    assert run_store._stamp_from('[1, "a"]') == run_store._stamp_from('[1, "a"]')


def _db(tmp_path):
    path = tmp_path / "cox.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE work_items (initiative TEXT, task_id TEXT, kind TEXT, updated_at TEXT, epoch INTEGER, holder TEXT, expires_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("i1", "t1", "task", "2026-11-06T01", 0, None, None),
            ("i1", "t2", "epic", "2026-11-06T03", 2, "me", "2026-11-06T09"),
            ("i2", "t1", "task", "2026-11-06T02", 0, None, None),
        ],
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


def _probe(argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_probe_scripts_run_against_a_real_sqlite_store(tmp_path):
    url = _db(tmp_path)
    assert _probe(run_store._work_items_stamp_argv(sys.executable, url, None))[:2] == [3, "2026-11-06T03"]
    assert _probe(run_store._work_items_stamp_argv(sys.executable, url, "i2"))[:2] == [1, "2026-11-06T02"]
    assert _probe(run_store._queue_stamp_argv(sys.executable, url, "i1", "task"))[:2] == [1, "2026-11-06T01"]
    assert _probe(run_store._queue_stamp_argv(sys.executable, url, None, None))[:2] == [3, "2026-11-06T03"]


@pytest.mark.parametrize("reader", ["work_items", "queue"])
def test_a_state_change_on_a_non_newest_row_at_the_same_updated_at_changes_the_stamp(tmp_path, reader):
    url = _db(tmp_path)
    argv = (
        run_store._work_items_stamp_argv(sys.executable, url, None)
        if reader == "work_items"
        else run_store._queue_stamp_argv(sys.executable, url, None, None)
    )
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))
    conn.execute("ALTER TABLE work_items ADD COLUMN state TEXT")
    conn.commit()
    before = _probe(argv)
    conn.execute("UPDATE work_items SET state = 'done' WHERE initiative = 'i1' AND task_id = 't1'")
    conn.commit()
    conn.close()
    after = _probe(argv)
    assert before[:2] == after[:2]
    assert run_store._stamp_from(json.dumps(before)) != run_store._stamp_from(json.dumps(after))


def test_a_claim_or_release_changes_the_queue_stamp_though_updated_at_stays(tmp_path):
    url = _db(tmp_path)
    before = run_store._stamp_from(json.dumps(_probe(run_store._queue_stamp_argv(sys.executable, url, None, None))))
    path = url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE work_items SET holder = NULL, expires_at = NULL WHERE task_id = 't2'")
    conn.commit()
    conn.close()
    after = run_store._stamp_from(json.dumps(_probe(run_store._queue_stamp_argv(sys.executable, url, None, None))))
    assert before != after
