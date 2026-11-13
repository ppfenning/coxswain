"""The steward tick from profile to executed action: real deps, a real git repository, only the process launch recorded."""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_run, cli, courier, run_store
from harness import store_migrate

INITIATIVE = "epic-x"
PHASE = "p1"
NEXT_PHASE = "p2"  # open work after the merge: a relaunch needs a ready task, and an all-approved epic plans no relaunch
LEVEL = f"epic/{INITIATIVE}/{PHASE}"
TICKET = f"pr/{INITIATIVE}"
_OLD = "2026-01-01T00:00:00Z"
_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=_GIT_ENV)


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _contains(repo: Path, ancestor: str, branch: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", ancestor, branch, check=False).returncode == 0


def _tasks(initiative: str) -> list[tuple[str, str, str, str]]:
    """(phase, task, state, run): two approved tasks of the level, each from its own run, and one ready task after it."""
    return [
        (PHASE, "t1", "approved", f"{initiative}-1"), (PHASE, "t1b", "approved", f"{initiative}-2"),
        (NEXT_PHASE, "t2", "ready", ""),
    ]


def _repo(ws: Path, initiative: str = INITIATIVE, land_at: str = "ticket", precreate_ticket: bool = True) -> Path:
    """`main`, optionally the ticket branch at main, and a level branch one commit ahead of it; the initiative names the repo.

    Every file is committed, so the working tree is clean for any land that checks it."""
    repo = ws / f"repo-{initiative}"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _commit(repo, ".agent-checks")
    _commit(repo, "a.txt")
    if precreate_ticket:
        _git(repo, "branch", f"pr/{initiative}")
    _git(repo, "checkout", "-b", f"epic/{initiative}/{PHASE}")
    _commit(repo, "level.txt")
    _git(repo, "checkout", "main")
    (ws / "work" / initiative).mkdir(parents=True)
    (ws / "work" / initiative / "initiative.md").write_text(f"---\nrepo: {repo}\nland_at: {land_at}\n---\n", encoding="utf-8")
    for phase, task, state, run in _tasks(initiative):  # the docket reads the ticket files; the store gives their state
        ticket = ws / "work" / initiative / phase / f"{task}.md"
        ticket.parent.mkdir(parents=True, exist_ok=True)
        ticket.write_text(f"---\nid: {task}\ntitle: {task}\nstate: {state}\n---\nbody\n", encoding="utf-8")
        if run:  # the task record the stranded read turns into the approved task's run
            record = ws / "runs" / run / "tasks" / phase / f"{task}.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"initiative": initiative, "phase": phase, "repo": str(repo), "arbitration": {"verdict": "approve"}}), encoding="utf-8")
    return repo


def _seed_store(runs: Path, monkeypatch, initiatives: tuple[str, ...]) -> None:
    """Per initiative one row, its tasks and their runs, through the store's own writer; every run's row has ended."""
    monkeypatch.setattr(run_store, "_harness_python", lambda: Path(sys.executable))
    filler = {"needs": [], "title": "t", "surfaces": [], "body": "", "extra": {}}  # every work_items column is NOT NULL
    rows = [
        row
        for i in initiatives
        for row in (
            {**filler, "initiative": i, "task_id": i, "kind": "initiative", "phase": "", "state": "ready"},
            *({**filler, "initiative": i, "task_id": task, "kind": "task", "phase": phase, "state": state} for phase, task, state, _ in _tasks(i)),
        )
    ]
    store_migrate.open_store(run_store._store_url(runs), _OLD).close()  # the harness's own migrations create the tables
    assert [run_store.upsert_row_detail(runs, row) for row in rows] == [""] * len(rows)
    with sqlite3.connect(runs / "cox.db") as conn:
        conn.execute(
            "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at) VALUES (?, 'chair-x@h:1', 3, ?, '2999-01-01T00:00:00Z')",
            (cli.chair.LEASE_NAME, _OLD),
        )
        for i in initiatives:
            for run in (f"{i}-1", f"{i}-2"):
                conn.execute("INSERT INTO runs (run_id, launched_at, ended_at) VALUES (?, ?, ?)", (run, _OLD, "2026-01-01T01:00:00Z"))


LANDED = (0, "merge: ok\nmark_done: ok\n")
DIRTY = (1, "land: refusing, the working tree is dirty\n")


def _tick(
    tmp_path: Path, monkeypatch, *, drop_executor: bool, initiatives: tuple[tuple[str, str, bool], ...] = ((INITIATIVE, "ticket", True),),
    broken: tuple[str, ...] = (), answers: dict[str, tuple[int, str]] | None = None,
):
    """(repo, workspace, launched argvs, performed results, ticket tip before the tick) after one chair tick.

    `initiatives` is (id, land_at, ticket branch pre-created); `broken` ids lose their level branch; `answers` maps
    a `cox runs land` line (argv[2:6] joined) to its (code, output), landed by default. The repo is the first initiative's."""
    ws = tmp_path
    runs = ws / "runs"
    runs.mkdir()
    monkeypatch.setattr("agent_tools.chair_exec.land_commit", lambda repo: "abc123")
    repos = [_repo(ws, i, land_at, pre) for i, land_at, pre in initiatives]
    for i in broken:
        _git(ws / f"repo-{i}", "branch", "-D", f"epic/{i}/{PHASE}")
    repo = repos[0]
    _seed_store(runs, monkeypatch, tuple(i for i, _, _ in initiatives))
    monkeypatch.setattr(cli.chair.store_cli, "lease_renew", lambda runs_dir, name, who, epoch, ttl: None)
    monkeypatch.setattr(cli.chair.store_cli, "lease_release", lambda runs_dir, name, who, epoch: cli.chair.store_cli.LeaseReleased())
    (runs / cli.chair.LEASE_FILENAME).write_text(json.dumps({"holder": "chair-x@h:1", "epoch": 3}), encoding="utf-8")
    cli.chair.write(runs, {"session": "chair-x", "pid": 1, "host": "h", "taken_at": _OLD, "heartbeat_at": _OLD, "runs": [], "claude_session": None})
    deps = cli._chair_run_deps(runs, {"chair": {"steward": True}}, "chair-x", 1, "h", False, lambda line: None, tmp_path / "profile.yaml", "store")
    launched: list[list[str]] = []

    def launch(argv: list[str]) -> tuple[int, str]:
        launched.append(argv)
        return (answers or {}).get(" ".join(argv[2:6]), LANDED) if argv[:3] == ["cox", "runs", "land"] else (0, "")

    exec_deps = dataclasses.replace(
        deps.exec_deps, run=launch, launch_stagger_s=0.0,
        **({"steward_exec": None} if drop_executor else {}),
    )
    performed: list[dict] = []

    def perform(*args):
        results = chair_run.chair_exec.perform(*args)
        performed.extend(results)
        return results

    before = _git(repo, "rev-parse", TICKET, check=False).stdout.strip()  # empty when the case starts with no ticket branch
    chair_run.tick(dataclasses.replace(deps, exec_deps=exec_deps, perform=perform), False, datetime.now(UTC))
    return repo, ws, launched, performed, before


def _bus(ws: Path) -> list[dict]:
    path = ws / "courier.jsonl"
    return courier.entries(path.read_text(encoding="utf-8")) if path.exists() else []


def _relaunches(launched: list[list[str]], initiative: str = INITIATIVE) -> list[list[str]]:
    return [argv for argv in launched if argv[:3] == ["cox", "route", "launch"] and f"work/{initiative}" in argv]


def _lands(launched: list[list[str]], flag: str) -> list[str]:
    """The `cox runs land` lines (argv[2:6] joined) that carry `flag`, in the order they ran."""
    return [" ".join(a[2:6]) for a in launched if a[:3] == ["cox", "runs", "land"] and flag in a]


def _events(ws: Path, name: str) -> list[tuple[str, str]]:
    return [(e["topic"], e["event"]) for e in _bus(ws) if e.get("event") == name]


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


@pytest.mark.parametrize("land_at", ["ticket", "level", "task"])
def test_a_tick_merges_at_each_land_at(tmp_path, monkeypatch, land_at) -> None:
    repo, ws, launched, _performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False, initiatives=((INITIATIVE, land_at, False),))

    assert _events(ws, "level_merged") == [(INITIATIVE, "level_merged")]
    assert _git(repo, "status", "--porcelain").stdout == ""
    phase_lands, task_lands = _lands(launched, "--phase"), _lands(launched, "--task")
    if land_at == "ticket":
        assert _contains(repo, LEVEL, TICKET)  # the steward created the branch, then merged the level into it
        assert not phase_lands  # the planner's own per-task lands of approved work may run; the steward lands no phase
        assert len(_relaunches(launched)) == 1
    elif land_at == "level":
        assert not _branch_exists(repo, TICKET)
        assert phase_lands == [f"land {INITIATIVE}-2 --phase {PHASE}"] and not task_lands
        assert len(_relaunches(launched)) == 1
    else:
        assert not _branch_exists(repo, TICKET)
        assert task_lands == [f"land {INITIATIVE}-1 --task t1", f"land {INITIATIVE}-2 --task t1b"] and not phase_lands


