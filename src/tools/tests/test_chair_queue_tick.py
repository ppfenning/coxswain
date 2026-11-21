"""C3, the tick's side: it checks a kind's worker lease again before each housekeeping or watch action, a duplicate
revert reuses the open revert PR, and an unreadable lease read fails closed only where a worker could be running.

The lease lives in a real store (the sqlite double, migrated by the harness). The revert's git and forge are the
in-memory ports `test_chair_exec_revert` uses; the worker taking the kind over is a real lease write made while the
revert runs."""

from pathlib import Path

from agent_tools import chair_kinds, chair_read_queues, run_store
from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_plan import plan_tick, tag_queue_kinds
from harness import store_chair_lease as kl
from harness.store_migrate import open_store
from tests.test_chair_exec_revert import REVERT, URL, FakeForge, FakeGit
from tests.test_chair_plan_kinds import _NOW, _RED_MAIN, _facts, _stale_row, _unstarted

NOW = "2026-11-20T12:00:30Z"


def _runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    open_store(run_store.store_url_for(runs), "2026-11-20T00:00:00Z").close()
    return runs


def _deps(recorded: list, **kw) -> Deps:
    return Deps(
        run=lambda argv: (0, ""), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=recorded.append, run_id=lambda a: "run-1", repo_for=lambda a: "r",
        quarantine_phase=lambda *args: None, resolve_land=lambda *args: None, **kw,
    )


def test_the_tick_marks_each_action_a_worker_could_run_with_its_kind_and_nothing_else():
    facts = _facts(landed_main=[_RED_MAIN], stale_candidates=[_stale_row("a", "a-1")], initiatives=[_unstarted("u")])
    tagged = tag_queue_kinds(plan_tick(facts, _NOW), facts, _NOW)
    kinds = {(a["kind"], a.get("queue_kind")) for a in tagged}
    assert {("revert_land", "watch"), ("stale_to_draft", "housekeeping"), ("housekeeping", "housekeeping")} <= kinds
    assert ("launch_epic", None) in kinds


def test_a_lease_taken_while_the_tick_performs_skips_the_ticks_later_actions_of_that_kind(tmp_path: Path):
    runs = _runs(tmp_path)
    conn = open_store(run_store.store_url_for(runs), "2026-11-20T00:00:00Z")

    class TakenOverGit(FakeGit):
        def push(self, repo, branch):  # while the tick's revert runs, a housekeeping worker takes its kind
            assert kl.acquire(conn, "chair:housekeeping", "worker-b", 60) == 1

    reader = chair_read_queues.QueueLeasesReader(runs, lambda: 0.0)
    recorded: list = []
    deps = _deps(recorded, revert_ports=lambda a: (TakenOverGit(), FakeForge()), queue_held=reader.held)
    actions = [
        {**REVERT, "queue_kind": "watch"},
        {"kind": "stale_to_draft", "initiative": "a", "epoch": 0, "queue_kind": "housekeeping"},
        {"kind": "needs_chair", "initiative": "x", "cause": "c", "epoch": 0},
    ]
    try:
        results = perform(actions, deps, lambda: 0, False)  # type: ignore[arg-type]
    finally:
        conn.close()
    statuses = [(r["action"]["kind"], r["status"]) for r in results]
    assert statuses[0] == ("revert_land", "done")
    assert ("stale_to_draft", "skipped") in statuses  # housekeeping is the worker's now
    assert ("needs_chair", "recorded") in statuses  # an action of no queue kind is untouched
    skipped = next(r for r in results if r["action"]["kind"] == "stale_to_draft")
    assert skipped["reason"] == "a queue worker holds chair:housekeeping"


