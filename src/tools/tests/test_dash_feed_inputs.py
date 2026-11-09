import os
from pathlib import Path

import pytest

from agent_tools import dash_feed_inputs as inputs


@pytest.fixture
def tree(tmp_path):
    runs, projects = tmp_path / "runs", tmp_path / "projects"
    (runs / "r1-trace").mkdir(parents=True)
    (projects / "proj").mkdir(parents=True)
    files = {
        "db": runs / "cox.db",
        "wal": runs / "cox.db-wal",
        "record": runs / "r1:build.json",
        "lane": runs / "r1-trace" / "build-1.jsonl",
        "transcript": projects / "proj" / "s1.jsonl",
    }
    for path in files.values():
        path.write_text("x")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    return runs, projects, files


def _moved(runs, projects, action):
    before = inputs.probe(runs, projects)
    action()
    return inputs.changed(before, inputs.probe(runs, projects))


def test_constants():
    assert inputs.GROUPS == ("store", "records", "transcripts", "lane_traces")
    assert inputs.UNREADABLE != (0, 0)


def test_classify_one_call_per_rule(tmp_path):
    runs, projects = tmp_path / "runs", tmp_path / "projects"
    assert inputs.classify(runs / "cox.db", runs, projects) == "store"
    assert inputs.classify(runs / "cox.db-journal", runs, projects) == "store"
    assert inputs.classify(runs / "r1-trace" / "build-1.jsonl", runs, projects) == "lane_traces"
    assert inputs.classify(runs / "r1:build.json", runs, projects) == "records"
    assert inputs.classify(runs / "sub" / "cox.db", runs, projects) == "records"
    assert inputs.classify(projects / "p" / "s.jsonl", runs, projects) == "transcripts"
    assert inputs.classify(tmp_path / "other", runs, projects) is None


def test_identical_trees_are_equal(tree):
    runs, projects, _ = tree
    assert inputs.probe(runs, projects) == inputs.probe(runs, projects)
    assert _moved(runs, projects, lambda: None) == frozenset()


def test_touching_a_record_moves_only_records(tree):
    runs, projects, files = tree
    moved = _moved(runs, projects, lambda: os.utime(files["record"], ns=(2_000_000_000, 2_000_000_000)))
    assert moved == {"records"}


def test_appending_to_a_transcript_moves_only_transcripts(tree):
    runs, projects, files = tree
    moved = _moved(runs, projects, lambda: files["transcript"].write_text("xy"))
    assert moved == {"transcripts"}


def test_store_siblings_move_only_store(tree):
    runs, projects, files = tree
    assert _moved(runs, projects, lambda: files["wal"].write_text("xyz")) == {"store"}
    assert _moved(runs, projects, lambda: files["db"].write_text("xyz")) == {"store"}


def test_lane_trace_moves_only_lane_traces(tree):
    runs, projects, files = tree
    assert _moved(runs, projects, lambda: files["lane"].write_text("xyz")) == {"lane_traces"}


def test_a_missing_file_moves_its_group(tree):
    runs, projects, files = tree
    assert _moved(runs, projects, files["record"].unlink) == {"records"}
    assert _moved(runs, projects, files["transcript"].unlink) == {"transcripts"}


def test_an_unreadable_path_is_a_marker_not_an_exception(tree):
    runs, projects, _ = tree
    before = inputs.probe(runs, projects)
    (runs / "dangling.json").symlink_to(runs / "nowhere")
    after = inputs.probe(runs, projects)
    assert inputs.UNREADABLE in {reading for _, reading in after["records"]}
    assert inputs.changed(before, after) == {"records"}


def test_missing_directories_give_empty_groups(tmp_path):
    result = inputs.probe(tmp_path / "no-runs", tmp_path / "no-projects")
    assert result == dict.fromkeys(inputs.GROUPS, ())


def test_a_non_local_store_is_always_changed(tree):
    runs, projects, _ = tree
    remote = inputs.probe(runs, projects, store_is_local_sqlite=False)
    assert remote["store"] is None
    assert inputs.changed(remote, remote) == {"store"}


def test_fingerprint_from_literals():
    runs, projects = Path("/r"), Path("/p")
    readings = {"/r/cox.db": (1, 2), "/r/a.json": (3, 4), "/p/x/t.jsonl": (5, 6)}
    assert inputs.fingerprint(readings, runs, projects) == {
        "store": (("/r/cox.db", (1, 2)),),
        "records": (("/r/a.json", (3, 4)),),
        "transcripts": (("/p/x/t.jsonl", (5, 6)),),
        "lane_traces": (),
    }