@pytest.mark.xfail(
    strict=True,
    reason="defect in the planner, not patched here: at land_at task the pre-land facts still hold t1 and t1b approved, "
    "so the planner reports 'blocked by p1: 2 awaiting land' and plans no relaunch in the tick that lands them",
)
def test_a_task_tick_relaunches_after_the_merge(tmp_path, monkeypatch) -> None:
    _repo_x, _ws, launched, performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False, initiatives=((INITIATIVE, "task", False),))

    assert len(_relaunches(launched)) == 1, [(r["action"]["kind"], r.get("reason", "")) for r in performed]


def test_a_failing_initiative_does_not_stop_the_next_from_merging_and_relaunching(tmp_path, monkeypatch) -> None:
    both = (("epic-x", "ticket", True), ("epic-y", "level", True))
    _repo_x, ws, launched, performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False, initiatives=both, broken=("epic-x",))

    assert _events(ws, "merge_failed") == [("epic-x", "merge_failed")]
    assert _events(ws, "level_merged") == [("epic-y", "level_merged")]
    assert _lands(launched, "--phase") == [f"land epic-y-2 --phase {PHASE}"]
    assert len(_relaunches(launched, "epic-y")) == 1
    # chair_exec.perform skips a relaunch tagged after_steward when its epic's merge_level failed; the failed epic is
    # not relaunched, it is recorded with that reason, and the next epic is unaffected.
    assert len(_relaunches(launched, "epic-x")) == 0
    assert any("not relaunched, merge_level for epic-x failed" in r.get("reason", "") for r in performed)
    assert _git(tmp_path / "repo-epic-y", "status", "--porcelain").stdout == ""


