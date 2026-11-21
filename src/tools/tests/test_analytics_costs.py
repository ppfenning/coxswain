"""Spend windows from the lake plus the store's tail (`analytics_costs`).

The pure core is tested on values. The edge is tested against a real migrated sqlite store and a real local Iceberg
lake synced by `lake_sync`, the default lake `cox lake sync` makes in the runs dir: nothing is mocked."""

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_tools import analytics_costs, run_store
from agent_tools.analytics_costs import (
    catalog_problem,
    chunks,
    lake_mark,
    parse_rows,
    sum_between,
    tail_run_ids,
    text_floor,
)

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


def _db(runs_dir: Path) -> sqlite3.Connection:
    return sqlite3.connect(run_store.sqlite_path(run_store.store_url_for(runs_dir)))


@pytest.fixture
def notes(monkeypatch, caplog):
    """The fallback warnings this test caused, in order; the last-reason state starts empty."""
    monkeypatch.setattr(analytics_costs, "_last_reason", None)
    caplog.set_level("WARNING", logger=analytics_costs.__name__)
    return lambda: [r.getMessage() for r in caplog.records if r.name == analytics_costs.__name__]


def test_without_a_lake_one_store_read_answers_every_window_as_cost_since_does_and_says_why(runs, notes):
    assert _windows(runs) == _store_truth(runs) == [1.5, 1.5, 0.5]
    assert not (runs / "lake-catalog.db").exists()  # reading never creates the lake
    [note] = notes()
    assert note.startswith("analytics: no lake catalog at ") and note.endswith("spend windows read the live store")


def test_the_lake_answers_for_the_runs_it_holds_and_the_store_for_every_other_run_id(runs, notes):
    # One test, two syncs: the lake's tables take seconds to create, and every step below needs one.
    _sync_lake(runs)  # the lake's mark is now r-ended's ended_at, 09:00
    assert _windows(runs) == _store_truth(runs) == [1.5, 1.5, 0.5]
    assert notes() == []

    # A run stamped ended at or before the mark after the sync (a backfilled ended_at, a slow clock): the sync never
    # takes it, so the store answers for it, by run id, now and after every later sync.
    with _db(runs) as db:
        db.execute("INSERT INTO runs (run_id, launched_at, ended_at) VALUES ('r-late', '2026-11-10T07:00:00+00:00', "
                   "'2026-11-10T08:30:00+00:00')")
        db.execute("INSERT INTO node_calls (call_id, run_id, seq, ts, cost_usd, ok) VALUES "
                   "('c-late', 'r-late', 1, '2026-11-10T08:10:00+00:00', 3.0, 1)")
        # A call whose run has no runs row, and one with no run id: cost_since counts both, so the windows do too.
        db.execute("INSERT INTO node_calls (call_id, run_id, seq, ts, cost_usd, ok) VALUES "
                   "('c-ghost', 'r-ghost', 1, '2026-11-10T08:20:00+00:00', 0.125, 1), "
                   "('c-none', NULL, 1, '2026-10-20T09:00:00+00:00', 0.0625, 1)")
    truth = [4.625, 4.625, 0.5625]
    assert _windows(runs) == _store_truth(runs) == truth
    _sync_lake(runs)
    assert _windows(runs) == _store_truth(runs) == truth

    # The runs the lake holds come from the lake alone: deleted from the store, the windows still count them.
    with _db(runs) as db:
        db.execute("DELETE FROM node_calls WHERE run_id = 'r-ended'")
    assert _windows(runs) == truth
    assert _store_truth(runs) == [3.375, 3.375, 0.0625]
    assert notes() == []


def test_a_lake_never_synced_falls_back_to_the_store_and_says_so(runs, notes):
    pytest.importorskip("pyiceberg")
    from agent_tools import lake_config, lake_tables

    lake_tables.ensure_tables(lake_config.load_catalog(lake_config.resolve_lake({}, runs)))
    assert _windows(runs) == _store_truth(runs)
    assert [n.split(";")[0] for n in notes()] == ["analytics: the lake's node_calls has never been synced (cox lake sync)"]


def test_an_unreadable_lake_falls_back_and_a_changed_reason_warns_again_but_a_repeat_does_not(runs, notes):
    assert _windows(runs) == _store_truth(runs)
    (runs / "lake-catalog.db").write_bytes(b"not a sqlite database" * 100)
    assert _windows(runs) == _store_truth(runs)
    assert _windows(runs) == _store_truth(runs)
    first, second = notes()
    assert first.startswith("analytics: no lake catalog at ")
    assert second.startswith("analytics: the lake could not be read: ")


def test_tail_run_ids_are_the_stores_ids_the_lake_does_not_hold():
    assert tail_run_ids(["r3", "r1", "r2", "r1"], {"r2", "r9"}) == ("r1", "r3")
    assert tail_run_ids([], {"r1"}) == ()


def test_chunks_split_an_in_list_without_losing_or_repeating_an_id():
    assert [list(c) for c in chunks(("a", "b", "c", "d", "e"), 2)] == [["a", "b"], ["c", "d"], ["e"]]
    assert list(chunks((), 2)) == []


def test_the_chair_path_does_not_load_the_lake_reader():
    probe = ("import sys, agent_tools.chair_read_idle_open; "
             "print(sorted(m for m in ('agent_tools.analytics_costs', 'pyiceberg') if m in sys.modules))")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert (done.returncode, done.stdout.strip()) == (0, "[]"), done.stderr


def test_a_window_before_the_rows_read_is_refused_rather_than_undercounted(runs):
    read = analytics_costs.cost_reader(runs, T0 - timedelta(days=1))
    with pytest.raises(ValueError, match="starts before the rows read"):
        read("2026-11-01T00:00:00+00:00", T0.isoformat())


def test_no_store_gives_none_so_the_caller_falls_back_to_usage_files(tmp_path):
    read = analytics_costs.cost_reader(tmp_path, T0 - timedelta(days=32))
    assert read(*WINDOWS[0]) is None
