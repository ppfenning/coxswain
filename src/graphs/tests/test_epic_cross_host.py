"""A phase finds the branches earlier phases left on origin, from any host, and stops on one that is nowhere."""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from threading import Timer
from types import SimpleNamespace
from typing import NamedTuple

import pytest

import harness.epic as epic
from harness.phase_remote import FetchOutcome, Kind, missing_branch_cause
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    cart,
    drive,
    git,
    initiative,
    is_ancestor,
    repo,
)
from tests.test_epic_merge_quarantine import _merge_ctx
from tests.test_epic_phase_subset import P1, P1_BRANCH, P2, staged

P2_BRANCH = f"epic/demo-initiative/{P2}"
INITIATIVE = "demo-initiative"


class Hosts(NamedTuple):
    a: Path
    b: Path
    origin: Path
    cart_b: dict
    home_b: Path


@pytest.fixture
def hosts(repo, cart, tmp_path) -> Hosts:  # noqa: F811
    """Two clones of one bare origin, each with its own worktree root. Host A is the `repo` fixture."""
    bare = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(bare), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    home_b = tmp_path / "host-b"
    home_b.mkdir()
    git("clone", "-q", str(bare), str(home_b / "repo"), cwd=tmp_path)
    cart_b = {**cart, "landing_areas": {**cart["landing_areas"], "worktree_root": str(home_b / "worktrees")}}
    return Hosts(repo, home_b / "repo", bare, cart_b, home_b)


def _local_branches(clone: Path) -> list[str]:
    return git("branch", "--list", "epic/*", "--format=%(refname:short)", cwd=clone).splitlines()


def _assert_stopped_not_quarantined(result: dict, branch: str, cause: str) -> None:
    (record,) = result["phases"]
    assert record["status"] == "blocked"
    assert record["missing_branch"] == branch
    assert record["reason"] == cause
    assert "phase_failed_to_start" not in record
    assert result["quarantined"] == []
    assert result["totals"]["tasks_quarantined"] == 0
    assert record.get("task_records", []) == []


def test_phase_two_on_another_host_builds_on_the_phase_one_branch_it_finds_on_origin(hosts, cart, tmp_path) -> None:  # noqa: F811
    first, _ = drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a")
    assert first["phases"][0]["status"] == "complete"
    p1_commit = git("rev-parse", P1_BRANCH, cwd=hosts.a)
    assert _local_branches(hosts.b) == []

    result, _ = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    (record,) = result["phases"]
    assert record["status"] != "blocked"
    assert is_ancestor(hosts.b, p1_commit, P2_BRANCH)


def test_a_parent_branch_on_neither_clone_nor_origin_stops_the_phase_without_a_quarantine(hosts, monkeypatch) -> None:
    opened: list[str] = []
    real = epic._open_phase_worktree
    monkeypatch.setattr(epic, "_open_phase_worktree", lambda *a, **k: opened.append("called") or real(*a, **k))

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    _assert_stopped_not_quarantined(
        result, P1_BRANCH, missing_branch_cause(P1_BRANCH, FetchOutcome(Kind.MISSING_EVERYWHERE))
    )
    assert opened == []
    assert not any(c["role"] == "build" for c in runner.calls)
    assert _local_branches(hosts.b) == []


def test_a_settled_parent_in_cut_from_only_that_is_missing_everywhere_stops_before_the_worktree_opens(
    hosts, monkeypatch
) -> None:
    work = staged(unmerged=("t1-probe",))
    opened: list[str] = []
    real = epic._open_phase_worktree
    monkeypatch.setattr(epic, "_open_phase_worktree", lambda *a, **k: opened.append("called") or real(*a, **k))

    result, _ = drive(hosts.b, hosts.cart_b, hosts.home_b, work=work, only_phases=[P2], run_id="epic-b")

    _assert_stopped_not_quarantined(
        result, P1_BRANCH, missing_branch_cause(P1_BRANCH, FetchOutcome(Kind.MISSING_EVERYWHERE))
    )
    assert opened == []


def test_a_parent_whose_needs_resolve_to_commit_ids_still_stops_when_its_branch_is_missing(hosts, monkeypatch) -> None:
    """An open parent with approved, merged needs contributes merged commit ids, which live only on its branch."""
    monkeypatch.setattr(epic, "_required_refs", lambda *args, **kwargs: ["a" * 40])
    opened: list[str] = []
    real = epic._open_phase_worktree
    monkeypatch.setattr(epic, "_open_phase_worktree", lambda *a, **k: opened.append("called") or real(*a, **k))

    result, _ = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    _assert_stopped_not_quarantined(
        result, P1_BRANCH, missing_branch_cause(P1_BRANCH, FetchOutcome(Kind.MISSING_EVERYWHERE))
    )
    assert opened == []


def test_a_parent_branch_that_is_missing_but_needed_for_nothing_does_not_stop_the_phase(hosts, monkeypatch) -> None:
    monkeypatch.setattr(epic, "_required_refs", lambda *args, **kwargs: [])

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    (record,) = result["phases"]
    assert "missing_branch" not in record and record["status"] != "blocked"
    assert any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)


def test_an_unreachable_origin_is_a_fetch_failure_not_a_missing_branch_claim(hosts, monkeypatch) -> None:
    resolve = epic.resolve_default_ref

    def resolve_then_lose_origin(*args, **kwargs):
        resolved = resolve(*args, **kwargs)
        git("remote", "set-url", "origin", str(hosts.home_b / "gone.git"), cwd=hosts.b)  # the start-up fetch is done
        return resolved

    monkeypatch.setattr(epic, "resolve_default_ref", resolve_then_lose_origin)

    result, _ = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    (record,) = result["phases"]
    assert record["status"] == "blocked" and record["missing_branch"] == P1_BRANCH
    assert "origin was unreachable" in record["reason"] and "neither locally nor on origin" not in record["reason"]
    assert result["quarantined"] == []


