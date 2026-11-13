import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from test_chair_facts import _deps

from agent_tools.chair_facts import gather_facts
from agent_tools.chair_read_remote_unfetched import (
    is_remote_unfetched,
    read_remote_unfetched,
    remote_unfetched_decompose_facts,
)
from agent_tools.chair_types import RemoteUnfetched
from agent_tools.remote_lane import remote_record

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

RUNS_COLUMNS = "run_id TEXT PRIMARY KEY, launched_at TEXT"


def _runs_table(runs_dir, *run_ids):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute(f"CREATE TABLE runs ({RUNS_COLUMNS})")
    for run_id in run_ids:
        conn.execute("INSERT INTO runs (run_id, launched_at) VALUES (?, ?)", (run_id, "2026-09-25T00:00:00Z"))
    conn.commit()
    conn.close()


def test_remote_record_with_no_local_run_dir_or_log_is_present_with_its_run_id(tmp_path):
    _runs_table(tmp_path, "i-1")
    (tmp_path / "i-1.remote.json").write_text("{}", encoding="utf-8")
    assert read_remote_unfetched(tmp_path, ["i"]) == {"i": "i-1"}


def test_remote_record_with_a_local_run_dir_is_absent(tmp_path):
    _runs_table(tmp_path, "i-1")
    (tmp_path / "i-1.remote.json").write_text("{}", encoding="utf-8")
    (tmp_path / "i-1").mkdir()
    assert read_remote_unfetched(tmp_path, ["i"]) == {}


def test_remote_record_with_a_local_log_and_no_run_dir_is_absent(tmp_path):
    _runs_table(tmp_path, "i-1")
    (tmp_path / "i-1.remote.json").write_text("{}", encoding="utf-8")
    (tmp_path / "i-1.log").write_text("", encoding="utf-8")
    assert read_remote_unfetched(tmp_path, ["i"]) == {}


def test_no_remote_record_is_absent(tmp_path):
    _runs_table(tmp_path, "i-1")
    assert read_remote_unfetched(tmp_path, ["i"]) == {}


def test_an_older_remote_unfetched_run_is_reported_despite_a_newer_local_run(tmp_path):
    _runs_table(tmp_path, "i-2", "i-3")
    (tmp_path / "i-2.remote.json").write_text("{}", encoding="utf-8")
    (tmp_path / "i-3").mkdir()
    assert read_remote_unfetched(tmp_path, ["i"]) == {"i": "i-2"}


def test_fetched_marker_makes_is_remote_unfetched_false():
    assert is_remote_unfetched(True, False, False, True) is False


def test_no_fetched_marker_makes_is_remote_unfetched_true():
    assert is_remote_unfetched(True, False, False, False) is True


def _remote_record_file(runs_dir, run, graph="epic"):
    record = remote_record("host-a", "2026-09-25T00:00:00Z", graph=graph)
    (runs_dir / f"{run}.remote.json").write_text(json.dumps(record), encoding="utf-8")


def test_decompose_facts_lists_the_initiative_whose_newest_run_is_a_decompose():
    assert remote_unfetched_decompose_facts({"b": "b-1", "a": "a-2"}, {"b-1": True, "a-2": True}) == ["a", "b"]


def test_decompose_facts_leaves_out_a_build_run_and_a_run_with_no_flag():
    assert remote_unfetched_decompose_facts({"a": "a-2", "b": "b-1"}, {"a-2": False}) == []


def test_decompose_facts_judges_only_the_run_the_map_names():
    assert remote_unfetched_decompose_facts({"a": "a-3"}, {"a-2": True, "a-3": False}) == []


def test_a_decompose_only_initiative_is_in_remote_unfetched_and_the_decompose_list(tmp_path):
    _runs_table(tmp_path, "i-1")
    _remote_record_file(tmp_path, "i-1", graph="decompose")
    result = read_remote_unfetched(tmp_path, ["i"])
    assert result == {"i": "i-1"}
    assert result.decompose == ["i"]


def test_a_build_run_initiative_is_in_remote_unfetched_only(tmp_path):
    _runs_table(tmp_path, "i-1")
    _remote_record_file(tmp_path, "i-1")
    result = read_remote_unfetched(tmp_path, ["i"])
    assert result == {"i": "i-1"}
    assert result.decompose == []


def test_a_newer_build_run_hides_an_older_decompose_run(tmp_path):
    _runs_table(tmp_path, "i-1", "i-2")
    _remote_record_file(tmp_path, "i-1", graph="decompose")
    _remote_record_file(tmp_path, "i-2")
    result = read_remote_unfetched(tmp_path, ["i"])
    assert result == {"i": "i-2"}
    assert result.decompose == []


def test_gather_facts_carries_the_decompose_list_from_a_remote_unfetched_source():
    unfetched = RemoteUnfetched({"i": "i-1", "j": "j-1"}, ["i"])
    facts = gather_facts(replace(_deps(), remote_unfetched=lambda: unfetched), NOW)
    assert facts["remote_unfetched"] == {"i": "i-1", "j": "j-1"}
    assert facts["remote_unfetched_decompose"] == ["i"]


def test_gather_facts_decompose_list_is_empty_for_a_plain_mapping():
    facts = gather_facts(replace(_deps(), remote_unfetched=lambda: {"i": "i-1"}), NOW)
    assert facts["remote_unfetched"] == {"i": "i-1"}
    assert facts["remote_unfetched_decompose"] == []
