import json
from pathlib import Path

import pytest

import agent_tools.chair_activity_read as car
from agent_tools.chair_activity_read import (
    Entry,
    extend_lines,
    plan_read,
    project_folder_name,
    read_agents,
    read_agents_cached,
    read_project_sessions,
    read_project_sessions_cached,
    read_session_lines,
    read_session_lines_cached,
)


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    projects = tmp_path / "projects"
    workspace = tmp_path / "ws"
    folder = projects / project_folder_name(workspace)
    subagents = folder / "sess" / "subagents"
    subagents.mkdir(parents=True)
    (folder / "sess.jsonl").write_text("s1\ns2\n")
    (folder / "other.jsonl").write_text("o1\n")
    (subagents / "agent-b.meta.json").write_text('{"kind": "x"}')
    (subagents / "agent-b.jsonl").write_text("b1\nb2\n")
    (subagents / "agent-a.meta.json").write_text('{"kind": "y"}')
    (subagents / "agent-c.meta.json").write_text("not json")
    return projects, workspace, folder


@pytest.fixture
def spy(monkeypatch):
    calls = {"loads": 0, "reads": []}
    real_loads, real_read = json.loads, car._read_from

    def loads(*args, **kwargs):
        calls["loads"] += 1
        return real_loads(*args, **kwargs)

    def read_from(path, offset):
        calls["reads"].append((path.name, offset))
        return real_read(path, offset)

    monkeypatch.setattr(car.json, "loads", loads)
    monkeypatch.setattr(car, "_read_from", read_from)
    return calls


def _reset(calls):
    calls["loads"] = 0
    calls["reads"].clear()


def test_plan_read_one_literal_per_outcome():
    prev = Entry(10, 5, 10, ("a",), 1)
    assert plan_read(None, 3, 1) == "fresh"
    assert plan_read(prev, None, None) == "drop"
    assert plan_read(prev, 10, 5) == "keep"
    assert plan_read(prev, 10, 6) == "fresh"
    assert plan_read(prev, 12, 6) == "append"
    assert plan_read(prev, 4, 6) == "fresh"


def test_extend_lines_leaves_a_half_written_line_unconsumed():
    assert extend_lines(("a",), b"b\nc") == (("a", "b", "c"), 2, 2)
    assert extend_lines((), b"") == ((), 0, 0)
    assert extend_lines((), b"\xff\n") is None


def test_first_call_equals_uncached(tmp_path, spy):
    projects, workspace, _ = _tree(tmp_path)
    _, sessions = read_project_sessions_cached(projects, workspace, {})
    assert sessions == read_project_sessions(projects, workspace)


def test_idle_second_call_parses_and_opens_nothing(tmp_path, spy):
    projects, workspace, _ = _tree(tmp_path)
    cache, first = read_project_sessions_cached(projects, workspace, {})
    assert spy["loads"] == 3
    _reset(spy)
    again, second = read_project_sessions_cached(projects, workspace, cache)
    assert (spy["loads"], spy["reads"]) == (0, [])
    assert second == first
    assert again == cache


def test_append_reads_only_the_appended_lines(tmp_path, spy):
    projects, workspace, folder = _tree(tmp_path)
    cache, _ = read_project_sessions_cached(projects, workspace, {})
    _reset(spy)
    with (folder / "sess.jsonl").open("a") as fh:
        fh.write("s3\ns4\n")
    _, sessions = read_project_sessions_cached(projects, workspace, cache)
    assert spy["reads"] == [("sess.jsonl", 6)]
    assert spy["loads"] == 0
    assert sessions == read_project_sessions(projects, workspace)
    assert [s["lines"] for s in sessions if s["session_id"] == "sess"] == [["s1", "s2", "s3", "s4"]]