def test_a_stale_local_branch_is_fast_forwarded_by_the_fetch(hosts, cart, tmp_path) -> None:  # noqa: F811
    drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a")
    git("branch", P1_BRANCH, "main", cwd=hosts.b)
    stale = git("rev-parse", P1_BRANCH, cwd=hosts.b)

    outcomes = epic._fetch_phase_branches(SimpleNamespace(repo=hosts.b), [P1_BRANCH])

    assert outcomes[P1_BRANCH].kind == Kind.FAST_FORWARDED
    assert stale != git("rev-parse", P1_BRANCH, cwd=hosts.b) == git("rev-parse", f"origin/{P1_BRANCH}", cwd=hosts.b)


def test_a_local_branch_holding_its_own_commit_is_not_overwritten_by_the_fetch(hosts, cart, tmp_path) -> None:  # noqa: F811
    drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a")
    git("branch", P1_BRANCH, "main", cwd=hosts.b)
    own = git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit-tree", "-m", "own", "-p", P1_BRANCH,
              f"{P1_BRANCH}^{{tree}}", cwd=hosts.b)
    git("update-ref", f"refs/heads/{P1_BRANCH}", own, cwd=hosts.b)

    outcomes = epic._fetch_phase_branches(SimpleNamespace(repo=hosts.b), [P1_BRANCH])

    assert outcomes[P1_BRANCH].kind == Kind.DIVERGED
    assert git("rev-parse", P1_BRANCH, cwd=hosts.b) == own
    assert git("rev-parse", f"refs/coxswain/fetched/origin/{P1_BRANCH}", cwd=hosts.b) == git(
        "rev-parse", f"origin/{P1_BRANCH}", cwd=hosts.b
    )


def test_the_branches_to_fetch_are_the_own_and_parent_branches_and_the_epic_refs_of_needed_and_cut_from() -> None:
    own, p1, p2 = (f"epic/{INITIATIVE}/{p}" for p in ("p3", "p1", "p2"))

    sha = "a" * 40

    assert epic._phase_branches_to_fetch(INITIATIVE, own, [p1, p2], [p1], [p1, "main", sha, "pr/x"]) == (
        [own, p1, p2],
        [p1],
        ["main", sha, "pr/x"],
    )
    assert epic._phase_branches_to_fetch(INITIATIVE, own, [], [], [f"epic/other/{P1}"]) == (
        [own],
        [],
        [f"epic/other/{P1}"],
    )
    assert epic._phase_branches_to_fetch(INITIATIVE, own, [], [], [p2]) == ([own, p2], [p2], [])
    assert epic._phase_branches_to_fetch(INITIATIVE, own, [p1], [sha], [sha]) == ([own, p1], [], [sha])


def _merge_on_b(hosts, store_conn, task: str):
    """Run the merge arm on host B, in a worktree of the phase branch, for the draft of `task`."""
    ctx = _merge_ctx(store_conn, "files")
    ctx.repo = hosts.b
    ctx.run_id = "epic-b"
    ctx.phase_branch = lambda phase: P1_BRANCH
    ctx.draft_branch = lambda phase, name: f"{P1_BRANCH}--{name}"
    worktree = hosts.home_b / "wt"
    ctx.phase_worktree = lambda phase: worktree
    git("worktree", "add", "-q", "-b", P1_BRANCH, str(worktree), "main", cwd=hosts.b)
    state = epic._Execution(landed={task: True}, merged={}, moved={}, quarantined=[])
    move = {"kind": "merge_stack", "target": task}
    return epic._execute(ctx, move, slot="merge", subject=task, phase=P1, state=state, by_id={}), state


def test_a_draft_another_host_pushed_is_fetched_for_the_merge(hosts, cart, tmp_path, store_conn) -> None:  # noqa: F811
    drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a")
    draft = f"{P1_BRANCH}--t1-probe"
    assert git("branch", "--list", draft, cwd=hosts.b) == ""

    (ok, _), state = _merge_on_b(hosts, store_conn, "t1-probe")

    assert ok and state.merged == {"t1-probe": True}
    assert is_ancestor(hosts.b, git("rev-parse", draft, cwd=hosts.a), P1_BRANCH)


def test_a_draft_on_neither_clone_nor_origin_refuses_the_merge_without_a_quarantine(hosts, store_conn) -> None:
    (ok, detail), state = _merge_on_b(hosts, store_conn, "t9-ghost")

    assert not ok and state.quarantined == []
    assert detail == missing_branch_cause(f"{P1_BRANCH}--t9-ghost", FetchOutcome(Kind.MISSING_EVERYWHERE))


def test_a_dependent_waits_for_a_parent_that_is_launched_and_still_cutting_its_branch(hosts) -> None:
    parent: Future[dict] = Future()
    ctx = SimpleNamespace(repo=hosts.b, initiative_id=INITIATIVE, launched={P1: parent})
    cutting = Timer(0.3, lambda: (git("branch", P1_BRANCH, "main", cwd=hosts.b), parent.set_result({})))
    cutting.start()
    try:
        missing = epic.fetch_branch(hosts.b, P1_BRANCH)
        assert missing.kind == Kind.MISSING_EVERYWHERE

        found = epic._await_launched_branch(ctx, P1_BRANCH, missing)
        unlaunched = epic._await_launched_branch(ctx, f"epic/{INITIATIVE}/p9-none", missing)
    finally:
        cutting.join()

    assert found.kind == Kind.PRESENT_LOCALLY
    assert unlaunched.kind == Kind.MISSING_EVERYWHERE
