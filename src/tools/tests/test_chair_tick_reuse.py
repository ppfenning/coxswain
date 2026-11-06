"""A second chair tick over an unchanged workspace reads no work-file body, no decompose record and no full store table.

Counts only; no assertion looks at a clock.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import inspect
import json
from pathlib import Path

import pytest

from agent_tools import chair_decompose_runs, chair_facts, cli, run_store

NOW = datetime.datetime(2026, 10, 7, 12, 0, tzinfo=datetime.UTC)
PHASES = [("p1", "t1"), ("p1", "t2"), ("p2", "t3")]
RUNS = ["a-1", "a-2"]


@dataclasses.dataclass
class Done:
    returncode: int
    stdout: str


class Env:
    """A temporary workspace, a fake store and the counters watching them."""

    def __init__(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ws, self.runs = ws, ws / "runs"
        self.version = 1  # bumped by `store_write`; the fake store's probe digest follows it
        self.exit_rows: list[dict] = []
        self.reads: collections.Counter = collections.Counter()
        self._patch(monkeypatch)

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = f"sqlite:///{self.runs / 'cox.db'}"
        monkeypatch.setattr(run_store, "_harness_python", lambda: Path("py"))
        monkeypatch.setattr(run_store, "_store_url", lambda _runs_dir: url)
        monkeypatch.setattr(run_store, "_run_argv", self._work_items_run)
        monkeypatch.setattr(run_store, "_queue_run", self._queue_run)
        real_read_text, real_parse_log, real_task_items = Path.read_text, chair_decompose_runs._parse_log, chair_decompose_runs._task_items
        env = self

        def counting_read_text(path: Path, *a, **k) -> str:
            where = "work_file" if (env.ws / "work") in path.parents else "intake_file" if (env.ws / "intake") in path.parents else None
            if where:
                env.reads[where] += 1
            return real_read_text(path, *a, **k)

        monkeypatch.setattr(Path, "read_text", counting_read_text)
        monkeypatch.setattr(chair_decompose_runs, "_parse_log", lambda *a: env.reads.update(["decompose_log"]) or real_parse_log(*a))
        monkeypatch.setattr(chair_decompose_runs, "_task_items", lambda *a: env.reads.update(["decompose_items"]) or real_task_items(*a))
        monkeypatch.setattr(chair_decompose_runs, "exit_rows", self._exit_rows)

    def _exit_rows(self, _runs_dir: Path) -> list[dict]:
        self.reads["decompose_exits"] += 1
        return list(self.exit_rows)

    def _serve(self, argv: list[str], table: str) -> Done:
        if "hashlib" in " ".join(argv):  # the probe: one JSON line, [row count, greatest updated_at, digest]
            return Done(0, json.dumps([1, "2026-10-06", f"v{self.version}"]))
        self.reads[f"full_{table}"] += 1
        return Done(0, json.dumps({"initiative": "i1", "task_id": "t1", "phase": "p1", "state": "ready", "kind": "task"}) + "\n")

    def _work_items_run(self, argv: list[str]) -> Done:
        return self._serve(argv, "work_items")

    def _queue_run(self, _runs_dir: Path, build) -> Done:
        return self._serve(build("py", "sqlite:///x"), "queue")

    def store_write(self) -> None:
        """A write to the store: the file stamp moves and so does the probe digest."""
        self.version += 1
        with (self.runs / "cox.db").open("ab") as db:
            db.write(b"x")

    def add_run(self, run_id: str) -> None:
        (self.runs / f"{run_id}.log").write_text("ran\n", encoding="utf-8")
        self.exit_rows.append({"run_id": run_id, "launched_at": f"2026-10-06T10:0{run_id[-1]}:00Z", "ended_at": "2026-10-06T11:00:00Z", "status": "done"})
        self.store_write()

    def drop_run(self, run_id: str) -> None:
        (self.runs / f"{run_id}.log").unlink()
        self.exit_rows = [r for r in self.exit_rows if r["run_id"] != run_id]
        self.store_write()

    def work_paths(self) -> list[Path]:
        return sorted([*self.ws.glob("work/*/*/*.md"), *self.ws.glob("work/*/initiative.md")])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "cox.db").write_bytes(b"")
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "a.md").write_text("---\nid: a\n---\nintake body\n", encoding="utf-8")
    # `a` holds no task item in the store, so the chair reads its decompose runs; `i1` holds one.
    for name, front in [("a", "intake: intake/a.md\n"), ("i1", "repo: /r/i1\n")]:
        (tmp_path / "work" / name).mkdir(parents=True)
        (tmp_path / "work" / name / "initiative.md").write_text(f"---\n{front}---\n", encoding="utf-8")
    for phase, task in PHASES:
        path = tmp_path / "work" / "i1" / phase / f"{task}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"---\nid: {task}\nstate: ready\n---\nbody {task}\n", encoding="utf-8")
    made = Env(tmp_path, monkeypatch)
    for run_id in RUNS:
        made.add_run(run_id)
    return made


def _deps(env: Env):
    return cli._chair_run_deps(env.runs, {}, "chair", 1, "h", True, print, env.ws / "p.yaml", "store")


def _nonlocals(fn, name: str):
    return inspect.getclosurevars(fn).nonlocals[name]


def _cache_sizes(deps) -> dict[str, object]:
    facts = deps.facts_deps
    files_cache = _nonlocals(_nonlocals(facts.tickets, "work_files"), "files_cache")
    queue_cache = _nonlocals(facts.work_rows, "queue_cache")
    caches = _nonlocals(_nonlocals(facts.decompose_runs, "decompose_cache_for"), "decompose_caches")
    return {"files": len(files_cache), "queue": len(queue_cache), "decompose": {k: len(c) for k, c in caches.items()}}


def _tick(env: Env, deps) -> tuple[dict, collections.Counter]:
    env.reads.clear()
    deps.beat()
    return chair_facts.gather_facts(deps.facts_deps, NOW), collections.Counter(env.reads)


def test_a_second_tick_over_nothing_changed_reads_no_work_file_decompose_record_or_full_table(env: Env) -> None:
    deps = _deps(env)
    first_facts, first = _tick(env, deps)
    second_facts, second = _tick(env, deps)
    watched = ["work_file", "decompose_log", "decompose_exits", "decompose_items", "full_work_items", "full_queue"]
    assert {k: first[k] > 0 for k in watched} == dict.fromkeys(watched, True)
    assert {k: second[k] for k in watched} == dict.fromkeys(watched, 0)
    assert second_facts == first_facts


def test_a_changed_work_file_and_a_store_write_are_read_again_and_nothing_else(env: Env) -> None:
    deps = _deps(env)
    _tick(env, deps)
    env.reads.clear()
    (env.ws / "work" / "i1" / "p1" / "t1.md").write_text("---\nid: t1\nstate: ready\n---\nbody t1 changed\n", encoding="utf-8")
    env.store_write()
    _, third = _tick(env, deps)
    # the store write invalidates two work_items reads: the tick's store rows, and the decompose item count of `a`
    assert (third["work_file"], third["full_work_items"], third["full_queue"]) == (1, 2, 1)


def test_cache_entries_follow_the_files_and_runs_that_remain(env: Env) -> None:
    deps = _deps(env)
    _tick(env, deps)
    before = _cache_sizes(deps)
    (env.ws / "work" / "i1" / "p2" / "t3.md").unlink()
    (env.ws / "work" / "i1" / "p1" / "t2.md").unlink()
    env.drop_run("a-2")
    for _ in range(3):
        _tick(env, deps)
    after = _cache_sizes(deps)
    # a decompose cache holds the run exit rows and the item count, plus one entry per run log
    assert (before["files"], before["decompose"]) == (len(env.work_paths()) + 2, {"a": 2 + 2})
    assert (after["files"], after["decompose"]) == (len(env.work_paths()), {"a": 2 + 1})
    assert after["queue"] == 1


def test_intake_caches_stop_at_the_cap_and_drop_the_oldest(env: Env) -> None:
    caches_for = _nonlocals(_deps(env).facts_deps.decompose_runs, "decompose_cache_for")
    caches = _nonlocals(caches_for, "decompose_caches")
    for n in range(cli._CHAIR_DECOMPOSE_INTAKES + 5):
        caches_for(f"intake-{n}")
    assert (len(caches), "intake-0" in caches, f"intake-{cli._CHAIR_DECOMPOSE_INTAKES + 4}" in caches) == (
        cli._CHAIR_DECOMPOSE_INTAKES, False, True,
    )


def test_known_residual_the_intake_file_is_read_again_on_every_tick(env: Env) -> None:
    """KNOWN RESIDUAL, left alone on purpose: `chair_exec.read_intake_id` reads the intake file by `Path.read_text` on
    every `decompose_runs_of` call, so an unchanged intake is read on every tick and the counters see it. Caching it
    belongs to its own task. Until then this pins that the reads do not grow, not that they are zero. The idle-stall
    chain's own reads (`round_intake`, `ticket_items`, the backlog) go through the cached work files and show no
    work-file read on the second tick in the first test."""
    deps = _deps(env)
    _, first = _tick(env, deps)
    _, second = _tick(env, deps)
    assert 0 < second["intake_file"] <= first["intake_file"]