def test_appended_agent_transcript_is_read_from_its_offset(tmp_path, spy):
    projects, workspace, folder = _tree(tmp_path)
    cache, _ = read_project_sessions_cached(projects, workspace, {})
    _reset(spy)
    with (folder / "sess" / "subagents" / "agent-b.jsonl").open("a") as fh:
        fh.write("b3\n")
    _, sessions = read_project_sessions_cached(projects, workspace, cache)
    assert spy["reads"] == [("agent-b.jsonl", 6)]
    assert spy["loads"] == 0
    assert sessions == read_project_sessions(projects, workspace)


def test_truncated_file_is_read_afresh(tmp_path, spy):
    projects, workspace, folder = _tree(tmp_path)
    cache, _ = read_project_sessions_cached(projects, workspace, {})
    _reset(spy)
    (folder / "sess.jsonl").write_text("t1\n")
    _, sessions = read_project_sessions_cached(projects, workspace, cache)
    assert spy["reads"] == [("sess.jsonl", 0)]
    assert sessions == read_project_sessions(projects, workspace)
    assert [s["lines"] for s in sessions if s["session_id"] == "sess"] == [["t1"]]


def test_vanished_file_is_dropped_from_cache_and_sessions(tmp_path, spy):
    projects, workspace, folder = _tree(tmp_path)
    cache, _ = read_project_sessions_cached(projects, workspace, {})
    (folder / "other.jsonl").unlink()
    again, sessions = read_project_sessions_cached(projects, workspace, cache)
    assert folder / "other.jsonl" not in again
    assert [s["session_id"] for s in sessions] == ["sess"]
    assert sessions == read_project_sessions(projects, workspace)


def test_unterminated_last_line_is_shown_then_completed_not_duplicated(tmp_path, spy):
    projects, workspace, folder = _tree(tmp_path)
    (folder / "new.jsonl").write_text("a\nb")
    cache, sessions = read_project_sessions_cached(projects, workspace, {})
    assert sessions == read_project_sessions(projects, workspace)
    _reset(spy)
    with (folder / "new.jsonl").open("a") as fh:
        fh.write("c\nd\n")
    _, sessions = read_project_sessions_cached(projects, workspace, cache)
    assert spy["reads"] == [("new.jsonl", 2)]
    assert [s["lines"] for s in sessions if s["session_id"] == "new"] == [["a", "bc", "d"]]
    assert sessions == read_project_sessions(projects, workspace)


def test_line_separators_split_like_the_uncached_reader(tmp_path, spy):
    projects, workspace, folder = _tree(tmp_path)
    (folder / "odd.jsonl").write_bytes(b"x\xe2\x80\xa8y\r\nz\r")
    cache, sessions = read_project_sessions_cached(projects, workspace, {})
    assert sessions == read_project_sessions(projects, workspace)
    with (folder / "odd.jsonl").open("ab") as fh:
        fh.write(b"\nw\n")
    _, sessions = read_project_sessions_cached(projects, workspace, cache)
    assert sessions == read_project_sessions(projects, workspace)


def test_changed_meta_is_parsed_once_and_unchanged_ones_are_not(tmp_path, spy):
    projects, workspace, folder = _tree(tmp_path)
    cache, _ = read_project_sessions_cached(projects, workspace, {})
    _reset(spy)
    (folder / "sess" / "subagents" / "agent-a.meta.json").write_text('{"kind": "zz", "n": 1}')
    _, sessions = read_project_sessions_cached(projects, workspace, cache)
    assert spy["loads"] == 1
    assert sessions == read_project_sessions(projects, workspace)


def test_session_lines_and_agents_match_uncached_and_idle_reads_nothing(tmp_path, spy):
    projects, _, _ = _tree(tmp_path)
    lines_cache, lines = read_session_lines_cached(projects, "sess", {})
    agents_cache, agents = read_agents_cached(projects, "sess", {})
    assert lines == read_session_lines(projects, "sess")
    assert agents == read_agents(projects, "sess")
    _reset(spy)
    assert read_session_lines_cached(projects, "sess", lines_cache) == (lines_cache, lines)
    assert read_agents_cached(projects, "sess", agents_cache) == (agents_cache, agents)
    assert (spy["loads"], spy["reads"]) == (0, [])
