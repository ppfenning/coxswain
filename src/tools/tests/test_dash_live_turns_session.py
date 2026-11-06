import json
import os
from datetime import UTC, datetime
from pathlib import Path

from test_dash_feed_board_contract import TURN_KEYS

from agent_tools.dash_live_turns_session import read_session_turns, turns_from_lines
from agent_tools.stats_chair import chair_cost

PRICES = {"claude-sonnet-5-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}
USAGE = {
    "input_tokens": 1800,
    "output_tokens": 420,
    "cache_read_input_tokens": 61000,
    "cache_creation_input_tokens": 2300,
}


def _assistant(
    ts: str | None = "2026-11-06T11:59:00.000Z",
    model: str = "claude-sonnet-5-5",
    message_id: str | None = "msg_1",
    request_id: str | None = None,
    usage: dict = USAGE,
) -> str:
    message = {"model": model, "usage": usage, **({} if message_id is None else {"id": message_id})}
    entry = {"type": "assistant", "message": message, **({} if request_id is None else {"requestId": request_id})}
    return json.dumps(entry if ts is None else {**entry, "timestamp": ts})


USER = json.dumps({"type": "user", "timestamp": "2026-11-06T11:58:00.000Z", "message": {"content": "hi"}})


def _turns(lines: list[str], prices: dict = PRICES) -> list[dict]:
    return turns_from_lines(lines, "host-a", "session", "chair", "", prices)


def test_an_assistant_turn_carries_the_turn_keys_tokens_and_priced_cost():
    [turn] = _turns([_assistant()])
    assert tuple(turn) == TURN_KEYS
    assert turn == {
        "ts": "2026-11-06T11:59:00.000Z",
        "host": "host-a",
        "source": "session",
        "kind": "chair",
        "agent_id": "",
        "model": "claude-sonnet-5-5",
        "run": "",
        "initiative": "",
        "task": "",
        "input_tokens": 1800,
        "output_tokens": 420,
        "cache_read_tokens": 61000,
        "cache_write_tokens": 2300,
        "cost_usd": 0.038625,
    }


def test_user_untimed_and_malformed_lines_are_skipped():
    assert _turns([USER, _assistant(ts=None), "{not json", "[]", "", _assistant(model="")]) == []


def test_a_line_with_a_non_integer_usage_count_is_skipped_and_the_rest_survive():
    bad = [
        {**USAGE, "input_tokens": "1800"},
        {**USAGE, "output_tokens": {"n": 1}},
        {**USAGE, "cache_read_input_tokens": True},
    ]
    lines = [_assistant(message_id=f"bad_{i}", usage=usage) for i, usage in enumerate(bad)]
    assert [t["ts"] for t in _turns([*lines, _assistant()])] == ["2026-11-06T11:59:00.000Z"]


def test_a_model_missing_from_the_prices_costs_a_float_zero():
    [turn] = _turns([_assistant()], prices={})
    assert turn["cost_usd"] == 0.0
    assert type(turn["cost_usd"]) is float


def test_content_block_lines_of_one_message_make_one_turn():
    assert len(_turns([_assistant(), _assistant(), _assistant(message_id="msg_2")])) == 2


def test_turn_costs_sum_to_chair_cost_when_lines_repeat_by_request_id_or_by_text():
    lines = [
        _assistant(message_id=None, request_id="req_1"),
        _assistant(message_id=None, request_id="req_1", ts="2026-11-06T11:59:01.000Z"),
        _assistant(message_id=None, request_id="req_2"),
        _assistant(message_id=None),
        _assistant(message_id=None),
    ]
    turns = _turns(lines)
    assert len(turns) == 3
    assert sum(t["cost_usd"] for t in turns) == chair_cost(lines, PRICES)


def _write(path: Path, *lines: str, at: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if at is not None:
        os.utime(path, (at.timestamp(), at.timestamp()))


FRESH = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SINCE = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)


def test_read_session_turns_labels_chair_operator_and_subagent_and_honours_since(tmp_path):
    stale = datetime(2026, 11, 1, tzinfo=UTC)
    project = tmp_path / "proj"
    _write(project / "chair-1.jsonl", _assistant("2026-11-06T11:00:00Z", message_id="a"), at=FRESH)
    _write(project / "other-2.jsonl", USER, _assistant("2026-11-06T10:00:00Z", message_id="b"), at=FRESH)
    _write(
        project / "chair-1" / "subagents" / "agent-abc.jsonl",
        _assistant("2026-11-06T10:30:00Z", message_id="c"),
        at=FRESH,
    )
    _write(project / "old-3.jsonl", _assistant("2026-11-01T10:00:00Z", message_id="d"), at=stale)
    (project / "bad-4.jsonl").write_bytes(b"\xff\xfe\x00 not utf-8\n")
    os.utime(project / "bad-4.jsonl", (FRESH.timestamp(), FRESH.timestamp()))

    turns = read_session_turns(tmp_path, "host-a", "chair-1", SINCE, prices=PRICES)

    assert [(t["ts"], t["source"], t["kind"], t["agent_id"]) for t in turns] == [
        ("2026-11-06T10:00:00Z", "session", "operator", ""),
        ("2026-11-06T10:30:00Z", "subagent", "subagent", "abc"),
        ("2026-11-06T11:00:00Z", "session", "chair", ""),
    ]
    assert {t["host"] for t in turns} == {"host-a"}
    assert {t["cost_usd"] for t in turns} == {0.038625}


def test_read_session_turns_ignores_dotted_stem_copies_of_sessions_and_agents(tmp_path):
    project = tmp_path / "proj"
    line = _assistant("2026-11-06T11:00:00Z")
    _write(project / "s1.jsonl", line, at=FRESH)
    _write(project / "s1.bak.jsonl", line, at=FRESH)
    _write(project / "s1" / "subagents" / "agent-abc.jsonl", line, at=FRESH)
    _write(project / "s1" / "subagents" / "agent-abc.bak.jsonl", line, at=FRESH)
    _write(project / "s1.bak" / "subagents" / "agent-abc.jsonl", line, at=FRESH)

    turns = read_session_turns(tmp_path, "h", "s1", SINCE, prices=PRICES)

    assert [(t["source"], t["agent_id"]) for t in turns] == [("session", ""), ("subagent", "abc")]


def test_read_session_turns_drops_turns_older_than_since_from_a_fresh_file(tmp_path):
    old = _assistant("2026-11-05T23:59:59Z", message_id="old")
    new = _assistant("2026-11-06T00:00:00Z", message_id="new")
    odd = _assistant("not a time", message_id="odd")
    _write(tmp_path / "p" / "s.jsonl", old, new, odd, at=FRESH)
    assert [t["ts"] for t in read_session_turns(tmp_path, "h", "s", SINCE)] == ["2026-11-06T00:00:00Z"]


def test_read_session_turns_without_prices_costs_zero_and_missing_dir_is_empty(tmp_path):
    since = datetime(2026, 11, 1, tzinfo=UTC)
    _write(tmp_path / "p" / "s.jsonl", _assistant())
    assert [t["cost_usd"] for t in read_session_turns(tmp_path, "h", "s", since)] == [0.0]
    assert read_session_turns(tmp_path / "absent", "h", "s", since) == []
