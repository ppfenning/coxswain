"""Task states ready, building, approved, merged and landed, as the dash feed and `route status` show them."""

import json
from pathlib import Path

import pytest

from agent_tools import dash_feed, route

_FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_v1.json"
_SLUG = "dash-feed-streams-a-versioned-json-snapshot-of"


def _task(initiative, phase, task_id, state, needs=(), **extra):
    return {
        "kind": "task", "initiative": initiative, "phase": phase, "task_id": task_id, "state": state,
        "needs": list(needs), "extra": extra,
    }


_ROWS = [
    {"kind": "initiative", "initiative": _SLUG, "task_id": _SLUG, "phase": None, "state": "ready", "needs": [], "extra": {"priority": 1}},
    _task(_SLUG, "p1-foundations", "feed-schema", "approved", priority=1),
    _task(_SLUG, "p1-foundations", "feed-merged", "approved", merged=True),
    _task(_SLUG, "p1-foundations", "feed-landed", "done"),
    _task(_SLUG, "p2-feed", "feed-queue", "ready", needs=["feed-schema"]),
    {**_task("unnumbered-initiative", "p1-foundations", "only-task", "ready"), "extra": {"priority": 2}},
]


def _queue_section(monkeypatch, tmp_path):
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: _ROWS)
    monkeypatch.setattr(dash_feed, "_task_records", lambda runs_dir: [])
    rows, _ = dash_feed._queue(tmp_path, ())
    return [dash_feed._queue_v1(row, {_SLUG: "I412"}, {_SLUG: "example-repo"}) for row in rows]


def _without_total(queue):
    # The fixture counts a third phase whose tasks the feed has no word for; rows cannot produce an unlisted phase.
    return [{key: value for key, value in row.items() if key != "phases_total"} for row in queue]


def test_the_queue_the_feed_emits_from_literal_rows_equals_the_fixtures(monkeypatch, tmp_path):
    fixture = json.loads(_FIXTURE.read_text())
    queue = _queue_section(monkeypatch, tmp_path)

    assert _without_total(queue) == _without_total(fixture["queue"])
    totals = [row["phases_total"] for row in fixture["queue"]]
    whole = dash_feed.snapshot(
        fixture["at"], fixture["chair"], fixture["spend"], fixture["machines"], fixture["runs"],
        [{**row, "phases_total": total} for row, total in zip(queue, totals, strict=True)], fixture["queue_total"],
        fixture["inbox"], fixture["inbox_total"], fixture["watch"], fixture["decisions"], fixture["spend_series"],
        fixture["history"], fixture["history_today"], chair_agents=fixture["chair_agents"], spend_split=fixture["spend_split"],
        spend_sources=fixture["spend_sources"], budget=fixture["budget"], board=fixture["board"],
        live=fixture["live"],
    )
    # orchestration is emitted by the producer task; drop this filter there.
    assert whole == {key: value for key, value in fixture.items() if key != "orchestration"}


@pytest.mark.parametrize(
    ("task", "on_live_lane", "land_at", "word"),
    [
        (_task("i", "p", "t", "ready"), False, "task", "ready"),
        (_task("i", "p", "t", "in_progress"), True, "task", "building"),
        (_task("i", "p", "t", "approved"), False, "task", "approved"),
        (_task("i", "p", "t", "approved", merged=True), False, "task", "merged"),
        (_task("i", "p", "t", "done"), False, "task", "landed"),
        (_task("i", "p", "t", "done", land_at="task"), False, "task", "landed"),
        (_task("i", "p", "t", "done", land_at="ticket"), False, "task", "merged"),
    ],
)
def test_a_store_row_reads_as_one_word_of_the_vocabulary(task, on_live_lane, land_at, word):
    assert dash_feed._task_state(task, set(), on_live_lane, False, land_at) == word


def test_the_configured_granularity_is_the_initiatives_land_at_else_ticket():
    rows = [{"kind": "initiative", "initiative": "a", "extra": {"land_at": "task"}}, {"kind": "initiative", "initiative": "b", "extra": {}}]

    assert (dash_feed._configured_land_at(rows, "a"), dash_feed._configured_land_at(rows, "b")) == ("task", "ticket")


def _item(task_id, state, merged=False, **fields):
    return {"id": task_id, "initiative": "i", "phase": "p1", "state": state, "needs": [], **({"merged": True} if merged else {}), **fields}


def test_an_approved_task_awaits_a_merge_and_a_merged_one_does_not():
    approved = [_item("a", "approved")]
    merged = [_item("a", "approved", merged=True)]

    assert route._initiative_summary("i", approved) == {"id": "i", "phase": None, "ready": 0, "awaiting_merge": 1}
    assert route._initiative_summary("i", merged) is None


@pytest.mark.parametrize(("state", "counted"), [("ready", False), ("in_progress", False), ("done", False), ("approved", True)])
def test_only_an_approved_task_counts_as_awaiting_merge(state, counted):
    assert route.is_awaiting_merge(_item("a", state)) is counted


def test_a_merged_state_is_a_known_state():
    assert route.state_problems([{"initiative": "i", "file": "f", "state": "merged"}]) == []


def test_waiting_lines_print_the_remedy_for_an_approved_need_and_not_a_merged_one():
    remedies = {"dep": "  run `cox land dep`"}
    waiter = _item("t", "ready", needs=["dep"])

    approved = route.waiting_lines([waiter, _item("dep", "approved")], remedies, "i")
    merged = route.waiting_lines([waiter, _item("dep", "approved", merged=True)], remedies, "i")
    building = route.waiting_lines([waiter, _item("dep", "in_progress")], remedies, "i")

    assert approved == ["t waits on dep (approved)", "  run `cox land dep`"]
    assert merged == ["t waits on dep (merged)"]
    assert building == ["t waits on dep (in_progress)"]
    assert route.waiting_lines([waiter, _item("dep", "done")], remedies, "i") == []


def test_the_store_merge_flag_reaches_the_items_and_the_docket_summary():
    rows = [_task("i", "p1", "a", "approved", merged=True), _task("i", "p1", "b", "approved")]
    items = route._task_items(rows)

    assert [i.get("merged", False) for i in items] == [True, False]
    entry = {"id": "i", "ready_tasks": []}
    assert route._initiative_from_docket(entry, items) == {"id": "i", "phase": None, "ready": 0, "awaiting_merge": 1}


def test_with_store_states_carries_the_merge_flag_of_the_stored_row():
    items = [_item("a", "ready"), _item("b", "ready")]
    rows = [_task("i", "p1", "a", "approved", merged=True), _task("i", "p1", "b", "approved")]

    stored = route.with_store_states(items, rows, "store")

    assert [(i["state"], route.state_word(i)) for i in stored] == [("approved", "merged"), ("approved", "approved")]
