"""A phase finds the branches earlier phases left on origin, from any host, and stops on one that is nowhere.

At `initiative` the branch a phase needs is the initiative branch: it holds every merged parent phase. At `task` a
phase is stacked on its parents' own branches."""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from threading import Timer
from types import SimpleNamespace
from typing import NamedTuple

import pytest

import harness.epic as epic
from harness import epic_branches
from harness.phase_remote import FetchOutcome, Kind, missing_branch_cause
from harness.store_migrate import open_store
from harness.store_write import Store
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
INIT_BRANCH = f"init/{INITIATIVE}"


def _shared_store() -> Store:
    """One store for both hosts, as a deployment has: the landing record host A writes is the one host B reads."""
    return Store(open_store("sqlite:///:memory:", "2026-11-12T00:00:00+00:00"))


def _pushing_early(cart: dict) -> dict:  # noqa: F811
    return {**cart, "landing_areas": {**cart["landing_areas"], "push_initiative": "early"}}


def test_at_initiative_phase_two_on_another_host_builds_on_the_initiative_branch_it_finds_on_origin(hosts, cart, tmp_path) -> None:  # noqa: F811
    store = _shared_store()
    first, _ = drive(hosts.a, _pushing_early(cart), tmp_path, work=initiative(two_phases=False), run_id="epic-a", store=store)
    assert first["phases"][0]["initiative_merge"]["status"] == "merged"
    merge_sha = first["phases"][0]["initiative_merge"]["merge_sha"]
    p1_commit = git("rev-parse", P1_BRANCH, cwd=hosts.a)
    assert git("branch", "--list", INIT_BRANCH, cwd=hosts.b) == ""

    result, runner = drive(
        hosts.b, _pushing_early(hosts.cart_b), hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b", store=store
    )

    (record,) = result["phases"]
    assert record["base"] == INIT_BRANCH and record["status"] != "blocked"
    assert any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)
    assert is_ancestor(hosts.b, merge_sha, P2_BRANCH) and is_ancestor(hosts.b, p1_commit, P2_BRANCH)


def test_at_initiative_a_merge_this_clone_cannot_get_stops_the_phase_without_a_quarantine(hosts, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    """Host A's merge of phase one is not on origin (an older host kept it local): host B must not build phase two
    without it. A merge made now is pushed before it is recorded, so origin is put back here to make that case."""
    store = _shared_store()
    first, _ = drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a", store=store)
    assert first["phases"][0]["initiative_merge"]["status"] == "merged"
    assert git("rev-parse", INIT_BRANCH, cwd=hosts.origin) == git("rev-parse", INIT_BRANCH, cwd=hosts.a), "pushed before recorded"
    git("update-ref", f"refs/heads/{INIT_BRANCH}", git("merge-base", INIT_BRANCH, "main", cwd=hosts.a), cwd=hosts.origin)
    opened: list[str] = []
    real = epic.open_phase_worktree
    monkeypatch.setattr(epic, "open_phase_worktree", lambda *a, **k: opened.append("called") or real(*a, **k))

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b", store=store)

    (record,) = result["phases"]
    assert record["status"] == "blocked" and record["missing_branch"] == INIT_BRANCH
    assert f"does not hold the merge of parent phase '{P1}'" in record["reason"]
    assert result["quarantined"] == [] and result["totals"]["tasks_quarantined"] == 0
    assert opened == []
    assert not any(c["role"] == "build" for c in runner.calls)
    assert _local_branches(hosts.b) == []


def _second_merge_on_b(hosts, first: dict, *, extra: str | None) -> str:
    """Host B's own "merge phase" commit of phase one on the base origin's merge was made on: the same tree as
    origin's, or (with `extra`) a different one. Its tip."""
    merge_sha = first["phases"][0]["initiative_merge"]["merge_sha"]
    git("fetch", "-q", "origin", f"+refs/heads/{P1_BRANCH}:refs/remotes/origin/{P1_BRANCH}", cwd=hosts.b)
    git("fetch", "-q", "origin", f"+refs/heads/{INIT_BRANCH}:refs/remotes/origin/{INIT_BRANCH}", cwd=hosts.b)
    git("checkout", "-q", "-b", INIT_BRANCH, f"{merge_sha}^1", cwd=hosts.b)
    git("-c", "user.name=lane", "-c", "user.email=lane@invalid", "merge", "-q", "--no-ff", "-m",
        f"{INITIATIVE}: merge phase {P1} (on lane-host)", f"origin/{P1_BRANCH}", cwd=hosts.b)
    if extra:
        (hosts.b / extra).write_text("only on lane-host\n")
        git("add", extra, cwd=hosts.b)
        git("-c", "user.name=lane", "-c", "user.email=lane@invalid", "commit", "-q", "-m", "lane-only", cwd=hosts.b)
    git("checkout", "-q", "--detach", cwd=hosts.b)
    return git("rev-parse", INIT_BRANCH, cwd=hosts.b)


def test_at_initiative_a_parent_check_that_finds_its_branch_diverged_with_the_same_tree_resets_it_and_builds(hosts, cart, tmp_path) -> None:  # noqa: F811
    """Two "merge phase" commits of one phase, one here and one on origin, with one tree: origin's is the one."""
    store = _shared_store()
    first, _ = drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a", store=store)
    origin_tip = git("rev-parse", INIT_BRANCH, cwd=hosts.origin)
    local = _second_merge_on_b(hosts, first, extra=None)
    assert local != origin_tip

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b", store=store)

    (record,) = result["phases"]
    assert record["status"] != "blocked", record.get("reason")
    assert any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)
    assert is_ancestor(hosts.b, origin_tip, P2_BRANCH) and not is_ancestor(hosts.b, local, P2_BRANCH)