def test_a_duplicate_revert_reuses_the_open_revert_pr_and_opens_no_second_one():
    opened: list[str] = []
    merged: list[str] = []

    class CountingForge(FakeForge):
        def open_pr(self, repo, branch, title, body):
            opened.append(branch)
            return URL

        def merge(self, pr):
            merged.append(pr)

    looked: list[tuple[str, int]] = []
    deps = _deps(
        [], revert_ports=lambda a: (FakeGit(), CountingForge()),
        revert_reuse=lambda a: ((lambda repo, pr: looked.append((repo, pr)) or "https://forge/repo/pull/50"), lambda *args: []),
    )
    results = perform([REVERT], deps, lambda: 0, False)  # type: ignore[list-item]
    assert looked == [("repo", 12)] and opened == [] and merged == ["https://forge/repo/pull/50"]
    assert results[0]["status"] == "done"


def test_with_no_open_revert_pr_the_stale_branch_is_cleared_and_one_pr_is_opened():
    cleared: list[tuple] = []
    deps = _deps([], revert_ports=lambda a: (FakeGit(), FakeForge()),
                 revert_reuse=lambda a: ((lambda repo, pr: None), lambda *args: cleared.append(args) or []))
    assert perform([REVERT], deps, lambda: 0, False)[0]["status"] == "done"  # type: ignore[list-item]
    assert cleared == [("repo", "revert/12", False)]


def test_an_unreadable_lease_read_changes_nothing_on_the_default_and_fails_closed_where_a_worker_could_run():
    unreadable = {"queue_leases": None, "queue_lease_now": "", "queue_leases_failing_s": 0.0}
    assert chair_kinds.tick_skips(unreadable) == frozenset()  # type: ignore[arg-type]
    assert chair_kinds.tick_skips({**unreadable, "queue_leases_seen": True}) == set(chair_kinds.QUEUE_KINDS)  # type: ignore[arg-type]
    assert chair_kinds.tick_skips({**unreadable, "chair_queues": ["watch"]}) == set(chair_kinds.QUEUE_KINDS)  # type: ignore[arg-type]


def test_a_lease_read_failing_past_the_ttl_raises_one_needs_chair_only_where_a_worker_could_run():
    base = {"queue_leases": None, "queue_lease_now": "", "chair_queues": ["housekeeping"]}
    assert chair_kinds.unreadable_actions({**base, "queue_leases_failing_s": 30.0}) == []  # type: ignore[arg-type]
    (raised,) = chair_kinds.unreadable_actions({**base, "queue_leases_failing_s": 61.0})  # type: ignore[arg-type]
    assert (raised["kind"], raised["cause"]) == ("needs_chair", "queue_leases_unreadable")
    assert chair_kinds.unreadable_actions({**base, "chair_queues": [], "queue_leases_failing_s": 600.0}) == []  # type: ignore[arg-type]
    held = {**base, "queue_leases": {}, "queue_lease_now": NOW, "queue_leases_failing_s": 0.0}
    assert chair_kinds.unreadable_actions(held) == []  # type: ignore[arg-type]
    tick = [a.get("cause") for a in plan_tick({**_facts(), **base, "queue_leases_failing_s": 61.0}, _NOW)]  # type: ignore[arg-type]
    assert tick.count("queue_leases_unreadable") == 1


def test_the_reader_remembers_a_lease_row_it_has_seen_and_times_how_long_the_read_has_failed(tmp_path: Path):
    runs = _runs(tmp_path)
    conn = open_store(run_store.store_url_for(runs), "2026-11-20T00:00:00Z")
    try:
        kl.acquire(conn, "chair:watch", "worker-a", 60)
    finally:
        conn.close()
    clock = iter([5.0, 80.0, 90.0])
    reader = chair_read_queues.QueueLeasesReader(runs, lambda: next(clock))
    first = reader()
    assert first.leases is not None and first.seen and first.failing_s == 0.0
    store = run_store.sqlite_path(run_store.store_url_for(runs))
    for suffix in ("", "-wal", "-shm"):
        Path(f"{store}{suffix}").unlink(missing_ok=True)
    Path(store).write_text("not a database", encoding="utf-8")
    failing = reader()
    assert (failing.leases, failing.seen, failing.failing_s) == (None, True, 0.0)
    later = reader()
    assert later.failing_s == 75.0  # timed from the first failed read (5 s to 80 s)
    assert reader.held("watch") is True  # unknown, and a worker has been seen: the tick leaves it alone
