"""The kept chair_actions window: when a tick tops it up from its newest rows, and when it reads the whole window again.

The store is a list of rows; the two readers below answer from it exactly as the window's SQL does (`actions_query`,
`window_tail_query`), so every expectation is the store's own answer. `count_and_tail_on` runs that query on SQLite."""

from datetime import UTC, datetime, timedelta

from agent_tools.chair_action_rows import FrozenRow
from agent_tools.chair_actions_cache import ActionsCache, kept_from, tail_bound, topped_up
from agent_tools.chair_read_stale import _KEPT_KINDS, actions_cutoff

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def row(ts: str, kind: str = "status", target: str = "i1", holder: str = "chair") -> dict[str, object]:
    return {"ts": ts, "holder": holder, "kind": kind, "target": target, "status": "done", "action_json": {"kind": kind}}


def in_window(r: dict[str, object], cutoff: str) -> bool:
    return str(r["ts"]) >= cutoff or r["kind"] in _KEPT_KINDS


class Store:
    """A chair_actions table, insert-only, and the reads the cache sends it."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = list(rows)
        self.sent: list[str] = []
        self.tail_fails = False

    def full(self, runs_dir, now, window_days):
        self.sent.append("full")
        cutoff = actions_cutoff(now, window_days)
        return [dict(r) for r in self.rows if in_window(r, cutoff)]

    def tail(self, runs_dir, cutoff, since):
        self.sent.append(f"tail {since}")
        if self.tail_fails:
            return None
        window = [r for r in self.rows if in_window(r, cutoff)]
        return len(window), [dict(r) for r in window if str(r["ts"]) >= since]


def cache_on(store: Store) -> ActionsCache:
    return ActionsCache(full=store.full, tail=store.tail)


def as_set(rows) -> list[tuple]:
    return sorted((r["ts"], r["holder"], r["kind"], r["target"]) for r in rows)


BASE = [row("2026-11-17T09:00:00Z"), row("2026-11-18T11:00:00Z", target="i2"), row("2026-09-01T00:00:00Z", "needs_chair")]


def test_the_first_read_is_the_whole_window_and_a_quiet_tick_only_tails_it():
    store = Store(BASE + [row("2026-09-01T00:00:00Z", "status")])  # an old status row is outside the window
    cache = cache_on(store)
    first = cache.read(None, NOW, 7)
    second = cache.read(None, NOW, 7)
    assert as_set(first) == as_set(BASE) == as_set(second)
    assert store.sent == ["full", "tail 2026-11-18T10:45:00"]
    assert all(isinstance(r, FrozenRow) for r in second)
    assert second[0] is first[0]  # the same kept row, views and all
    assert cache.tick_counts() == ({"chair_actions": 1}, {"chair_actions": 1})


def test_a_new_row_is_added_once_after_the_kept_rows():
    store = Store(BASE)
    cache = cache_on(store)
    cache.read(None, NOW, 7)
    store.rows.append(row("2026-11-18T11:30:00Z", "needs_chair", "i3"))
    after = cache.read(None, NOW, 7)
    again = cache.read(None, NOW, 7)
    assert [r["target"] for r in after] == ["i1", "i2", "i1", "i3"]
    assert as_set(again) == as_set(after)
    assert store.sent == ["full", "tail 2026-11-18T10:45:00", "tail 2026-11-18T11:15:00"]


def test_a_row_written_behind_the_tail_bound_is_found_by_the_count_and_a_full_read():
    store = Store(BASE)
    cache = cache_on(store)
    cache.read(None, NOW, 7)
    store.rows.append(row("2026-11-18T08:00:00Z", target="late"))  # an hour behind the newest kept row
    found = cache.read(None, NOW, 7)
    assert "late" in [r["target"] for r in found]
    assert store.sent == ["full", "tail 2026-11-18T10:45:00", "full"]


def test_a_new_window_date_a_failed_tail_and_a_deleted_row_each_read_the_whole_window():
    store = Store(BASE)
    cache = cache_on(store)
    cache.read(None, NOW, 7)
    cache.read(None, NOW + timedelta(days=1), 7)
    assert store.sent[-1] == "full"
    store.tail_fails = True
    cache.read(None, NOW + timedelta(days=1), 7)
    assert store.sent[-2:] == ["tail 2026-11-18T10:45:00", "full"]
    store.tail_fails = False
    store.rows.pop(0)
    assert as_set(cache.read(None, NOW + timedelta(days=1), 7)) == as_set(store.full(None, NOW + timedelta(days=1), 7))


def test_an_empty_window_is_kept_and_tailed_from_the_start():
    store = Store([])
    cache = cache_on(store)
    assert cache.read(None, NOW, 7) == []
    store.rows.append(row("2026-11-18T11:00:00Z"))
    assert [r["target"] for r in cache.read(None, NOW, 7)] == ["i1"]
    assert store.sent == ["full", "tail "]


def test_tail_bound_is_the_margin_before_the_newest_ts_in_a_form_that_sorts_first():
    assert tail_bound("2026-11-18T11:00:00Z") == "2026-11-18T10:45:00"
    assert tail_bound("2026-11-18T11:00:00+00:00") == "2026-11-18T10:45:00"
    assert tail_bound("2026-11-18T13:00:00+02:00") == "2026-11-18T10:45:00"  # in UTC, the form the recorder writes
    bound = "2026-11-18T10:45:00"
    assert bound < "2026-11-18T10:45:00Z" and bound < "2026-11-18T10:45:00.5Z" and bound < "2026-11-18T10:45:00+00:00"
    assert tail_bound("") == ""
    assert tail_bound("not a time") is None


def test_topped_up_adds_by_primary_key_and_keeps_the_newest():
    kept = kept_from("2026-11-11", BASE)
    same = topped_up(kept, [dict(BASE[1])])
    assert same is kept
    grown = topped_up(kept, [dict(BASE[1]), row("2026-11-18T11:00:00Z", holder="other")])
    assert len(grown.rows) == 4 and grown.newest == "2026-11-18T11:00:00Z"
    assert kept_from("2026-11-11", []).newest == ""



class Counted:
    """A real SQLite connection that counts the statements sent through it."""

    def __init__(self, conn) -> None:
        self.conn, self.statements = conn, 0

    def execute(self, sql, params=()):
        self.statements += 1
        return self.conn.execute(sql, params)


def _sqlite_store(rows):
    import json
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE chair_actions (ts TEXT, epoch INTEGER, holder TEXT, kind TEXT, target TEXT, status TEXT, "
                 "reason TEXT, action_json TEXT, PRIMARY KEY (ts, holder, kind, target))")
    for r in rows:
        conn.execute("INSERT INTO chair_actions VALUES (?, 1, ?, ?, ?, ?, '', ?)",
                     (r["ts"], r["holder"], r["kind"], r["target"], r["status"], json.dumps(r["action_json"])))
    return conn


def test_the_count_and_the_tail_come_from_one_statement_so_one_snapshot():
    """Two statements could see two stores: a row committed between them is counted but not read, and so hides a late
    older row the tail missed. One statement reads one snapshot, so the count is always of the rows it returned beside."""
    from agent_tools.chair_read_stale import count_and_tail_on

    rows = BASE + [row("2026-09-01T00:00:00Z", "status"), row("2026-11-18T11:30:00Z", target="i3")]
    conn = Counted(_sqlite_store(rows))
    cutoff = actions_cutoff(NOW, 7)
    count, tail = count_and_tail_on(conn, "?", cutoff, "2026-11-18T10:45:00")
    assert conn.statements == 1
    assert count == len([r for r in rows if in_window(r, cutoff)]) == 4
    assert [r["target"] for r in tail] == ["i2", "i3"]
    assert set(tail[0]) == {"ts", "epoch", "holder", "kind", "target", "status", "reason", "action_json"}


def test_an_empty_tail_still_carries_the_count():
    from agent_tools.chair_read_stale import count_and_tail_on

    conn = Counted(_sqlite_store(BASE))
    assert count_and_tail_on(conn, "?", actions_cutoff(NOW, 7), "2026-11-19T00:00:00") == (3, [])
    assert count_and_tail_on(Counted(_sqlite_store([])), "?", actions_cutoff(NOW, 7), "") == (0, [])