def test_a_tick_merges_the_level_and_relaunches_after_the_merge(tmp_path, monkeypatch) -> None:
    repo, ws, launched, performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False)

    assert _contains(repo, LEVEL, TICKET)
    assert [e["event"] for e in _bus(ws) if e.get("event") == "level_merged"] == ["level_merged"]
    assert len(_relaunches(launched)) == 1
    assert [r["action"]["kind"] for r in performed if r["action"]["kind"] in ("merge_level", "relaunch")] == ["merge_level", "relaunch"]
    assert not any("no steward executor" in str(r.get("reason", "")) for r in performed)


def test_a_tick_without_an_executor_refuses_the_merge_and_still_relaunches(tmp_path, monkeypatch) -> None:
    repo, ws, launched, performed, before = _tick(tmp_path, monkeypatch, drop_executor=True)

    merge = next(r for r in performed if r["action"]["kind"] == "merge_level")
    assert (merge["status"], "no steward executor" in merge["reason"]) == ("refused", True)
    attention = [e for e in _bus(ws) if e.get("event") == "needs_attention"]
    assert len(attention) == 1 and "no steward executor" in attention[0]["fields"]["reason"]
    assert not [e for e in _bus(ws) if e.get("event") == "level_merged"]
    assert len(_relaunches(launched)) == 1
    assert _git(repo, "rev-parse", TICKET).stdout.strip() == before
    assert not _contains(repo, LEVEL, TICKET)
