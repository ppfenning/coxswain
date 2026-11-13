"""The idle refresh: nothing changed costs under 10 ms, and a changed input recomputes only what reads it.

The workspace is the incremental tests' (store stand-in, run records, stubbed edges) with the live edge put back, a
projects dir of several transcripts, and a chair session among them.
"""

import json
import os
import time
from datetime import UTC, datetime

import pytest
from test_dash_feed_incremental import NOW, workspace  # noqa: F401

from agent_tools import dash_feed, dash_feed_cost, dash_live_turns_session

REAL_LIVE_EDGE = dash_feed._live_edge
SECTIONS = list(dash_feed_cost.SECTION_ORDER)
METERS = dash_feed._burn_meters(
    {"five_hour_fraction": 0.4, "five_hour_resets_at": "2026-11-09T14:00:00Z",
     "weekly_fraction": 0.5, "weekly_resets_at": "2026-11-12T04:00:00Z"},
    {"five_hour": {"cox_seats_usd": 4.0}, "weekly": {"cox_seats_usd": 5.0}}, 0.93,
)
STAMP = datetime(2026, 11, 9, 11, 59, tzinfo=UTC).timestamp()


def _line(n: int) -> str:
    message = {"id": f"msg-{n}", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 5}}
    return json.dumps({"type": "assistant", "timestamp": "2026-11-09T11:59:00.000Z", "message": message}) + "\n"


@pytest.fixture
def bench(workspace, monkeypatch):  # noqa: F811
    runs, work = workspace
    monkeypatch.setattr(dash_feed, "_live_edge", REAL_LIVE_EDGE)
    monkeypatch.setattr(dash_feed, "_burn_meters", lambda *a, **k: METERS)
    folder = runs.parent / "projects" / "proj"
    folder.mkdir()
    for name in ("abc", "s1", "s2", "s3"):
        path = folder / f"{name}.jsonl"
        path.write_text("".join(_line(n) for n in range(40)))
        os.utime(path, (STAMP, STAMP))
    return runs, work, folder


def _without_cost(feed: dict) -> str:
    return json.dumps({k: v for k, v in feed.items() if k != "cost"}, sort_keys=True)


def _warm(runs, work):
    return dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)


def _record_reads(monkeypatch) -> list[tuple[str, int]]:
    reads: list[tuple[str, int]] = []
    real = dash_live_turns_session._read_from

    def spy(path, offset):
        reads.append((path.name, offset))
        return real(path, offset)

    monkeypatch.setattr(dash_live_turns_session, "_read_from", spy)
    return reads


def test_an_unchanged_refresh_uses_under_10_ms_and_reuses_all_ten_sections(bench):
    runs, work, _ = bench
    first, cache = _warm(runs, work)
    assert first["live"] != dash_feed._zero_live(dash_feed._parse_now(NOW))
    before = time.process_time()
    again, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, cache)
    elapsed = time.process_time() - before
    assert len(SECTIONS) == 10
    assert again["cost"]["reused"] == SECTIONS
    assert again["cost"]["recomputed"] == []
    assert elapsed < 0.010, f"idle refresh used {elapsed * 1000:.2f} ms of process time"


def test_one_appended_line_recomputes_live_only_and_parses_only_its_bytes(bench, monkeypatch):
    runs, work, folder = bench
    _, cache = _warm(runs, work)
    target = folder / "s2.jsonl"
    size = target.stat().st_size
    with target.open("a") as handle:
        handle.write(_line(99))
    reads = _record_reads(monkeypatch)
    feed, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, cache)
    assert feed["cost"]["recomputed"] == ["live"]
    assert feed["cost"]["reused"] == [name for name in SECTIONS if name != "live"]
    assert reads == [("s2.jsonl", size)]
    assert target.stat().st_size - size == len(_line(99))


def test_a_changed_run_record_recomputes_only_the_sections_that_read_records(bench):
    runs, work, _ = bench
    _, cache = _warm(runs, work)
    (runs / "run-1.log").write_text("one\ntwo\n")
    feed, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, cache)
    reading = [name for name in SECTIONS if "records" in dash_feed._SECTION_GROUPS[name]]
    assert reading
    assert feed["cost"]["recomputed"] == reading
    assert feed["cost"]["reused"] == [name for name in SECTIONS if name not in reading]


def test_the_cached_feed_equals_the_uncached_feed_apart_from_cost(bench):
    runs, work, folder = bench
    _, cache = _warm(runs, work)
    idle, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, cache)
    assert _without_cost(idle) == _without_cost(dash_feed.gather_feed(runs, work, NOW))
    with (folder / "s1.jsonl").open("a") as handle:
        handle.write(_line(99))
    grown, _ = dash_feed.gather_feed_cached(runs, work, NOW, None, cache)
    assert _without_cost(grown) == _without_cost(dash_feed.gather_feed(runs, work, NOW))
    assert "cost" not in dash_feed.gather_feed(runs, work, NOW)