def test_at_initiative_a_parent_check_that_finds_its_branch_diverged_with_other_work_stops_for_the_chair(hosts, cart, tmp_path, _courier_sends_are_recorded) -> None:  # noqa: F811
    store = _shared_store()
    first, _ = drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a", store=store)
    origin_tip = git("rev-parse", INIT_BRANCH, cwd=hosts.origin)
    local = _second_merge_on_b(hosts, first, extra="lane-only.txt")

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b", store=store)

    (record,) = result["phases"]
    assert record["status"] == "blocked" and record["reason"].startswith("needs the chair:")
    assert local[:12] in record["reason"] and origin_tip[:12] in record["reason"]
    assert not any(c["role"] == "build" for c in runner.calls)
    assert git("rev-parse", INIT_BRANCH, cwd=hosts.b) == local, "neither copy is moved"
    told = [c for c in _courier_sends_are_recorded if c[2].startswith("needs_chair:")]
    assert told == [(f"coxswain://initiative/{INITIATIVE}", "chair", f"needs_chair: {INITIATIVE}: {record['reason']}")]

    again, _ = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b2", store=store)

    assert again["phases"][0]["reason"] == record["reason"], "the phase still stops"
    assert [c for c in _courier_sends_are_recorded if c[2].startswith("needs_chair:")] == told, "told once, not every run"


def test_a_lane_divergence_is_news_only_with_a_reason_not_told_before() -> None:
    assert epic_branches.diverged_news(None, "needs the chair: a")
    assert not epic_branches.diverged_news("needs the chair: a", "needs the chair: a")
    assert epic_branches.diverged_news("needs the chair: a", "needs the chair: b"), "another pair of commits"


def test_at_initiative_a_lane_whose_record_names_its_own_merge_does_not_reset_it_and_tells_the_chair(hosts, cart, tmp_path, _courier_sends_are_recorded) -> None:  # noqa: F811
    """Same tree as origin's, but the record (written before every merge was pushed) names this clone's merge commit.
    The parent check finds the recorded merge here, so the phase builds; its merge then meets origin's copy."""
    from harness import landing_store

    store = _shared_store()
    first, _ = drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a", store=store)
    local = _second_merge_on_b(hosts, first, extra=None)
    record = landing_store.get_record(store.conn, INITIATIVE)
    entry = {**record["phases"][P1], "merge_sha": local}
    landing_store.put_record(store.conn, {**record, "phases": {**record["phases"], P1: entry}})

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b", store=store)

    (phase,) = result["phases"]
    merge = phase["initiative_merge"]
    assert phase["status"] == "awaiting_land" and merge["status"] == "error"
    assert "needs the chair:" in merge["detail"] and f"lacks recorded commit(s) {local[:12]}" in merge["detail"]
    assert git("rev-parse", INIT_BRANCH, cwd=hosts.b) == local, "the recorded merge commit is not dropped"
    told = [c for c in _courier_sends_are_recorded if c[2].startswith("needs_chair:")]
    assert len(told) == 1 and f"lacks recorded commit(s) {local[:12]}" in told[0][2]


def test_at_initiative_a_parent_merged_nowhere_blocks_the_named_phase(hosts) -> None:
    """The store says phase one's tasks are approved, and no landing record says the phase merged."""
    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    (record,) = result["phases"]
    assert record["status"] == "blocked" and "missing_branch" not in record
    assert record["reason"] == f"parent phase '{P1}' is not merged into {INIT_BRANCH} yet; dependents start once it is"
    assert result["quarantined"] == []
    assert not any(c["role"] == "build" for c in runner.calls)


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


@pytest.mark.usefixtures("at_task")
def test_phase_two_on_another_host_builds_on_the_phase_one_branch_it_finds_on_origin(hosts, cart, tmp_path) -> None:  # noqa: F811
    first, _ = drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a")
    assert first["phases"][0]["status"] == "complete"
    p1_commit = git("rev-parse", P1_BRANCH, cwd=hosts.a)
    assert _local_branches(hosts.b) == []

    result, _ = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    (record,) = result["phases"]
    assert record["status"] != "blocked"
    assert is_ancestor(hosts.b, p1_commit, P2_BRANCH)


