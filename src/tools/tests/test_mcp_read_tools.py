import json

import pytest

from agent_tools import dash_board, dash_feed
from agent_tools.mcp_protocol import ToolError
from agent_tools.mcp_read_tools import (
    MAX_LIMIT,
    MAX_RESULT_CHARS,
    ReadPorts,
    board_tasks,
    fit,
    read_tools,
)

_TASKS = (
    ("alpha", "a1", "landed", 1.5), ("alpha", "a2", "merged", 0.25), ("alpha", "a3", "ready", 0.0),
    ("alpha", "a4", "building", 2.0), ("beta", "b1", "quarantined", 3.1234), ("beta", "b2", "approved", 0.5),
    ("beta", "b3", "dropped", 0.0),
)
_QUEUE = [{"initiative": "beta", "phases": [{
    "id": "p1", "wave": 1, "lane": None, "status": "blocked",
    "reason": "task b1 is quarantined: gate failed on lint",
    "tasks": [{"id": "b1", "state": "quarantined"}, {"id": "b2", "state": "approved"}],
}]}]
_RUNS = [{"run": "run-7", "machine": "m1", "phase": "p1", "node": "build", "attempt": 2, "turns": 3, "cost": 2.0,
          "verdict": "", "status": "running"}]
_RELEASE = {"version": "1.2.3", "pinned": {"version": "1.2.2"}, "last_cut": {"tag": "v1.2.2"}, "tap": None,
            "gate_progress": None}


def _feed(tasks=_TASKS, runs=_RUNS, queue=_QUEUE, short_ids=None):
    """A feed built by the real dash_board.build_board and dash_feed.snapshot, so the fixture keeps their shape."""
    board = dash_board.build_board(
        [{"initiative": i, "phase": "p1", "task_id": t} for i, t, _, _ in tasks],
        {(i, "p1", t): state for i, t, state, _ in tasks},
        {"a4": ("run-7", "m1")},
        {i: "e" for i, _, _, _ in tasks},
        {"alpha": "al"} if short_ids is None else short_ids,
        {t: cost for _, t, _, cost in tasks},
    )
    return dash_feed.snapshot("2026-11-10T00:00:00Z", {}, {}, [], runs, queue, len(queue), [], 0, {}, board=board)


def _ports(snapshot=None, events=(), unlaunched=None, calls=None, release=_RELEASE):
    def fake_events(since, limit):
        if calls is not None:
            calls.append((since, limit))
        return list(events)

    return ReadPorts(
        snapshot=lambda: snapshot or _feed(),
        release=lambda: release,
        unlaunched=unlaunched or (lambda ident: []),
        events=fake_events,
    )


def _run(ports, name, arguments=None):
    tool = next(t for t in read_tools(ports) if t.name == name)
    return tool.run(arguments or {}, "test")


def _full_queue():
    states = ("ready", "building", "approved", "quarantined")
    tasks = tuple(
        (f"initiative-with-a-long-slug-{n:03d}", f"initiative-with-a-long-slug-{n:03d}-task-{m:02d}", states[m % 4], 1.25)
        for n in range(50)
        for m in range(20)
    )
    return _feed(tasks, runs=[], queue=[], short_ids={})


def test_every_tool_is_read_only_and_takes_no_confirm():
    tools = read_tools(_ports())
    assert [t.name for t in tools] == ["release_status", "queue_board", "explain", "recent_events"]
    assert all(t.writes is False and t.destructive is False for t in tools)
    assert all("confirm" not in t.input_schema["properties"] for t in tools)


def test_release_status_counts_done_and_open_per_initiative():
    result = _run(_ports(), "release_status")
    assert result["version"] == "1.2.3"
    assert result["last_cut"] == {"tag": "v1.2.2"}
    assert result["initiatives"] == [
        {"initiative": "alpha", "done": 2, "open": 2},
        {"initiative": "beta", "done": 0, "open": 2},
    ]
    assert result["totals"] == {"done": 2, "open": 4}


def test_release_status_one_initiative_by_id_and_unknown_version():
    one = _run(_ports(), "release_status", {"id": "al"})
    assert one == {"initiative": "alpha", "done": 2, "open": 2,
                   "counts": {"ready": 1, "building": 1, "merged": 1, "landed": 1}}
    with pytest.raises(ToolError, match="9.9.9"):
        _run(_ports(), "release_status", {"version": "9.9.9"})
    with pytest.raises(ToolError, match="nope"):
        _run(_ports(), "release_status", {"id": "nope"})


def test_release_status_clips_oversized_release_facts():
    huge = {**_RELEASE, "last_cut": {"notes": "x" * 20000}, "pinned": {"p": ["y" * 50] * 500}, "tap": "z" * 9000}
    result = _run(_ports(release=huge), "release_status")
    assert len(json.dumps(result)) <= MAX_RESULT_CHARS
    assert set(result["last_cut"]) == {"clipped"}
    assert result["version"] == "1.2.3"


