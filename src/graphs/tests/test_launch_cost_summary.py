import harness.store_read as read
from harness.cli import initiative_cost_line, initiative_of_run
from harness.store_migrate import open_store

NOW = "2026-11-10T00:00:00+00:00"


def put(conn, table, **row):
    marks = ", ".join(conn.dialect.placeholder for _ in row)
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({marks})", tuple(row.values()))


def call(conn, call_id, run_id, seq, task, cost, ts):
    put(conn, "node_calls", call_id=call_id, run_id=run_id, seq=seq, task_id=task, role="build", cost_usd=cost, ts=ts)


def seeded():
    c = open_store("sqlite:///:memory:", NOW)
    for run in ("ini-1", "ini-2", "other-1", "ini-x-1"):
        put(c, "runs", run_id=run, principal="operator", status="done", started_at=NOW)
    call(c, "a1", "ini-1", 1, "t1", 1.0, "2026-11-10T01:00:00+00:00")
    call(c, "a2", "ini-1", 2, "t2", 0.5, "2026-11-10T01:05:00+00:00")
    put(c, "attempts", run_id="ini-1", task_id="t1", seq=0, kind="verify", reason="lint", ts="2026-11-10T01:10:00+00:00")
    call(c, "b1", "ini-2", 1, "t1", 0.75, "2026-11-10T02:00:00+00:00")
    call(c, "b2", "ini-2", 2, "t3", 0.25, "2026-11-10T02:05:00+00:00")
    call(c, "o1", "other-1", 1, "t1", 9.0, "2026-11-10T03:00:00+00:00")
    call(c, "x1", "ini-x-1", 1, "t1", 7.0, "2026-11-10T03:00:00+00:00")
    return c


def test_line_has_total_and_whole_percent():
    assert initiative_cost_line({"total_usd": 12.5, "retry_usd": 2.5}) == "  initiative cost: $12.50 across its runs, 20% on retries"


def test_line_rounds_share_to_whole_percent():
    assert initiative_cost_line({"total_usd": 3.0, "retry_usd": 1.0}).endswith(", 33% on retries")


def test_line_for_zero_total_has_no_share():
    assert initiative_cost_line({"total_usd": 0.0, "retry_usd": 0.0}) == "  initiative cost: $0.00 across its runs"


def test_line_for_no_read_is_none():
    assert initiative_cost_line(None) is None


def test_initiative_of_run():
    assert [initiative_of_run(r) for r in ("ini-2", "a-b-14", "epic-date-ab12", "ini", "-3")] == ["ini", "a-b", None, None, None]


def test_cost_sums_two_runs_and_counts_calls_after_the_first_attempt_as_retry():
    c = seeded()
    try:
        assert read.initiative_cost(c, "ini") == {"total_usd": 2.5, "retry_usd": 0.75}
    finally:
        c.close()


def test_cost_leaves_out_other_initiatives_and_lookalike_run_ids():
    c = seeded()
    try:
        assert read.initiative_cost(c, "other") == {"total_usd": 9.0, "retry_usd": 0.0}
        assert read.initiative_cost(c, "ini-x") == {"total_usd": 7.0, "retry_usd": 0.0}
    finally:
        c.close()


def test_cost_is_none_without_runs():
    c = seeded()
    try:
        assert read.initiative_cost(c, "missing") is None
    finally:
        c.close()
