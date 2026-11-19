"""The reads one tick shares among its readers (H6): each gives what the reader's own read gave.

The decompose runs of every intake, from one `runs` read and one work item count; the landed task ids, from one glob
and each record's verdict kept by its file stamp; the land watch rows, from one pass over the window; and the timing
line's counts, from both caches."""

import os

from agent_tools import chair_decompose_runs as runs
from agent_tools import chair_revert_watch as watch
from agent_tools.chair_action_rows import freeze
from agent_tools.chair_read_approved import read_landed_task_ids
from agent_tools.chair_read_cache import StampCache
from agent_tools.chair_tick_cache import merged_counts, store_note

RUN_ROWS = [
    {"run_id": "a-2", "launched_at": "2026-10-02", "ended_at": "2026-10-02T01", "status": "done"},
    {"run_id": "b-1", "launched_at": "2026-10-01", "ended_at": None, "status": "running"},
    {"run_id": "a-1", "launched_at": "2026-10-01", "ended_at": "2026-10-01T01", "status": "done"},
    {"run_id": "a-10-1", "launched_at": "2026-10-03", "ended_at": None, "status": "running"},
    {"run_id": "a-3", "launched_at": "2026-10-04", "ended_at": None, "status": "refused"},
]


def test_runs_by_intake_is_select_runs_for_every_intake_at_once():
    grouped = runs.runs_by_intake(RUN_ROWS)
    for intake in ("a", "b", "a-10", "none"):
        assert grouped.get(intake, []) == runs.select_runs(RUN_ROWS, intake)
    assert sorted(grouped) == ["a", "a-10", "b"]


def test_item_counts_counts_each_initiative_and_a_failed_read_says_nothing():
    rows = [{"initiative": "a", "task_id": "t1"}, {"initiative": "a", "task_id": "t2"}, {"initiative": "b", "task_id": "t1"}]
    assert runs.item_counts(rows) == {"a": 2, "b": 1}
    assert runs.item_counts([]) == {}
    assert runs.item_counts(object()) is None


def test_the_tick_reads_give_each_intake_its_runs_logs_and_count(tmp_path):
    (tmp_path / "a-1.log").write_text("recorded a-1: 1 auto-applied, 0 gated decision(s), 0 proposal(s)\n")
    (tmp_path / "a-2.log").write_text("chair: plan approved but not executed: x\n")
    tick = runs.TickRuns(runs.runs_by_intake(RUN_ROWS), {"a": 3})

    found = runs.read_decompose_runs(tmp_path, "a", StampCache(8), tick)
    assert [(r.run_id, r.wrote_items, r.refusal_line) for r in found] == [
        ("a-1", False, None), ("a-2", True, "chair: plan approved but not executed: x"),
    ]
    assert [r.wrote_items for r in runs.read_decompose_runs(tmp_path, "a", None, runs.TickRuns(tick.runs, {}))] == [False, False]
    assert [r.wrote_items for r in runs.read_decompose_runs(tmp_path, "a", None, runs.TickRuns(tick.runs, None))] == [False, True]
    assert runs.read_decompose_runs(tmp_path, "zzz", None, tick) == []


def _record(runs_dir, run: str, task: str, text: str):
    path = runs_dir / run / "tasks" / "p1" / f"{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_landed_ids_with_a_cache_match_the_plain_read_and_follow_a_changed_or_removed_record(tmp_path):
    _record(tmp_path, "r-1", "t1", '{"landed": true}')
    second = _record(tmp_path, "r-1", "t2", '{"status": "approved"}')
    gone = _record(tmp_path, "r-2", "t3", '{"landed": true}')
    _record(tmp_path, "r-2", "t4", "not json, \"landed\"")
    cache = StampCache(64)

    assert read_landed_task_ids(tmp_path, cache) == read_landed_task_ids(tmp_path) == frozenset({"t1", "t3"})
    assert read_landed_task_ids(tmp_path, cache) == frozenset({"t1", "t3"})
    assert cache.hits == 4

    second.write_text('{"landed": true, "status": "approved"}', encoding="utf-8")
    os.utime(second, ns=(5_000_000_000, 5_000_000_000))
    gone.unlink()
    assert read_landed_task_ids(tmp_path, cache) == read_landed_task_ids(tmp_path) == frozenset({"t1", "t2"})
    assert len(cache) == 3


WATCH = {"status": "recorded", "initiative": "i", "phase": "p", "repo": "/r", "pr": 1}


def _watch_row(ts: str, kind: str, **doc: object) -> dict[str, object]:
    return {"ts": ts, "holder": "chair", "kind": kind, "target": "i", "status": "recorded", "action_json": {"kind": kind, **doc}}


def test_the_watch_rows_answer_every_watch_reader_as_all_rows_do():
    rows = [
        _watch_row("2026-11-01T00:00:00Z", "revert_watch", **{**WATCH, "commit": "c1"}),
        _watch_row("2026-11-01T00:01:00Z", "needs_chair", cause="no_run"),
        _watch_row("2026-11-01T00:02:00Z", "revert_watch", **{**WATCH, "commit": "c2", "initiative": "j"}),
        _watch_row("2026-11-01T00:03:00Z", "revert_watch_resolved", commit="c1", outcome="held"),
        _watch_row("2026-11-01T00:04:00Z", "status"),
    ]
    for given in (rows, [freeze(r) for r in rows]):
        kept = watch.watch_rows(given)
        assert len(kept) == 3
        for reader in (watch.pending_watches, watch.outcomes, watch.watches):
            assert reader(lambda kept=kept: kept) == reader(lambda given=given: given)
        assert watch.outcomes(lambda kept=kept: kept) == {"i": ["held"]}


def test_merged_counts_add_both_caches_for_the_timing_line():
    readers = ({"work_items": 2, "hosts": 1}, {"work_items": 5})
    actions_first, actions_quiet = ({"chair_actions": 1}, {"chair_actions": 0}), ({"chair_actions": 1}, {"chair_actions": 1})
    assert merged_counts(readers, actions_first) == ({"work_items": 2, "hosts": 1, "chair_actions": 1}, {"work_items": 5, "chair_actions": 0})
    before, after = merged_counts(readers, actions_first), merged_counts(readers, actions_quiet)
    assert store_note(before, after) == "store: read=none reused=1"
