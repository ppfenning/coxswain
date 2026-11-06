from __future__ import annotations

import json

import pytest

from agent_tools.dash_live_turns_lane import read_lane_turns, turns_from_events

TURN_KEYS = (
    "ts",
    "host",
    "source",
    "kind",
    "agent_id",
    "model",
    "run",
    "initiative",
    "task",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
)
PRICES = {"m1": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}
USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_input_tokens": 50000,
    "cache_creation_input_tokens": 2000,
}
STAMP = "2026-11-06T11:59:00Z"
# 1000 * 3 + 200 * 15 + 50000 * 0.3 + 2000 * 3.75 = 28500 micro-dollars, written out so a swapped price key fails
COST = 0.0285


def _event(message_id="msg_1", output=200, **extra):
    """A stream-json assistant event as the CLI writes it: no timestamp unless the test adds one."""
    usage = {**USAGE, "output_tokens": output}
    message = {"id": message_id, "model": "m1", "usage": usage, "content": [{"type": "text", "text": "x"}]}
    return {"type": "assistant", "message": message, "session_id": "s", **extra}


def _turns(events, **kw):
    args = {"host": "h", "run": "r-1", "initiative": "ini", "task": "t", "node": "build", "prices": PRICES} | kw
    return turns_from_events(events, **args)


def test_turn_with_usage_and_timestamp_has_the_contract_keys():
    (turn,) = _turns([_event(timestamp=STAMP)])
    assert tuple(turn) == TURN_KEYS
    assert (turn["ts"], turn["model"], turn["source"]) == (STAMP, "m1", "lane")


def test_event_without_timestamp_is_skipped():
    assert _turns([_event()]) == []


def test_event_without_usage_is_skipped():
    assert _turns([{"type": "assistant", "timestamp": STAMP, "message": {"id": "m", "model": "m1"}}]) == []


def test_cache_read_and_write_are_split_from_input():
    (turn,) = _turns([_event(timestamp=STAMP)])
    assert (turn["input_tokens"], turn["output_tokens"], turn["cache_read_tokens"], turn["cache_write_tokens"]) == (
        1000,
        200,
        50000,
        2000,
    )


def test_task_node_host_run_and_initiative_are_stamped():
    (turn,) = _turns([_event(timestamp=STAMP)], task="board-render", node="review", host="host-b")
    assert (turn["task"], turn["kind"], turn["host"], turn["run"], turn["initiative"]) == (
        "board-render",
        "review",
        "host-b",
        "r-1",
        "ini",
    )


def test_cost_comes_from_prices_unless_the_event_carries_one():
    assert _turns([_event(timestamp=STAMP)])[0]["cost_usd"] == pytest.approx(COST)
    assert _turns([_event(timestamp=STAMP, cost_usd=0.5)])[0]["cost_usd"] == 0.5


def test_an_unpriced_model_costs_zero():
    assert _turns([_event(timestamp=STAMP)], prices={})[0]["cost_usd"] == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-06T11:59:00", STAMP),
        ("2026-11-06T12:59:00+01:00", STAMP),
        ("2026-11-06T11:59:00.810Z", STAMP),
        ("yesterday", None),
    ],
)
def test_timestamps_are_normalised_to_utc_and_a_bad_one_is_skipped(raw, expected):
    turns = _turns([_event(timestamp=raw)])
    assert [t["ts"] for t in turns] == ([expected] if expected else [])


def test_content_blocks_of_one_message_count_once_and_the_fullest_usage_survives():
    events = [
        _event("a", output=10, timestamp=STAMP),
        _event("a", output=90, timestamp=STAMP),
        _event("a", output=40, timestamp=STAMP),
        _event("b", output=5, timestamp=STAMP),
    ]
    assert [t["output_tokens"] for t in _turns(events)] == [90, 5]


def test_equal_repeats_keep_the_earlier_event():
    events = [_event("a", timestamp="2026-11-06T11:00:00Z"), _event("a", timestamp="2026-11-06T11:00:01Z")]
    assert [t["ts"] for t in _turns(events)] == ["2026-11-06T11:00:00Z"]


def test_request_id_dedupes_when_there_is_no_message_id():
    events = [_event(None, requestId="q", timestamp=STAMP), _event(None, requestId="q", timestamp=STAMP)]
    assert len(_turns(events)) == 1


def test_events_without_any_id_each_count():
    no_id = {"type": "assistant", "timestamp": STAMP, "message": {"model": "m1", "usage": dict(USAGE)}}
    assert len(_turns([no_id, no_id])) == 2


def _write(path, *events, raw=b""):
    path.write_bytes(b"".join(json.dumps(e).encode() + b"\n" for e in events) + raw)
    return path


def test_real_shaped_trace_file_without_timestamps_yields_no_records(tmp_path):
    trace_dir = tmp_path / "r-1-trace"
    trace_dir.mkdir()
    _write(trace_dir / "build-1.jsonl", _event(), raw=b"not json\n")
    assert read_lane_turns(tmp_path, "r-1", "h", "ini", "t", "build", PRICES) == []


def test_trace_file_with_timestamps_yields_records_stamped_with_the_file_stem(tmp_path):
    trace_dir = tmp_path / "r-1-trace"
    trace_dir.mkdir()
    _write(trace_dir / "build-1.jsonl", _event(timestamp=STAMP), raw=b"not json\n")
    (turn,) = read_lane_turns(tmp_path, "r-1", "h", "ini", "t", "build", PRICES)
    assert (turn["ts"], turn["agent_id"], turn["kind"], turn["cost_usd"]) == (
        STAMP,
        "build-1",
        "build",
        pytest.approx(COST),
    )


def test_each_node_reads_only_its_own_role_files_including_hyphenated_roles(tmp_path):
    trace_dir = tmp_path / "r-1-trace"
    trace_dir.mkdir()
    for name, mid in (("build-1", "a"), ("review-1", "b"), ("review-adversary-2", "c"), ("notes", "d")):
        _write(trace_dir / f"{name}.jsonl", _event(mid, timestamp=STAMP))

    def seen(node):
        return [(t["kind"], t["agent_id"]) for t in read_lane_turns(tmp_path, "r-1", "h", "ini", "t", node, PRICES)]

    assert seen("build") == [("build", "build-1")]
    assert seen("review") == [("review", "review-1")]
    assert seen("review-adversary") == [("review-adversary", "review-adversary-2")]


def test_unreadable_files_yield_no_records_and_do_not_hide_readable_ones(tmp_path):
    trace_dir = tmp_path / "r-1-trace"
    trace_dir.mkdir()
    (trace_dir / "build-1.jsonl").write_bytes(b"\xff\xfe\x00bad")
    (trace_dir / "build-2.jsonl").mkdir()
    _write(trace_dir / "build-3.jsonl", _event(timestamp=STAMP))
    assert [t["agent_id"] for t in read_lane_turns(tmp_path, "r-1", "h", "ini", "t", "build")] == ["build-3"]


def test_missing_trace_directory_and_trace_directory_that_is_a_file_yield_no_records(tmp_path):
    assert read_lane_turns(tmp_path, "r-1", "h", "ini", "t", "build") == []
    (tmp_path / "r-2-trace").write_text("not a directory")
    assert read_lane_turns(tmp_path, "r-2", "h", "ini", "t", "build") == []
