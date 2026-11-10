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
    remote_unfetched_facts,
)
from agent_tools.chair_types import RemoteUnfetched

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
UNFETCHED = (True, False, False, False)

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


def test_a_decompose_only_initiative_is_in_both_the_unfetched_map_and_the_decompose_list():
    unfetched = remote_unfetched_facts({"a": ["a-1"]}, {"a-1": UNFETCHED})
    assert unfetched == {"a": "a-1"}
    assert remote_unfetched_decompose_facts(unfetched, {"a-1": True}) == ["a"]


def test_an_initiative_whose_newest_unfetched_run_is_a_build_is_not_in_the_decompose_list():
    unfetched = remote_unfetched_facts({"a": ["a-1", "a-2"]}, {"a-1": UNFETCHED, "a-2": UNFETCHED})
    assert unfetched == {"a": "a-2"}
    assert remote_unfetched_decompose_facts(unfetched, {"a-1": True, "a-2": False}) == []


def test_the_decompose_list_is_sorted_and_a_run_with_no_flag_is_a_build():
    unfetched = {"b": "b-1", "a": "a-1", "c": "c-1"}
    assert remote_unfetched_decompose_facts(unfetched, {"b-1": True, "a-1": True}) == ["a", "b"]


def _remote(tmp_path, run, record):
    (tmp_path / f"{run}.remote.json").write_text(record, encoding="utf-8")


def test_read_flags_the_decompose_run_and_leaves_the_build_and_unparseable_runs_out(tmp_path):
    _runs_table(tmp_path, "d-1", "b-1", "u-1", "o-1", "o-2")
    _remote(tmp_path, "d-1", json.dumps({"host": "h", "launched_at": "t", "graph": "decompose"}))
    _remote(tmp_path, "b-1", json.dumps({"host": "h", "launched_at": "t"}))
    _remote(tmp_path, "u-1", "not json")
    _remote(tmp_path, "o-1", json.dumps({"host": "h", "launched_at": "t", "graph": "decompose"}))
    _remote(tmp_path, "o-2", json.dumps({"host": "h", "launched_at": "t"}))
    result = read_remote_unfetched(tmp_path, ["d", "b", "u", "o"])
    assert result == {"d": "d-1", "b": "b-1", "u": "u-1", "o": "o-2"}
    assert result.decompose == ["d"]


def _gathered(unfetched):
    return gather_facts(replace(_deps(), remote_unfetched=lambda: unfetched), NOW)


def test_gather_facts_carries_the_decompose_flag_from_a_remote_unfetched_result():
    facts = _gathered(RemoteUnfetched({"init-a": "init-a-3"}, decompose=["init-a"]))
    assert facts["remote_unfetched_decompose"] == ["init-a"]
    assert facts["remote_unfetched"] == {"init-a": "init-a-3"}


def test_gather_facts_has_no_decompose_initiatives_for_a_plain_mapping_or_no_source():
    assert _gathered({"init-a": "init-a-3"})["remote_unfetched_decompose"] == []
    facts = gather_facts(replace(_deps(), remote_unfetched=None), NOW)
    assert (facts["remote_unfetched"], facts["remote_unfetched_decompose"]) == ({}, [])
