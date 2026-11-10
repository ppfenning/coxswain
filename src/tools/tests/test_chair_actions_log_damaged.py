import os

from agent_tools.chair_read_record import ACTION_LOG, action_line, recorder
from agent_tools.dash_chair_action import read_current_action
from agent_tools.inbox_needs_chair import load_needs_chair_records
from agent_tools.jsonl_log import read_log

OPEN = '{"kind": "launch", "initiative": "i", "phase": "p", "ts": "t1"}\n'
NEEDS = {"kind": "needs_chair", "initiative": "i", "phase": "q", "cause": "ticket", "status": "recorded"}
NEEDS_WITH_NULS = (
    '{"kind": "needs_chair", "ini\x00tiative": "i", "phase"\x00: "q", "cause": "ti\x00cket", "status": "recorded"}\n'
)


def _record(runs_dir, action):
    recorder(runs_dir, lambda: 3, lambda: "2026-11-10T00:00:00Z")(action)


def test_a_recorded_action_lands_as_one_complete_line_in_one_write(tmp_path, monkeypatch):
    writes = []
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or real_write(fd, data))
    _record(tmp_path, {"kind": "land", "status": "landed"})
    expected = action_line({"kind": "land", "status": "landed"}, 3, "2026-11-10T00:00:00Z") + "\n"
    assert writes == [expected.encode("utf-8")]
    assert (tmp_path / ACTION_LOG).read_bytes() == expected.encode("utf-8")


def test_non_ascii_and_float_values_keep_the_bytes_action_line_gives(tmp_path):
    action = {"kind": "land", "reason": "café ✓", "ratio": 0.1, "big": 1e22, "nested": {"b": 1, "a": [1.5, None]}}
    _record(tmp_path, action)
    expected = action_line(action, 3, "2026-11-10T00:00:00Z") + "\n"
    assert (tmp_path / ACTION_LOG).read_bytes() == expected.encode("utf-8")


def test_a_record_after_a_torn_tail_starts_its_own_line(tmp_path):
    (tmp_path / ACTION_LOG).write_bytes(b'{"kind": "land"')
    _record(tmp_path, {"kind": "land", "status": "landed"})
    parsed = read_log(tmp_path / ACTION_LOG)
    assert [row["status"] for row in parsed.rows] == ["landed"]
    assert parsed.skipped == 1


def test_both_readers_skip_an_all_null_line_and_strip_embedded_nuls(tmp_path):
    (tmp_path / ACTION_LOG).write_text(OPEN + "\x00" * 16 + "\n" + NEEDS_WITH_NULS, encoding="utf-8")
    assert read_current_action(tmp_path) == {"kind": "launch", "target": "i/p", "since": "t1"}
    assert load_needs_chair_records(tmp_path) == [NEEDS]


def test_both_readers_give_their_usual_result_for_good_rows(tmp_path):
    (tmp_path / ACTION_LOG).write_text(OPEN + action_line(NEEDS, 1, "t2") + "\n", encoding="utf-8")
    assert read_current_action(tmp_path) == {"kind": "launch", "target": "i/p", "since": "t1"}
    assert load_needs_chair_records(tmp_path) == [{**NEEDS, "ts": "t2", "epoch": 1}]


def test_both_readers_return_the_missing_log_result(tmp_path):
    assert read_current_action(tmp_path) is None
    assert load_needs_chair_records(tmp_path) == []
