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
SECOND = "epic-y"  # the failure case's second initiative, with a repository of its own
PHASE = "p1"
NEXT_PHASE = "p2"  # open work after the merge: a relaunch needs a ready task, and an all-approved epic plans no relaunch
LEVEL = f"epic/{INITIATIVE}/{PHASE}"
TICKET = f"pr/{INITIATIVE}"
_OLD = "2026-01-01T00:00:00Z"
_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
LANDED = (0, "merge: ok\nmark_done: ok\n")  # a land counts only on exit 0 with `mark_done:` and `merge:`
DIRTY = (1, "land: refusing, the working tree is dirty\n")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=_GIT_ENV)


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _contains(repo: Path, ancestor: str, branch: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", ancestor, branch, check=False).returncode == 0


def _has_branch(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def _repo(ws: Path, land_at: str, initiative: str = INITIATIVE, *, ticket_branch: bool = True) -> Path:
    """`main`, the ticket branch at main, and a level branch one commit ahead of it; initiative.md names the repo and `land_at`.

    `ticket_branch=False` leaves no pr/<initiative> branch for the merge to find."""
    repo = ws / f"repo-{initiative}"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".agent-checks").write_text("true\n", encoding="utf-8")
    _commit(repo, "a.txt")
    if ticket_branch:
        _git(repo, "branch", f"pr/{initiative}")
    _git(repo, "checkout", "-b", f"epic/{initiative}/{PHASE}")
    _commit(repo, "level.txt")
    _git(repo, "checkout", "main")
    (ws / "work" / initiative).mkdir(parents=True)
    (ws / "work" / initiative / "initiative.md").write_text(f"---\nrepo: {repo}\nland_at: {land_at}\n---\n", encoding="utf-8")
    for phase, task, state in ((PHASE, "t1", "approved"), (NEXT_PHASE, "t2", "ready")):  # the docket reads the ticket files; the store gives their state
        ticket = ws / "work" / initiative / phase / f"{task}.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text(f"---\nid: {task}\ntitle: {task}\nstate: {state}\n---\nbody\n", encoding="utf-8")
    return repo


def _seed_store(runs: Path, monkeypatch, initiatives: tuple[str, ...]) -> None:
    """Per initiative one row and one approved task in the level, through the store's own writer; each run's row has ended."""
    monkeypatch.setattr(run_store, "_harness_python", lambda: Path(sys.executable))
    filler = {"needs": [], "title": "t", "surfaces": [], "body": "", "extra": {}}  # every work_items column is NOT NULL
    rows = [
        row
        for i in initiatives
        for row in (
            {**filler, "initiative": i, "task_id": i, "kind": "initiative", "phase": "", "state": "ready"},
            {**filler, "initiative": i, "task_id": "t1", "kind": "task", "phase": PHASE, "state": "approved"},
            {**filler, "initiative": i, "task_id": "t2", "kind": "task", "phase": NEXT_PHASE, "state": "ready"},
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
            conn.execute("INSERT INTO runs (run_id, launched_at, ended_at) VALUES (?, ?, ?)", (f"{i}-1", _OLD, "2026-01-01T01:00:00Z"))


def _tick(
    tmp_path: Path, monkeypatch, *, drop_executor: bool, land_at: str = "ticket", ticket_branch: bool = True,
    second: bool = False, answers: dict[str, tuple[int, str]] | None = None,
):
    """(repo, workspace, launched argvs, performed results, ticket tip before the tick) after one chair tick.

    `answers` maps a `cox runs land` line (`land <run> --phase p1`) to its (code, output); the rest of them land.
    `second` adds SECOND, landing at level, with its own repository."""
    ws = tmp_path
    runs = ws / "runs"
    runs.mkdir()
    repo = _repo(ws, land_at, ticket_branch=ticket_branch)
    if second:
        _repo(ws, "level", SECOND)
    _seed_store(runs, monkeypatch, (INITIATIVE, SECOND) if second else (INITIATIVE,))
    for i in (INITIATIVE, SECOND) if second else (INITIATIVE,):  # the approved task's run is read from its record, not from the store
        record = runs / f"{i}-1" / "tasks" / PHASE / "t1.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"branch": f"epic/{i}/{PHASE}--t1", "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"}, "landed": False}), encoding="utf-8")
    monkeypatch.setattr("agent_tools.chair_exec.land_commit", lambda repo: "abc123")  # the land is recorded, so no origin/main to read
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

    before = _git(repo, "rev-parse", TICKET).stdout.strip() if repo.exists() and _has_branch(repo, TICKET) else ""
    chair_run.tick(dataclasses.replace(deps, exec_deps=exec_deps, perform=perform), False, datetime.now(UTC))
    return repo, ws, launched, performed, before


def _bus(ws: Path) -> list[dict]:
    path = ws / "courier.jsonl"
    return courier.entries(path.read_text(encoding="utf-8")) if path.exists() else []


def _events(ws: Path, name: str) -> list[str]:
    """The topics of the bus entries named `name`, in the order they were published."""
    return [e["topic"] for e in _bus(ws) if e.get("event") == name]


def _why(performed: list[dict]) -> list[tuple]:
    """Each performed action's kind, status and reason, so a failed case names the step that refused."""
    return [(r["action"]["kind"], r["status"], r.get("reason")) for r in performed]


def _relaunches(launched: list[list[str]], initiative: str = INITIATIVE) -> list[list[str]]:
    return [argv for argv in launched if argv[:3] == ["cox", "route", "launch"] and f"work/{initiative}" in argv]


def _lands(launched: list[list[str]], initiative: str = INITIATIVE) -> list[list[str]]:
    """Every `cox runs land` argv for `initiative`, whose runs are named `<initiative>-1`."""
    return [argv for argv in launched if argv[:3] == ["cox", "runs", "land"] and argv[3].startswith(f"{initiative}-")]


def test_a_tick_at_ticket_creates_the_ticket_branch_and_relaunches_after_the_merge(tmp_path, monkeypatch) -> None:
    repo, ws, launched, performed, before = _tick(tmp_path, monkeypatch, drop_executor=False, land_at="ticket", ticket_branch=False)

    assert before == ""
    # Fails today: cli.py builds StewardPorts without target_branch_of, so chair_steward_exec._ensure_ticket_branch
    # refuses a missing pr/<initiative> with "no target branch is known" and merge_failed is published instead.
    assert _has_branch(repo, TICKET) and _contains(repo, LEVEL, TICKET), _why(performed)
    assert _events(ws, "level_merged") == [INITIATIVE]
    assert not _events(ws, "merge_failed")
    assert _lands(launched) == []
    assert len(_relaunches(launched)) == 1
    assert [r["action"]["kind"] for r in performed if r["action"]["kind"] in ("merge_level", "relaunch")] == ["merge_level", "relaunch"]
    assert not any("no steward executor" in str(r.get("reason", "")) for r in performed)


def test_a_tick_at_ticket_merges_into_an_existing_ticket_branch(tmp_path, monkeypatch) -> None:
    repo, ws, launched, performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False)

    assert _contains(repo, LEVEL, TICKET)
    assert _events(ws, "level_merged") == [INITIATIVE]
    assert len(_relaunches(launched)) == 1
    assert [r["action"]["kind"] for r in performed if r["action"]["kind"] in ("merge_level", "relaunch")] == ["merge_level", "relaunch"]


def test_a_tick_at_level_lands_the_phase_and_creates_no_ticket_branch(tmp_path, monkeypatch) -> None:
    repo, ws, launched, performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False, land_at="level", ticket_branch=False)

    assert len(_lands(launched)) == 1, _why(performed)
    [land] = _lands(launched)
    assert land[:10] == ["cox", "runs", "land", f"{INITIATIVE}-1", "--phase", PHASE, "--repo", str(repo), "--apply", "--no-claim"]
    assert not any(argv[:3] == ["cox", "runs", "land"] and "--task" in argv for argv in launched)
    assert _events(ws, "level_merged") == [INITIATIVE]
    assert not _events(ws, "merge_failed")
    assert not _has_branch(repo, TICKET)
    assert len(_relaunches(launched)) == 1
    assert [r["action"]["kind"] for r in performed if r["action"]["kind"] in ("merge_level", "relaunch")] == ["merge_level", "relaunch"]


def test_a_tick_at_task_lands_each_approved_task_from_its_run(tmp_path, monkeypatch) -> None:
    repo, ws, launched, performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False, land_at="task", ticket_branch=False)

    assert len(_lands(launched)) == 1, _why(performed)
    [land] = _lands(launched)  # t1 is the level's one approved task; t2 is ready and not landed
    assert land[:10] == ["cox", "runs", "land", f"{INITIATIVE}-1", "--task", "t1", "--repo", str(repo), "--apply", "--no-claim"]
    assert not any(argv[:3] == ["cox", "runs", "land"] and "--phase" in argv for argv in launched)
    assert _events(ws, "level_merged") == [INITIATIVE]
    assert not _events(ws, "merge_failed")
    assert not _has_branch(repo, TICKET)
    # Fails today: at land_at task the next phase waits on "p1: 1 awaiting land" until t1 is done, so the plan holds no
    # relaunch to run behind the merge. The defect is in the steward facts, not in this test.
    assert len(_relaunches(launched)) == 1, _why(performed)
    assert [r["action"]["kind"] for r in performed if r["action"]["kind"] in ("merge_level", "relaunch")] == ["merge_level", "relaunch"]


