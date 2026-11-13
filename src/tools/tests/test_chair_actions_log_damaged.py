import json

import pytest

from agent_tools.chair_read_record import ACTION_LOG, recorder
from agent_tools.dash_chair_action import read_current_action
from agent_tools.inbox_needs_chair import load_needs_chair_records

OPEN = {"ts": "t1", "kind": "land", "initiative": "init", "phase": "p1"}
NEEDS = {
    "ts": "2026-10-05T12:00:00+00:00",
    "kind": "needs_chair",
    "initiative": "init",
    "task_id": "t1",
    "cause": "code",
}


def _log(rows: list[dict], *extra: bytes) -> bytes:
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows) + b"".join(extra)


def test_a_recorded_action_lands_as_one_complete_line(tmp_path):
    record = recorder(tmp_path, lambda: 7, lambda: "t")
    record({"kind": "land", "initiative": "a"})
    record({"kind": "land", "initiative": "b"})
    raw = (tmp_path / ACTION_LOG).read_bytes()
    assert raw.endswith(b"\n")
    assert [json.loads(line) for line in raw.splitlines()] == [
        {"ts": "t", "epoch": 7, "kind": "land", "initiative": "a"},
        {"ts": "t", "epoch": 7, "kind": "land", "initiative": "b"},
    ]


def test_a_torn_tail_is_closed_before_the_next_action(tmp_path):
    (tmp_path / ACTION_LOG).write_bytes(b'{"ts": "t", "ki')
    recorder(tmp_path, lambda: 1, lambda: "t")({"kind": "land"})
    lines = (tmp_path / ACTION_LOG).read_bytes().splitlines()
    assert lines[0] == b'{"ts": "t", "ki'
    assert json.loads(lines[1])["kind"] == "land"


def test_a_missing_log_gives_the_usual_empty_results(tmp_path):
    assert read_current_action(tmp_path) is None
    assert load_needs_chair_records(tmp_path) == []


DAMAGE = {
    "all-null line": b"\x00" * 24 + b"\n",
    "embedded NULs": b'{"ts": "t2", "ki\x00\x00nd": "x"}\n\x00{"kind"\x00: "y"}\n',
}


@pytest.mark.parametrize("damage", DAMAGE.values(), ids=DAMAGE.keys())
def test_current_action_ignores_damaged_lines(tmp_path, damage):
    (tmp_path / ACTION_LOG).write_bytes(_log([OPEN]) + damage + _log([{"ts": "t3", "kind": "x", "status": "done"}]))
    assert read_current_action(tmp_path) == {"kind": "land", "target": "init/p1", "since": "t1"}


@pytest.mark.parametrize("damage", DAMAGE.values(), ids=DAMAGE.keys())
def test_open_needs_chair_rows_ignore_damaged_lines(tmp_path, damage):
    (tmp_path / ACTION_LOG).write_bytes(
        _log([NEEDS]) + damage + _log([{"kind": "land", "task_id": "t2", "status": "landed"}])
    )
    assert load_needs_chair_records(tmp_path) == [NEEDS]


def test_a_log_with_nuls_inside_a_good_row_still_reads_that_row(tmp_path):
    line = json.dumps(NEEDS).encode().replace(b'"kind"', b'"ki\x00nd"') + b"\n"
    (tmp_path / ACTION_LOG).write_bytes(line)
    assert load_needs_chair_records(tmp_path) == [NEEDS]