@pytest.mark.usefixtures("at_task")
def test_a_parent_branch_on_neither_clone_nor_origin_stops_the_phase_without_a_quarantine(hosts, monkeypatch) -> None:
    opened: list[str] = []
    real = epic.open_phase_worktree
    monkeypatch.setattr(epic, "open_phase_worktree", lambda *a, **k: opened.append("called") or real(*a, **k))

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    _assert_stopped_not_quarantined(
        result, P1_BRANCH, missing_branch_cause(P1_BRANCH, FetchOutcome(Kind.MISSING_EVERYWHERE))
    )
    assert opened == []
    assert not any(c["role"] == "build" for c in runner.calls)
    assert _local_branches(hosts.b) == []


@pytest.mark.usefixtures("at_task")
def test_a_parent_whose_needs_resolve_to_commit_ids_still_stops_when_its_branch_is_missing(hosts, monkeypatch) -> None:
    """An open parent with approved, merged needs contributes merged commit ids, which live only on its branch."""
    monkeypatch.setattr(epic, "required_refs", lambda *args, **kwargs: ["a" * 40])
    opened: list[str] = []
    real = epic.open_phase_worktree
    monkeypatch.setattr(epic, "open_phase_worktree", lambda *a, **k: opened.append("called") or real(*a, **k))

    result, _ = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    _assert_stopped_not_quarantined(
        result, P1_BRANCH, missing_branch_cause(P1_BRANCH, FetchOutcome(Kind.MISSING_EVERYWHERE))
    )
    assert opened == []


@pytest.mark.usefixtures("at_task")
def test_a_parent_branch_that_is_missing_but_needed_for_nothing_does_not_stop_the_phase(hosts, monkeypatch) -> None:
    monkeypatch.setattr(epic, "required_refs", lambda *args, **kwargs: [])

    result, runner = drive(hosts.b, hosts.cart_b, hosts.home_b, work=staged(), only_phases=[P2], run_id="epic-b")

    (record,) = result["phases"]
    assert "missing_branch" not in record and record["status"] != "blocked"
    assert any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)


@pytest.mark.usefixtures("at_task")
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

    outcomes = epic.fetch_phase_branches(SimpleNamespace(repo=hosts.b), [P1_BRANCH])

    assert outcomes[P1_BRANCH].kind == Kind.FAST_FORWARDED
    assert stale != git("rev-parse", P1_BRANCH, cwd=hosts.b) == git("rev-parse", f"origin/{P1_BRANCH}", cwd=hosts.b)


def test_a_local_branch_holding_its_own_commit_is_not_overwritten_by_the_fetch(hosts, cart, tmp_path) -> None:  # noqa: F811
    drive(hosts.a, cart, tmp_path, work=initiative(two_phases=False), run_id="epic-a")
    git("branch", P1_BRANCH, "main", cwd=hosts.b)
    own = git("-c", "user.name=t", "-c", "user.email=t@invalid", "commit-tree", "-m", "own", "-p", P1_BRANCH,
              f"{P1_BRANCH}^{{tree}}", cwd=hosts.b)
    git("update-ref", f"refs/heads/{P1_BRANCH}", own, cwd=hosts.b)

    outcomes = epic.fetch_phase_branches(SimpleNamespace(repo=hosts.b), [P1_BRANCH])

    assert outcomes[P1_BRANCH].kind == Kind.DIVERGED
    assert git("rev-parse", P1_BRANCH, cwd=hosts.b) == own
    assert git("rev-parse", f"refs/coxswain/fetched/origin/{P1_BRANCH}", cwd=hosts.b) == git(
        "rev-parse", f"origin/{P1_BRANCH}", cwd=hosts.b
    )


def test_the_branches_to_fetch_are_the_own_and_parent_branches_and_the_epic_refs_of_needed_and_cut_from() -> None:
    own, p1, p2 = (f"epic/{INITIATIVE}/{p}" for p in ("p3", "p1", "p2"))

    sha = "a" * 40

    assert epic.phase_branches_to_fetch(INITIATIVE, own, [p1, p2], [p1], [p1, "main", sha, "pr/x"]) == (
        [own, p1, p2],
        [p1],
        ["main", sha, "pr/x"],
    )
    assert epic.phase_branches_to_fetch(INITIATIVE, own, [], [], [f"epic/other/{P1}"]) == (
        [own],
        [],
        [f"epic/other/{P1}"],
    )
    assert epic.phase_branches_to_fetch(INITIATIVE, own, [], [], [p2]) == ([own, p2], [p2], [])
    assert epic.phase_branches_to_fetch(INITIATIVE, own, [p1], [sha], [sha]) == ([own, p1], [], [sha])


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
    state = epic.Execution(landed={task: True}, merged={}, moved={}, quarantined=[])
    move = {"kind": "merge_stack", "target": task}
    return epic.execute_cleared(ctx, move, slot="merge", subject=task, phase=P1, state=state, by_id={}), state


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
