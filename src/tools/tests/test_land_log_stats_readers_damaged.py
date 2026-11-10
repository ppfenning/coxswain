"""The land.jsonl readers in stats, route and run_store skip damaged lines and keep NUL-embedded rows that parse once stripped."""

import json

from agent_tools.route import landed_today
from agent_tools.run_store import landed_tasks
from agent_tools.stats_efficiency import first_log_day, landed_lands

STEPS = ["mark_done"]


def _row(task, ts="2026-09-22T10:00:00+00:00"):
    return {"run": "r1", "task": task, "exit": 0, "steps_reached": STEPS, "ts": ts}


# Two good rows, an all-NUL line, a row with NULs inside a key and a value, and plain garbage.
_NUL_ROW = json.dumps(_row("t3")).replace('"task"', '"ta\x00sk"').replace('"t3"', '"t\x003"')
LINES = [json.dumps(_row("t1")), "\x00" * 12, _NUL_ROW, "{not json", json.dumps(_row("t2"))]
TEXT = "\n".join(LINES) + "\n"


def test_landed_lands_keeps_good_and_nul_embedded_rows():
    assert landed_lands(LINES) == [("2026-09-22", "t1"), ("2026-09-22", "t3"), ("2026-09-22", "t2")]


def test_first_log_day_ignores_damaged_lines_and_reads_nul_embedded_ones():
    assert first_log_day(["\x00\x00", "{not json"]) is None
    assert first_log_day(["\x00\x00", json.dumps(_row("t3", "2026-09-20T00:00:00+00:00")).replace("2026", "20\x0026")]) == "2026-09-20"


def test_landed_today_keeps_good_and_nul_embedded_rows():
    assert landed_today(TEXT, "2026-09-22") == [("r1", "t1"), ("r1", "t3"), ("r1", "t2")]


def test_landed_tasks_reads_land_log_with_damaged_lines(tmp_path):
    (tmp_path / "land.jsonl").write_bytes(TEXT.encode() + b"\xff\xfe\n")
    landed = landed_tasks(tmp_path, "2026-09-22", "2026-09-23")
    assert sorted(row["task_id"] for row in landed.rows) == ["t1", "t2", "t3"]


def test_landed_tasks_is_empty_when_the_log_is_absent(tmp_path):
    assert landed_tasks(tmp_path, "2026-09-22", "2026-09-23").rows == ()
