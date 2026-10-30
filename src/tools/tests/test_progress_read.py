import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import progress_read

FIXTURE = Path(__file__).parent / "fixtures" / "node_progress_v1.json"
TEXT = FIXTURE.read_text()


def test_the_v1_fixture_parses_into_every_field_of_both_nodes():
    progress = progress_read.parse(TEXT)
    assert (progress.version, progress.run, progress.written_at) == (1, "live-run-view-1", "2026-10-30T14:05:30Z")
    plan, build = progress.nodes
    assert plan == progress_read.NodeProgress(
        node="plan", attempt=1, task="", tier="premium", status="done", turns=9, cost_usd=0.42,
        started_at="2026-10-30T14:00:00Z", last_tool="Read", last_tool_at="2026-10-30T14:02:10Z",
        worktree="/tmp/worktrees/live-run-view-1/plan", branch=None,
    )
    assert build == progress_read.NodeProgress(
        node="build", attempt=1, task="p1-foundations-task", tier="standard", status="running", turns=14,
        cost_usd=None, started_at="2026-10-30T14:03:00Z", last_tool="Edit", last_tool_at="2026-10-30T14:05:12Z",
        worktree="/tmp/worktrees/live-run-view-1/build", branch="live-run-view-1/p1-foundations-task",
    )


def test_the_parsed_record_is_frozen():
    with pytest.raises(FrozenInstanceError):
        progress_read.parse(TEXT).run = "other"


@pytest.mark.parametrize("text", ["", "   ", TEXT[: len(TEXT) // 2], "[1, 2]", "null"])
def test_empty_torn_or_non_object_text_is_none(text):
    assert progress_read.parse(text) is None


def test_a_version_2_file_is_none():
    assert progress_read.parse(TEXT.replace('"version": 1', '"version": 2')) is None


def test_a_node_missing_a_key_is_skipped_and_the_other_kept():
    raw = json.loads(TEXT)
    del raw["nodes"][0]["turns"]
    progress = progress_read.parse(json.dumps(raw))
    assert [node.node for node in progress.nodes] == ["build"]


def test_age_is_seconds_since_written_at():
    progress = progress_read.parse(TEXT)
    assert progress_read.age_s(progress, datetime(2026, 10, 30, 14, 6, 0, tzinfo=UTC)) == 30.0


def test_an_unparseable_written_at_has_no_age():
    progress = progress_read.parse(TEXT.replace("2026-10-30T14:05:30Z", "yesterday"))
    assert progress_read.age_s(progress, datetime(2026, 10, 30, 14, 6, 0, tzinfo=UTC)) is None


def test_local_reads_the_run_file_under_the_runs_dir(tmp_path):
    (tmp_path / "host-a-1.progress.json").write_text(TEXT)
    assert progress_read.local(tmp_path, "host-a-1") == progress_read.parse(TEXT)


def test_a_missing_file_is_none(tmp_path):
    assert progress_read.local(tmp_path, "host-b-1") is None


def test_an_unreadable_file_is_none(tmp_path):
    (tmp_path / "host-a-2.progress.json").write_bytes(b"\xff\xfe\x00")
    assert progress_read.local(tmp_path, "host-a-2") is None
