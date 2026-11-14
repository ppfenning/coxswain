import json

from agent_tools import route, run_store, stats_efficiency

SINCE, UNTIL = "2026-10-01T00:00:00+00:00", "2026-10-08T00:00:00+00:00"
DAY = "2026-10-03"


def land(task, ts=f"{DAY}T12:00:00+00:00"):
    return json.dumps({"ts": ts, "run": "epic-1", "task": task, "steps_reached": ["push", "mark_done"], "exit": 0})


ALL_NULL = "\x00" * 20
EMBEDDED = "\x00" + land("c")[:10] + "\x00\x00" + land("c")[10:] + "\x00"
LINES = [land("a"), ALL_NULL, land("b"), EMBEDDED]


def test_stats_reader_keeps_good_rows_and_the_embedded_nul_row():
    assert stats_efficiency.landed_lands(LINES) == [(DAY, "a"), (DAY, "b"), (DAY, "c")]


def test_stats_first_log_day_ignores_an_all_null_line_and_reads_an_embedded_nul_row():
    assert stats_efficiency.first_log_day([ALL_NULL, EMBEDDED]) == DAY
    assert stats_efficiency.first_log_day([ALL_NULL]) is None


def test_route_landed_today_keeps_good_rows_and_the_embedded_nul_row():
    got = route.landed_today("\n".join(LINES) + "\n", DAY)
    assert got == [("epic-1", "a"), ("epic-1", "b"), ("epic-1", "c")]


def test_run_store_land_records_keep_good_rows_and_the_embedded_nul_row(tmp_path):
    (tmp_path / "land.jsonl").write_text("\n".join(LINES) + "\n")
    got = run_store.landed_tasks(tmp_path, SINCE, UNTIL)
    assert (got.source, sorted(r["task_id"] for r in got.rows)) == ("land_records", ["a", "b", "c"])


def test_run_store_land_records_survive_an_invalid_utf8_byte(tmp_path):
    body = "\n".join([land("a"), ALL_NULL, land("b")]) + "\n"
    (tmp_path / "land.jsonl").write_bytes(b"\xff\xfe broken\n" + body.encode())
    got = run_store.landed_tasks(tmp_path, SINCE, UNTIL)
    assert sorted(r["task_id"] for r in got.rows) == ["a", "b"]
