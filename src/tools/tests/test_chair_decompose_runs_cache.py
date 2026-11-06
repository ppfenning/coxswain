import os
from pathlib import Path

import pytest

from agent_tools import chair_decompose_runs as mod
from agent_tools.chair_decompose_streak import DecomposeRun
from agent_tools.chair_read_cache import StampCache

OLD = "chair: plan approved but not executed: old"
NEW = "chair: plan approved but not executed: new"


@pytest.fixture
def parses(tmp_path, monkeypatch):
    mod._REFUSALS.clear()
    calls: list[str] = []
    real = mod.refusal_line

    def counting(log: str) -> str | None:
        if log:  # parse_run itself scans the empty string when the edge hands it no log
            calls.append(log)
        return real(log)

    monkeypatch.setattr(mod, "refusal_line", counting)
    monkeypatch.setattr(mod, "exit_rows", lambda runs_dir: [{"run_id": "idea-1", "launched_at": "1"}])
    monkeypatch.setattr(mod, "row_exited", lambda row: True)
    monkeypatch.setattr(mod, "_task_items", lambda runs_dir, initiative: 0)
    yield calls
    mod._REFUSALS.clear()


def test_unchanged_log_parses_once_and_a_new_mtime_parses_again(tmp_path, parses):
    log = tmp_path / "idea-1.log"
    log.write_text(f"{OLD}\n")
    os.utime(log, ns=(1_000_000_000, 1_000_000_000))

    first = mod.read_decompose_runs(tmp_path, "idea")
    second = mod.read_decompose_runs(tmp_path, "idea")
    assert first == second == [DecomposeRun("idea-1", False, OLD)]
    assert parses == [f"{OLD}\n"]

    log.write_text(f"{NEW}\n")
    os.utime(log, ns=(2_000_000_000, 2_000_000_000))
    assert mod.read_decompose_runs(tmp_path, "idea") == [DecomposeRun("idea-1", False, NEW)]
    assert parses == [f"{OLD}\n", f"{NEW}\n"]


def test_missing_log_reads_as_no_refusal_and_is_not_parsed(tmp_path, parses):
    assert mod.read_decompose_runs(tmp_path, "idea") == [DecomposeRun("idea-1", False, None)]
    assert parses == []


ZERO = "recorded idea-1: 0 auto-applied, 0 gated decision(s), 0 proposal(s)\n"
TWO = "recorded idea-1: 1 auto-applied, 0 gated decision(s), 2 proposal(s)\n"
EXITED = {"run_id": "idea-1", "launched_at": "1", "ended_at": "2"}
RUNNING = {"run_id": "idea-2", "launched_at": "3"}


class Store:
    """A stand-in run store: rows and a task count the test edits, plus a count of every read."""

    def __init__(self, root: Path) -> None:
        self.root, self.rows, self.items, self.tick = root, [], 3, 0
        self.reads = {"exit_rows": 0, "task_items": 0, "logs": 0}

    def write(self, path: Path, text: str) -> None:
        self.tick += 1
        path.write_text(text)
        os.utime(path, ns=(self.tick * 1_000_000_000, self.tick * 1_000_000_000))

    def commit(self) -> None:
        self.write(self.root / "cox.db", "x" * self.tick)  # a new stamp: the store changed


@pytest.fixture
def store(tmp_path, monkeypatch):
    mod._REFUSALS.clear()
    s = Store(tmp_path)
    s.commit()

    def exit_rows(runs_dir):
        s.reads["exit_rows"] += 1
        return [dict(r) for r in s.rows]

    def task_items(runs_dir, initiative):
        s.reads["task_items"] += 1
        return s.items

    real = Path.read_text

    def read_text(self, *args, **kwargs):
        s.reads["logs"] += self.suffix == ".log"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(mod.run_store, "_harness_python", lambda: tmp_path)
    monkeypatch.setattr(mod, "_store_files", lambda runs_dir: [tmp_path / "cox.db"])
    monkeypatch.setattr(mod, "exit_rows", exit_rows)
    monkeypatch.setattr(mod, "_task_items", task_items)
    monkeypatch.setattr(Path, "read_text", read_text)
    yield s
    mod._REFUSALS.clear()