def test_with_two_initiatives_both_land_and_relaunch(tmp_path, monkeypatch) -> None:
    _repo_x, ws, launched, _performed, _before = _tick(tmp_path, monkeypatch, drop_executor=False, land_at="level", second=True)

    assert sorted(" ".join(a[4:6]) for a in _lands(launched, INITIATIVE) + _lands(launched, SECOND)) == [f"--phase {PHASE}"] * 2
    assert sorted(_events(ws, "level_merged")) == [INITIATIVE, SECOND]
    assert (len(_relaunches(launched)), len(_relaunches(launched, SECOND))) == (1, 1)


def test_a_failing_initiative_publishes_merge_failed_and_the_next_one_still_lands(tmp_path, monkeypatch) -> None:
    answers = {f"land {INITIATIVE}-1 --phase {PHASE}": DIRTY}  # the first initiative's land is refused
    _repo_x, ws, launched, performed, _before = _tick(
        tmp_path, monkeypatch, drop_executor=False, land_at="level", second=True, answers=answers,
    )

    assert _events(ws, "merge_failed") == [INITIATIVE]
    assert _events(ws, "level_merged") == [SECOND]
    second_lands = _lands(launched, SECOND)
    assert [a[4:6] for a in second_lands] == [["--phase", PHASE]]
    assert [a[4:6] for a in _lands(launched)] == [["--phase", PHASE]]
    # The failed initiative still gets its relaunch. Fails today: chair_exec records it as "not relaunched, merge_level
    # for epic-x failed" (test_chair_exec_steward.py pins that), so the ticket's rule and that earlier task disagree.
    assert len(_relaunches(launched, INITIATIVE)) == 1, _why(performed)
    assert len(_relaunches(launched, SECOND)) == 1


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
