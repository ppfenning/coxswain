"""The dash feed's `intake` block: queued intake rows as the records dash_feed_intake_v1.json fixes."""

import json
from pathlib import Path

from agent_tools import dash_feed

_FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_intake_v1.json"


def _row(task_id, title, state="queued", kind="intake", **extra):
    return {"kind": kind, "initiative": "intake", "task_id": task_id, "state": state, "title": title, "extra": extra}


_ROWS = [
    _row("intake-slipstream-legend", "Sketch a legend for the Slipstream page", repo="coxswain-dash", stage="draft"),
    _row("intake-router-budget-bands", "Add budget bands to the router", repo="coxswain-tools", epic="cartridge-bounds",
         priority=1, target_release="2026.12.0"),
    _row("intake-tasks-pane-filter", "Filter the tasks pane by repo", repo="~/repos/coxswain-dash", epic="dash-waves",
         priority=5, target_release="2026.11.5"),
    _row("intake-ledger-compaction", "Compact the run ledger on a schedule", repo="coxswain-tools", priority=3),
    _row("intake-removed", "Gone", state="removed", priority=9),
    _row("intake-done", "Done", state="done", priority=9),
    _row("some-task", "A task", kind="task", priority=9),
]


def _ready(n, **extra):
    return _row(f"r{n}", f"Ready {n}", **extra)


def test_the_intake_the_feed_emits_from_literal_rows_equals_the_fixture():
    assert dash_feed._intake_v1(_ROWS, "2026.11.5") == json.loads(_FIXTURE.read_text())["intake"]


def test_equal_priorities_keep_row_order_and_drafts_follow_ready_rows():
    rows = [_row("d", "D", stage="draft", priority=9), _ready(1, priority=2), _ready(2, priority=2), _ready(3, priority=2)]

    assert [r["id"] for r in dash_feed._intake_v1(rows, "")] == ["r1", "r2", "r3", "d"]


def test_the_list_is_capped_at_the_queue_cap():
    rows = [_ready(n) for n in range(dash_feed._MAX_ROWS + 5)]

    assert len(dash_feed._intake_v1(rows, "")) == dash_feed._MAX_ROWS


def test_nothing_is_deferred_when_the_release_is_unknown():
    assert not any(r["deferred"] for r in dash_feed._intake_v1(_ROWS, ""))


def test_a_snapshot_with_no_intake_rows_has_an_empty_list():
    whole = dash_feed.snapshot("2026-11-07T00:00:00Z", {}, {}, [], [], [], 0, [], 0, [])

    assert whole["intake"] == []
    assert dash_feed._intake_v1([], "2026.11.5") == []
