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


def _repo(ws: Path) -> Path:
    """`main`, the ticket branch at main, and a level branch one commit ahead of it; the initiative names the repo."""
    repo = ws / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".agent-checks").write_text("true\n", encoding="utf-8")
    _commit(repo, "a.txt")
    _git(repo, "branch", TICKET)
    _git(repo, "checkout", "-b", LEVEL)
    _commit(repo, "level.txt")
    _git(repo, "checkout", "main")
    (ws / "work" / INITIATIVE).mkdir(parents=True)
    (ws / "work" / INITIATIVE / "initiative.md").write_text(f"---\nrepo: {repo}\n---\n", encoding="utf-8")
    for phase, task, state in ((PHASE, "t1", "approved"), (NEXT_PHASE, "t2", "ready")):  # the docket reads the ticket files; the store gives their state
        ticket = ws / "work" / INITIATIVE / phase / f"{task}.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text(f"---\nid: {task}\ntitle: {task}\nstate: {state}\n---\nbody\n", encoding="utf-8")
    return repo


def _seed_store(runs: Path, monkeypatch) -> None:
    """One initiative row and one approved task in the level, through the store's own writer; the run's row has ended."""
    monkeypatch.setattr(run_store, "_harness_python", lambda: Path(sys.executable))
    filler = {"needs": [], "title": "t", "surfaces": [], "body": "", "extra": {}}  # every work_items column is NOT NULL
    rows = [
        {**filler, "initiative": INITIATIVE, "task_id": INITIATIVE, "kind": "initiative", "phase": "", "state": "ready"},
        {**filler, "initiative": INITIATIVE, "task_id": "t1", "kind": "task", "phase": PHASE, "state": "approved"},
        {**filler, "initiative": INITIATIVE, "task_id": "t2", "kind": "task", "phase": NEXT_PHASE, "state": "ready"},
    ]
    store_migrate.open_store(run_store._store_url(runs), _OLD).close()  # the harness's own migrations create the tables
    assert [run_store.upsert_row_detail(runs, row) for row in rows] == [""] * len(rows)
    with sqlite3.connect(runs / "cox.db") as conn:
        conn.execute(
            "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at) VALUES (?, 'chair-x@h:1', 3, ?, '2999-01-01T00:00:00Z')",
            (cli.chair.LEASE_NAME, _OLD),
        )
        conn.execute("INSERT INTO runs (run_id, launched_at, ended_at) VALUES (?, ?, ?)", (f"{INITIATIVE}-1", _OLD, "2026-01-01T01:00:00Z"))


def _tick(tmp_path: Path, monkeypatch, *, drop_executor: bool):
    """(repo, workspace, launched argvs, performed results, ticket tip before the tick) after one chair tick."""
    ws = tmp_path
    runs = ws / "runs"
    runs.mkdir()
    repo = _repo(ws)
    _seed_store(runs, monkeypatch)
    monkeypatch.setattr(cli.chair.store_cli, "lease_renew", lambda runs_dir, name, who, epoch, ttl: None)
    monkeypatch.setattr(cli.chair.store_cli, "lease_release", lambda runs_dir, name, who, epoch: cli.chair.store_cli.LeaseReleased())
    (runs / cli.chair.LEASE_FILENAME).write_text(json.dumps({"holder": "chair-x@h:1", "epoch": 3}), encoding="utf-8")
    cli.chair.write(runs, {"session": "chair-x", "pid": 1, "host": "h", "taken_at": _OLD, "heartbeat_at": _OLD, "runs": [], "claude_session": None})
    deps = cli._chair_run_deps(runs, {"chair": {"steward": True}}, "chair-x", 1, "h", False, lambda line: None, tmp_path / "profile.yaml", "store")
    launched: list[list[str]] = []

    def launch(argv: list[str]) -> tuple[int, str]:
        launched.append(argv)
        return 0, ""

    exec_deps = dataclasses.replace(
        deps.exec_deps, run=launch, launch_stagger_s=0.0,
        **({"steward_exec": None} if drop_executor else {}),
    )
    performed: list[dict] = []

    def perform(*args):
        results = chair_run.chair_exec.perform(*args)
        performed.extend(results)
        return results

    before = _git(repo, "rev-parse", TICKET).stdout.strip()
    chair_run.tick(dataclasses.replace(deps, exec_deps=exec_deps, perform=perform), False, datetime.now(UTC))
    return repo, ws, launched, performed, before


def _bus(ws: Path) -> list[dict]:
    path = ws / "courier.jsonl"
    return courier.entries(path.read_text(encoding="utf-8")) if path.exists() else []


def _relaunches(launched: list[list[str]]) -> list[list[str]]:
    return [argv for argv in launched if argv[:3] == ["cox", "route", "launch"] and f"work/{INITIATIVE}" in argv]


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