def test_second_call_over_unchanged_files_reads_nothing(store):
    store.rows = [EXITED, RUNNING]
    store.write(store.root / "idea-1.log", ZERO)
    store.write(store.root / "idea-2.log", f"{OLD}\n")
    cache = StampCache(16)

    first = mod.read_decompose_runs(store.root, "idea", cache)
    assert store.reads == {"exit_rows": 1, "task_items": 1, "logs": 2}
    assert mod.read_decompose_runs(store.root, "idea", cache) == first
    assert store.reads == {"exit_rows": 1, "task_items": 1, "logs": 2}


def test_log_rewritten_by_a_running_decompose_is_read_again(store):
    store.rows = [EXITED, RUNNING]
    store.write(store.root / "idea-1.log", ZERO)
    store.write(store.root / "idea-2.log", f"{OLD}\n")
    cache = StampCache(16)
    assert mod.read_decompose_runs(store.root, "idea", cache) == [
        DecomposeRun("idea-1", False, None),
        DecomposeRun("idea-2", True, OLD),
    ]

    store.write(store.root / "idea-1.log", TWO + "more\n")
    store.write(store.root / "idea-2.log", f"{NEW}\n")
    after = mod.read_decompose_runs(store.root, "idea", cache)
    assert after == [DecomposeRun("idea-1", True, None), DecomposeRun("idea-2", True, NEW)]
    assert store.reads == {"exit_rows": 1, "task_items": 1, "logs": 4}
    assert after == mod.read_decompose_runs(store.root, "idea")


def test_store_change_rereads_rows_and_items(store):
    store.rows = [EXITED]
    cache = StampCache(16)
    assert mod.read_decompose_runs(store.root, "idea", cache) == [DecomposeRun("idea-1", True, None)]
    store.items = 0
    store.commit()
    assert mod.read_decompose_runs(store.root, "idea", cache) == [DecomposeRun("idea-1", False, None)]
    assert store.reads["exit_rows"] == store.reads["task_items"] == 2


def test_cached_result_equals_uncached_with_missing_logs_and_other_intakes(store):
    store.rows = [EXITED, RUNNING, {"run_id": "other-1", "launched_at": "1", "ended_at": "2"}]
    store.write(store.root / "idea-1.log", f"{OLD}\n{ZERO}")
    cache = StampCache(16)
    uncached = mod.read_decompose_runs(store.root, "idea")
    assert mod.read_decompose_runs(store.root, "idea", cache) == uncached
    assert mod.read_decompose_runs(store.root, "idea", cache) == uncached
    assert uncached == [DecomposeRun("idea-1", False, OLD), DecomposeRun("idea-2", True, None)]


def test_a_pruned_run_leaves_the_cache(store):
    store.rows = [EXITED, RUNNING]
    store.write(store.root / "idea-1.log", ZERO)
    store.write(store.root / "idea-2.log", f"{OLD}\n")
    cache = StampCache(16)
    mod.read_decompose_runs(store.root, "idea", cache)
    assert len(cache) == 4

    store.rows = [EXITED]
    store.commit()
    mod.read_decompose_runs(store.root, "idea", cache)
    assert len(cache) == 3
    store.rows = [EXITED, RUNNING]
    store.commit()
    before = store.reads["logs"]
    mod.read_decompose_runs(store.root, "idea", cache)
    assert store.reads["logs"] == before + 1  # idea-2's log was dropped, idea-1's was kept


def test_a_store_with_no_file_is_never_cached(store):
    store.rows = [EXITED]
    (store.root / "cox.db").unlink()
    cache = StampCache(16)
    mod.read_decompose_runs(store.root, "idea", cache)
    mod.read_decompose_runs(store.root, "idea", cache)
    assert store.reads["exit_rows"] == store.reads["task_items"] == 2
