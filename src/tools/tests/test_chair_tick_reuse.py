"""A second chair tick over an unchanged workspace re-reads no work file, decompose run or store table.

The counters wrap the leaf readers (file bodies, run-record parses, the store's full-read processes), never the cached
wrappers, so they see exactly what the caches are meant to save. Nothing here asserts on time.

Known residual: the idle-stall reader chain showed no unchanged-source read through these counters on the second tick.
Sources the counters do not wrap (the hosts table, the courier bus, run pid files, `gh` checks) are not covered here.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools import chair_decompose_runs, chair_facts, chair_read_cache, cli, run_store

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
PER_INTAKE_FIXED = 2  # the exit rows and the task count are cached beside each run's log facts
INTAKES = {"alpha": ["alpha-1", "alpha-2"], "beta": ["beta-1"]}


class Counts(dict):
    def __missing__(self, key: str) -> int:
        return 0


@pytest.fixture
def counts() -> Counts:
    return Counts()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT, ended_at TEXT, status TEXT)")
    for intake, run_ids in INTAKES.items():
        _write(tmp_path / f"{intake}.md", f"---\nid: {intake}\n---\nbody\n")
        _write(tmp_path / "work" / intake / "initiative.md", f"---\nintake: {intake}.md\n---\n")
        (tmp_path / "work" / intake / "p1").mkdir()
        for n, run_id in enumerate(run_ids):
            conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?)", (run_id, f"2026-11-0{n + 1}T00:00:00Z", f"2026-11-0{n + 1}T01:00:00Z", "ok"))
            _write(runs / f"{run_id}.log", f"recorded {run_id}: 0 auto-applied, 0 gated decision(s), 0 proposal(s)\n")
    for n in range(3):  # tasks in a third initiative, so the board is not only empty initiatives
        _write(tmp_path / "work" / "busy" / "p1" / f"t{n}.md", f"---\nid: t{n}\nstate: todo\nneeds: []\n---\n\nBody\n")
    _write(tmp_path / "work" / "busy" / "initiative.md", "---\nid: busy\n---\n")
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture
def made(monkeypatch) -> list[chair_read_cache.StampCache]:
    """Every cache the deps builder creates, in creation order."""
    seen: list[chair_read_cache.StampCache] = []
    real = chair_read_cache.StampCache

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, max_entries: int) -> None:
            super().__init__(max_entries)
            seen.append(self)

    monkeypatch.setattr(cli.chair_read_cache, "StampCache", Recording)
    return seen


@pytest.fixture
def deps(monkeypatch, ws: Path, counts: Counts, made):
    runs = ws / "runs"
    monkeypatch.setattr(run_store, "_store_url", lambda runs_dir: f"sqlite:///{runs / 'cox.db'}")
    monkeypatch.setattr(run_store, "_harness_python", lambda: Path(sys.executable))
    monkeypatch.setattr(cli, "_sync_filed_items", lambda *_a, **_k: None)

    def run_argv(argv: list[str]) -> SimpleNamespace:
        probe = "sha256" in argv[2]
        counts["work_items_probe" if probe else "work_items_read"] += 1
        return SimpleNamespace(returncode=0, stdout='[0, null, "digest"]' if probe else "")

    def queue_run(_runs_dir: Path, build) -> SimpleNamespace:
        argv = build(sys.executable, "sqlite:///unused")
        probe = "sha256" in argv[2] if argv[1] == "-c" else False
        counts["queue_probe" if probe else "queue_read"] += 1
        return SimpleNamespace(returncode=0, stdout='[0, null, "digest"]' if probe else "")

    real_read_text = Path.read_text

    def read_text(self: Path, *args, **kwargs) -> str:
        if (ws / "work") in self.parents:
            counts["work_file_read"] += 1
        return real_read_text(self, *args, **kwargs)

    real_parse, real_exits = chair_decompose_runs._parse_log, chair_decompose_runs.exit_rows
    monkeypatch.setattr(run_store, "_run_argv", run_argv)
    monkeypatch.setattr(run_store, "_queue_run", queue_run)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(chair_decompose_runs, "_parse_log", lambda *a: counts.update(run_parse=counts["run_parse"] + 1) or real_parse(*a))
    monkeypatch.setattr(chair_decompose_runs, "exit_rows", lambda *a: counts.update(exit_rows=counts["exit_rows"] + 1) or real_exits(*a))
    return cli._chair_run_deps(runs, {}, "chair", 1, "h", True, lambda _line: None, ws / "profile.yaml", "store")


def _tick(deps) -> chair_facts.Facts:
    deps.beat()  # clears the per-tick reads only; the process-lifetime caches stay
    return chair_facts.gather_facts(deps.facts_deps, NOW)


FULL_READS = ("work_file_read", "run_parse", "exit_rows", "work_items_read", "queue_read")


def test_the_second_gather_reads_no_body_no_run_record_and_no_full_store_table(deps, counts) -> None:
    first = _tick(deps)
    after_first = {key: counts[key] for key in FULL_READS}
    second = _tick(deps)
    assert all(after_first[key] > 0 for key in FULL_READS), after_first
    assert {key: counts[key] - after_first[key] for key in FULL_READS} == dict.fromkeys(FULL_READS, 0)
    # `tuning` reads the real clock for the week's elapsed fraction, so two gathers differ there and nowhere else
    assert {k: v for k, v in first.items() if k != "tuning"} == {k: v for k, v in second.items() if k != "tuning"}


def test_a_changed_work_file_is_read_again_and_only_that_one(deps, counts, ws: Path) -> None:
    _tick(deps)
    before = counts["work_file_read"]
    path = ws / "work" / "busy" / "p1" / "t0.md"
    path.write_text("---\nid: t0\nstate: todo\nneeds: []\n---\n\nA longer body\n", encoding="utf-8")
    _tick(deps)
    assert counts["work_file_read"] - before == 1


def test_cache_entries_follow_the_files_and_runs_that_remain(deps, made, ws: Path) -> None:
    _tick(deps)
    for task in ("t1", "t2"):
        (ws / "work" / "busy" / "p1" / f"{task}.md").unlink()
    (ws / "runs" / "alpha-1.log").unlink()
    conn = sqlite3.connect(ws / "runs" / "cox.db")
    conn.execute("DELETE FROM runs WHERE run_id = 'alpha-1'")
    conn.execute("DELETE FROM runs WHERE run_id = 'beta-1'")
    conn.commit()
    conn.close()
    (ws / "runs" / "beta-1.log").unlink()
    for _ in range(3):
        _tick(deps)
    files = next(c for c in made if c.max_entries == cli.CHAIR_FILES_CACHE_MAX)
    decompose = {len(c) for c in made if c.max_entries == cli.CHAIR_DECOMPOSE_CACHE_MAX}
    remaining_files = len(list((ws / "work").glob("*/*/*.md")) + list((ws / "work").glob("*/initiative.md")))
    assert len(files) == remaining_files
    assert decompose == {PER_INTAKE_FIXED + 1, PER_INTAKE_FIXED}  # alpha keeps one run; beta keeps none


def test_the_bounds_are_set_from_the_work_cached(made, deps) -> None:
    assert sorted(c.max_entries for c in made if c.max_entries != cli.CHAIR_DECOMPOSE_CACHE_MAX) == [
        cli.CHAIR_STORE_CACHE_MAX, cli.CHAIR_STORE_CACHE_MAX, cli.CHAIR_FILES_CACHE_MAX,
    ]
    assert cli.CHAIR_FILES_CACHE_MAX >= 2000 and cli.CHAIR_DECOMPOSE_CACHE_MAX >= 200


def test_a_reader_without_a_cache_parameter_is_called_exactly_as_before() -> None:
    assert cli._takes_cache(lambda runs_dir: [{"seen": runs_dir}], object())("r") == [{"seen": "r"}]


def test_a_reader_with_a_cache_parameter_receives_the_cache() -> None:
    cache = object()
    read = lambda runs_dir, initiative=None, kind=None, cache=None: [{"cache": cache}]  # noqa: E731
    assert cli._takes_cache(read, cache)("r") == [{"cache": cache}]
