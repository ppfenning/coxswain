"""Spend windows from the lake plus the store's tail (`analytics_costs`).

The pure core is tested on values. The edge is tested against a real migrated sqlite store and a real local Iceberg
lake synced by `lake_sync`, the default lake `cox lake sync` makes in the runs dir: nothing is mocked."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_tools import analytics_costs, run_store
from agent_tools.analytics_costs import catalog_problem, lake_mark, parse_rows, sum_between, text_floor

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


# --- the pure core ------------------------------------------------------------------------------------------------


def test_parse_rows_reads_every_iso_shape_as_a_utc_instant_and_drops_what_is_not_a_time():
    rows = parse_rows([
        ("2026-11-10T10:00:00Z", 1.0),
        ("2026-11-10T05:00:00-05:00", 2),
        ("2026-11-10T10:00:00", None),
        (datetime(2026, 11, 10, 10, tzinfo=UTC), 0.5),
        ("not a time", 9.0),
        (None, 9.0),
    ])
    ten = datetime(2026, 11, 10, 10, tzinfo=UTC)
    assert rows == ((ten, 1.0), (ten, 2.0), (ten, 0.0), (ten, 0.5))


def test_sum_between_is_half_open_and_compares_instants_not_text():
    costs = parse_rows([
        ("2026-11-10T00:00:00+00:00", 1.0),   # at since: in
        ("2026-11-10T23:30:00-05:00", 2.0),   # 04:30 UTC on the 11th: out, though its text sorts inside
        ("2026-11-10T23:59:59.5Z", 4.0),      # just before until: in
        ("2026-11-11T00:00:00Z", 8.0),        # at until: out
    ])
    assert sum_between(costs, "2026-11-10T00:00:00+00:00", "2026-11-11T00:00:00+00:00") == 5.0
    # the same window named in another zone is the same window
    assert sum_between(costs, "2026-11-09T19:00:00-05:00", "2026-11-10T19:00:00-05:00") == 5.0


def test_sum_between_refuses_a_bound_that_is_not_a_time():
    with pytest.raises(ValueError, match="not an ISO time"):
        sum_between((), "yesterday", "2026-11-11")


def test_text_floor_is_a_utc_date_a_day_early_so_text_order_cannot_drop_a_call():
    assert text_floor(datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=14)))) == "2026-10-30"
    assert text_floor(T0) == "2026-11-09"


def test_lake_mark_is_the_synced_ended_at_or_none():
    assert lake_mark({"coxswain.hwm": "2026-11-10T05:00:00+00:00"}, "coxswain.hwm") == "2026-11-10T05:00:00+00:00"
    assert lake_mark({}, "coxswain.hwm") is None
    assert lake_mark({"coxswain.hwm": ""}, "coxswain.hwm") is None


def test_catalog_problem_is_only_a_missing_sqlite_catalog_file():
    assert catalog_problem("sqlite:////r/lake-catalog.db", False) == "no lake catalog at /r/lake-catalog.db"
    assert catalog_problem("sqlite:////r/lake-catalog.db", True) is None
    assert catalog_problem("postgresql://u@h/lake", False) is None


# --- the edge, against a real store and a real lake -----------------------------------------------------------------

ENDED = "2026-11-10T09:00:00+00:00"
CALLS = (
    ("c-old-1", "r-ended", "2026-11-10T08:00:00+00:00", 1.25),
    ("c-old-2", "r-ended", "2026-10-20T08:00:00+00:00", 0.5),
    ("c-older", "r-ended", "2026-09-01T08:00:00+00:00", 64.0),   # before the read's floor: never summed
    ("c-live", "r-live", "2026-11-10T11:00:00+00:00", 0.25),
)
WINDOWS = (
    ("2026-11-01T00:00:00+00:00", T0.isoformat()),
    ("2026-11-10T00:00:00+00:00", "2026-11-11T00:00:00+00:00"),
    ("2026-10-20T00:00:00+00:00", "2026-10-21T00:00:00+00:00"),
)


@pytest.fixture
def runs(tmp_path) -> Path:
    """A migrated sqlite store in the runs dir: one ended run and one still running, with their calls."""
    from harness.store_dialect import connect
    from harness.store_migrate import default_modules, migrate

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    url = run_store.store_url_for(runs_dir)
    conn = connect(url)
    try:
        migrate(conn, "2026-01-01T00:00:00Z", default_modules())
    finally:
        conn.close()
    with sqlite3.connect(run_store.sqlite_path(url)) as db:
        db.execute("INSERT INTO runs (run_id, launched_at, ended_at) VALUES ('r-ended', '2026-09-01T07:00:00+00:00', ?)", (ENDED,))
        db.execute("INSERT INTO runs (run_id, launched_at, ended_at) VALUES ('r-live', '2026-11-10T10:00:00+00:00', NULL)")
        db.executemany("INSERT INTO node_calls (call_id, run_id, seq, ts, cost_usd, ok) VALUES (?, ?, 1, ?, ?, 1)", CALLS)
    return runs_dir


def _store_truth(runs_dir: Path) -> list[float | None]:
    return [run_store.cost_since(runs_dir, since, until) for since, until in WINDOWS]


def _sync_lake(runs_dir: Path) -> None:
    pytest.importorskip("pyiceberg")
    from agent_tools import lake_config, lake_sync, lake_tables

    catalog = lake_config.load_catalog(lake_config.resolve_lake({}, runs_dir))
    lake_tables.ensure_tables(catalog)
    lake_sync.sync(catalog, run_store.store_url_for(runs_dir))


def _windows(runs_dir: Path) -> list[float | None]:
    read = analytics_costs.cost_reader(runs_dir, T0 - timedelta(days=32))
    return [read(since, until) for since, until in WINDOWS]


def test_without_a_lake_one_store_read_answers_every_window_as_cost_since_does_and_says_why(runs):
    rows = analytics_costs.window_rows(runs, T0 - timedelta(days=32))

    assert rows is not None and rows.source == "store" and rows.note.startswith("no lake catalog at ")
    assert not (runs / "lake-catalog.db").exists()  # reading never creates the lake
    assert _windows(runs) == _store_truth(runs) == [1.5, 1.5, 0.5]


def test_a_synced_lake_answers_for_ended_runs_and_the_store_only_for_runs_it_does_not_hold(runs):
    # One test, two syncs: the lake's tables take seconds to create, and every step below needs one.
    _sync_lake(runs)
    rows = analytics_costs.window_rows(runs, T0 - timedelta(days=32))
    assert rows is not None and (rows.source, rows.note) == ("lake", None)
    assert _windows(runs) == _store_truth(runs) == [1.5, 1.5, 0.5]

    # A run that ends after the sync is read from the store until the next sync, which appends it once.
    store = run_store.sqlite_path(run_store.store_url_for(runs))
    with sqlite3.connect(store) as db:
        db.execute("UPDATE runs SET ended_at = '2026-11-10T11:30:00+00:00' WHERE run_id = 'r-live'")
    assert _windows(runs) == _store_truth(runs) == [1.5, 1.5, 0.5]
    _sync_lake(runs)
    assert _windows(runs) == [1.5, 1.5, 0.5]

    # Every call now comes from the lake: gone from the store, the windows still count them, and cost_since does not.
    with sqlite3.connect(store) as db:
        db.execute("DELETE FROM node_calls")
    assert _windows(runs) == [1.5, 1.5, 0.5]
    assert _store_truth(runs) == [0.0, 0.0, 0.0]


def test_a_window_before_the_rows_read_is_refused_rather_than_undercounted(runs):
    read = analytics_costs.cost_reader(runs, T0 - timedelta(days=1))
    with pytest.raises(ValueError, match="starts before the rows read"):
        read("2026-11-01T00:00:00+00:00", T0.isoformat())


def test_no_store_gives_none_so_the_caller_falls_back_to_usage_files(tmp_path):
    read = analytics_costs.cost_reader(tmp_path, T0 - timedelta(days=32))
    assert read(*WINDOWS[0]) is None
