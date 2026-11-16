"""The activity block reads operator transcripts past a cursor and is held under a key, so an idle refresh reuses
every section and parses no transcript line."""

import json
from collections import Counter
from types import SimpleNamespace

import pytest
from test_dash_feed_incremental import LATER, NOW, workspace  # noqa: F401
from test_dash_feed_operator_sessions import _AT, _SESSION_A, _SPEND, _line, world  # noqa: F401

from agent_tools import chair_activity_read, dash_feed, dash_feed_cost

REAL_ACTIVITY = dash_feed._activity
SECTIONS = list(dash_feed_cost.SECTION_ORDER)


@pytest.fixture
def tmp_path(short_tmp_path):
    """The project folder is named for the whole workspace path, so the workspace's path stays short."""
    return short_tmp_path


@pytest.fixture
def reads(monkeypatch):
    """Every transcript byte read, as (file name, offset), through either the cursor reader or the plain one."""
    seen: list[tuple[str, int]] = []
    read_from, read_lines = chair_activity_read._read_from, chair_activity_read._read_lines

    def counted_from(path, offset):
        seen.append((path.name, offset))
        return read_from(path, offset)

    def counted_lines(path):
        seen.append((path.name, 0))
        return read_lines(path)

    monkeypatch.setattr(chair_activity_read, "_read_from", counted_from)
    monkeypatch.setattr(chair_activity_read, "_read_lines", counted_lines)
    return seen


def _cached(world, cache, session=_SESSION_A):  # noqa: F811
    root, profile, runs, _ = world
    out = {}
    built = dash_feed._activity(
        runs, profile, session, root, _SPEND, _AT, {}, reader_cache=cache, on_reader_cache=out.update,
    )
    return built, out


def test_a_cached_activity_equals_an_uncached_one(world):  # noqa: F811
    root, profile, runs, _ = world
    plain = dash_feed._activity(runs, profile, _SESSION_A, root, _SPEND, _AT, {})
    built, cache = _cached(world, {})
    assert built == plain
    assert cache


def test_a_second_call_on_unchanged_files_reads_no_transcript_byte(world, reads):  # noqa: F811
    _, cache = _cached(world, {})
    reads.clear()
    again, _ = _cached(world, cache)
    assert reads == []
    root, profile, runs, _ = world
    assert again == dash_feed._activity(runs, profile, _SESSION_A, root, _SPEND, _AT, {})


def test_an_appended_line_is_read_from_its_offset_only(world, reads):  # noqa: F811
    root, profile, runs, projects = world
    _, cache = _cached(world, {})
    transcript = projects / chair_activity_read.project_folder_name(root) / f"{_SESSION_A}.jsonl"
    before = transcript.stat().st_size
    with transcript.open("a") as fh:
        fh.write(_line("a3", "2026-10-16T17:50:00Z", 1_000_000) + "\n")
    reads.clear()
    built, _ = _cached(world, cache)
    assert reads and set(reads) == {(transcript.name, before)}
    assert built == dash_feed._activity(runs, profile, _SESSION_A, root, _SPEND, _AT, {})


@pytest.fixture
def idle(workspace, monkeypatch):  # noqa: F811
    """The incremental workspace on a Postgres-style store, with the real activity block and one operator transcript."""
    runs, work = workspace
    stamp = {"value": (("runs", 5),)}
    calls: Counter = Counter()

    def activity(*args, **kwargs):
        calls["activity"] += 1
        return REAL_ACTIVITY(*args, **kwargs)

    seat = SimpleNamespace(spent_usd=0.0)
    monkeypatch.setattr(dash_feed, "_store_is_local_sqlite", lambda runs_dir: False)
    monkeypatch.setattr(dash_feed.run_store, "store_change_stamp", lambda runs_dir: stamp["value"])
    monkeypatch.setattr(dash_feed, "_activity", activity)
    monkeypatch.setattr(dash_feed.usage_window, "gather", lambda *a, **k: seat)
    monkeypatch.setattr(dash_feed.usage_window, "gather_weekly", lambda *a, **k: seat)
    folder = runs.parent / "projects" / chair_activity_read.project_folder_name(dash_feed._workspace(work))
    folder.mkdir(parents=True)
    transcript = folder / "s1.jsonl"
    transcript.write_text(_line("m1", "2026-11-09T11:59:00Z", 10, 5) + "\n")
    return runs, work, transcript, stamp, calls


def _without_cost(feed: dict) -> str:
    return json.dumps({k: v for k, v in feed.items() if k != "cost"}, sort_keys=True)


def test_an_idle_refresh_reuses_every_section_and_parses_no_transcript_line(idle, reads):
    runs, work, _, _, calls = idle
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    reads.clear()
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert feed["cost"]["reused"] == SECTIONS
    assert calls["activity"] == 1
    assert reads == []


def test_a_growing_transcript_recomputes_only_live_and_the_activity(idle, reads):
    runs, work, transcript, _, calls = idle
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    size = transcript.stat().st_size
    with transcript.open("a") as fh:
        fh.write(_line("m2", "2026-11-09T11:59:30Z", 10, 5) + "\n")
    reads.clear()
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert feed["cost"]["recomputed"] == ["live"]
    assert calls["activity"] == 2
    assert {offset for name, offset in reads if name == transcript.name} == {size}


def test_a_store_change_and_an_appended_line_give_the_uncached_feed(idle):
    runs, work, transcript, stamp, _ = idle
    _, cache = dash_feed.gather_feed_cached(runs, work, NOW, None, dash_feed.EMPTY_CACHE)
    stamp["value"] = (("runs", 6),)
    with transcript.open("a") as fh:
        fh.write(_line("m2", "2026-11-09T11:59:30Z", 1_000, 500) + "\n")
    feed, _ = dash_feed.gather_feed_cached(runs, work, LATER, None, cache)
    assert _without_cost(feed) == json.dumps(dash_feed.gather_feed(runs, work, LATER), sort_keys=True)
    assert feed["operator_sessions"]["five_hour"]