def test_queue_board_summary_is_compact_and_totals_cost():
    result = _run(_ports(), "queue_board")
    assert result["totals"]["quarantined"] == {"count": 1, "cost_usd": 3.1234}
    assert result["totals"]["building"] == {"count": 1, "cost_usd": 2.0}
    assert result["initiatives"] == ["alpha", "beta"]
    assert "rows" not in result


def test_queue_board_filters_by_state_with_detail():
    result = _run(_ports(), "queue_board", {"state": "quarantined", "detail": True})
    assert result["rows"] == [{"initiative": "beta", "task": "b1", "state": "quarantined", "cost_usd": 3.1234}]
    assert list(result["totals"]) == ["quarantined"]


def test_explain_unlaunched_initiative():
    asked = []

    def unlaunched(ident):
        asked.append(ident)
        return [{"kind": "unlaunched", "initiative": ident, "reason": "no lane free on an eligible host"}]

    result = _run(_ports(unlaunched=unlaunched), "explain", {"id": "al"})
    assert asked == ["alpha"]
    assert result == {"kind": "initiative", "id": "alpha", "unlaunched": True,
                      "reasons": ["no lane free on an eligible host"]}


def test_explain_quarantined_task_reads_the_reason_from_the_queue_phase():
    assert _run(_ports(), "explain", {"id": "b1"}) == {
        "kind": "task", "id": "b1", "state": "quarantined", "quarantine_reason": "gate failed on lint",
    }
    assert _run(_ports(), "explain", {"id": "b2"}) == {"kind": "task", "id": "b2", "state": "approved"}


def test_explain_live_task_gives_the_attempt_of_its_run():
    assert _run(_ports(), "explain", {"id": "a4"}) == {"kind": "task", "id": "a4", "state": "building", "attempt": 2}


def test_explain_unknown_id_names_it():
    with pytest.raises(ToolError, match="no-such-thing"):
        _run(_ports(), "explain", {"id": "no-such-thing"})


def _events(count):
    return [
        {"kind": "task_landed", "ts": f"2026-11-09T10:{n // 60:02d}:{n % 60:02d}Z", "task": f"t{n}"}
        for n in range(count)
    ]


def test_recent_events_caps_the_limit_before_the_size_ceiling_applies():
    calls = []
    result = _run(_ports(events=_events(300), calls=calls), "recent_events", {"limit": 500, "since": "2026-11-09"})
    assert calls == [("2026-11-09", MAX_LIMIT)]
    assert len(result["events"]) + result.get("omitted", 0) == MAX_LIMIT
    assert result["limit"] == MAX_LIMIT
    assert result["truncated"] is True
    assert "since" in result["more"]


def test_recent_events_returns_the_newest_first_within_the_limit():
    calls = []
    shuffled = _events(10)[::-1][3:] + _events(10)[:3]
    result = _run(_ports(events=shuffled, calls=calls), "recent_events", {"limit": 5})
    assert calls == [(None, 5)]
    assert [e["ts"] for e in result["events"]] == sorted(e["ts"] for e in shuffled)[::-1][:5]
    assert "truncated" not in result
    assert result["events"][0]["type"] == "task_landed"


def test_recent_events_default_limit_is_twenty():
    calls = []
    _run(_ports(calls=calls), "recent_events")
    assert calls == [(None, 20)]


def test_fit_truncates_and_says_how_to_fetch_the_rest():
    result = fit({"rows": ["x" * 100] * 200}, "rows", "ask for less")
    assert len(json.dumps(result)) <= MAX_RESULT_CHARS
    assert result["truncated"] is True
    assert result["omitted"] == 200 - len(result["rows"])
    assert result["more"] == "ask for less"
    assert fit({"rows": ["a"]}, "rows", "m") == {"rows": ["a"]}


def test_board_tasks_flattens_the_real_board_tree():
    assert [r["task"] for r in board_tasks(_feed())] == ["a1", "a2", "a3", "a4", "b1", "b2", "b3"]


def test_full_queue_results_stay_under_the_ceiling():
    ports = _ports(snapshot=_full_queue())
    assert len(board_tasks(_full_queue())) == 1000
    for name, arguments in (
        ("release_status", {}),
        ("queue_board", {}),
        ("queue_board", {"detail": True}),
        ("queue_board", {"detail": True, "state": "ready"}),
    ):
        result = _run(ports, name, arguments)
        assert len(json.dumps(result)) <= MAX_RESULT_CHARS, name
    detail = _run(ports, "queue_board", {"detail": True})
    assert detail["truncated"] is True
    assert "state" in detail["more"]
    assert detail["omitted"] + len(detail["rows"]) == 1000
    assert len(_run(ports, "release_status")["initiatives"]) == 50
