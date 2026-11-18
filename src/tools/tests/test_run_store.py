import dataclasses
import json
import os
import sqlite3
import sys
import types

import pytest

from agent_tools import epic, run_store

COLUMNS = (
    "call_id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, phase_id TEXT, task_id TEXT, role TEXT, tier TEXT, "
    "model_alias TEXT, model_id TEXT, claude_code_version TEXT, cost_usd REAL, ceiling_usd REAL, "
    "ceiling_source TEXT, turns INTEGER, duration_ms INTEGER, input_tokens INTEGER, cache_read_tokens INTEGER, "
    "cache_creation_tokens INTEGER, input_total INTEGER, output_tokens INTEGER, ok BOOL, ts TEXT, "
    "requested_tier TEXT, chosen_tier TEXT, decision_reason TEXT, router_tier TEXT, router_reason TEXT, "
    "ticket_key TEXT, outcome_key TEXT, system_one_prediction TEXT, system_one_confidence REAL, "
    "decision_json TEXT, detail_json TEXT"
)

ROW = {
    "call_id": "c3a75b39", "run_id": "r1", "seq": 1, "task_id": None, "role": "scope_epic", "tier": "cheap",
    "model_alias": "haiku", "model_id": "claude-haiku-4-5-20251001", "cost_usd": 0.05987000000000001,
    "ceiling_usd": 0.15, "ceiling_source": "profile", "turns": 2, "duration_ms": 79013, "input_tokens": 10,
    "cache_read_tokens": 0, "cache_creation_tokens": 11595, "input_total": 11605, "output_tokens": 7334,
    "ok": 1, "ts": "2026-09-25T04:34:43.390253+00:00", "decision_json": None,
}


def store(runs_dir, *rows):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute(f"CREATE TABLE node_calls ({COLUMNS})")
    for row in rows:
        conn.execute(f"INSERT INTO node_calls ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    conn.close()


RUNS_COLUMNS = (
    "run_id TEXT PRIMARY KEY, principal TEXT, launched_by TEXT, launched_at TEXT, cartridge_sha TEXT, "
    "cartridge_team TEXT, overlay_sha TEXT, provider_profile TEXT, started_at TEXT, ended_at TEXT, "
    "status TEXT, record_json TEXT"
)

RUN = {
    "run_id": "storage-sqlite-run-records-14", "principal": "epic-swarm(lifecycle-propose)", "launched_by": "cli",
    "launched_at": "2026-09-25T04:30:00.123456+00:00",
}


def runs_table(runs_dir, *rows):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute(f"CREATE TABLE runs ({RUNS_COLUMNS})")
    for row in rows:
        conn.execute(f"INSERT INTO runs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    conn.close()


ENDED = "2026-09-25T05:03:46.812070+00:00"


def run_row(run_id, ended_at=ENDED):
    return {"run_id": run_id, "launched_at": "2026-09-25T04:59:39.238479+00:00", "ended_at": ended_at, "status": None if ended_at is None else "ok"}


def test_run_ids_lists_every_id_in_the_runs_table(tmp_path):
    runs_table(tmp_path, run_row("a-1"), run_row("a-3", ended_at=None))
    assert run_store.run_ids(tmp_path) == {"a-1", "a-3"}


def test_run_ids_is_empty_with_no_store_or_no_runs_table(tmp_path):
    assert run_store.run_ids(tmp_path) == set()
    store(tmp_path, ROW)
    assert run_store.run_ids(tmp_path) == set()


def test_cost_since_sums_calls_of_the_day_and_nothing_before_or_after_it(tmp_path):
    store(tmp_path, {**ROW, "call_id": "a", "cost_usd": 0.25}, {**ROW, "call_id": "b", "cost_usd": 0.5},
          {**ROW, "call_id": "c", "cost_usd": 9.0, "ts": "2026-09-24T23:59:59+00:00"},
          {**ROW, "call_id": "d", "cost_usd": 7.0, "ts": "2026-09-26T00:00:00+00:00"})
    assert run_store.cost_since(tmp_path, "2026-09-25", "2026-09-26") == 0.75


def test_cost_since_is_none_without_a_store(tmp_path):
    assert run_store.cost_since(tmp_path, "2026-09-25") is None


def test_cost_since_is_zero_for_a_store_with_no_calls_that_day(tmp_path):
    store(tmp_path, {**ROW, "ts": "2026-09-24T10:00:00+00:00"})
    assert run_store.cost_since(tmp_path, "2026-09-25") == 0.0


def test_cost_since_is_none_for_a_store_without_a_node_calls_table(tmp_path):
    runs_table(tmp_path, run_row("a-1"))
    assert run_store.cost_since(tmp_path, "2026-09-25") is None


def chair_actions_table(runs_dir, *rows):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE chair_actions (kind TEXT, ts TEXT, action_json TEXT)")
    for row in rows:
        conn.execute(f"INSERT INTO chair_actions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    conn.close()


METER_DOC = {
    "five_hour": {"used_percentage": 84.0, "resets_at": 1780000000},
    "seven_day": {"used_percentage": 10.0, "resets_at": 1780500000},
    "observed_at": "2026-09-29T00:00:00+00:00",
}


def test_latest_chair_meter_returns_the_three_keys_of_its_one_row(tmp_path):
    chair_actions_table(tmp_path, {"kind": "meter", "ts": "2026-09-29T00:00:00+00:00", "action_json": json.dumps(METER_DOC)})
    assert run_store.latest_chair_meter(tmp_path) == METER_DOC


def test_latest_chair_meter_returns_the_row_with_the_newer_ts(tmp_path):
    older = {**METER_DOC, "five_hour": {"used_percentage": 1.0, "resets_at": 1780000000}}
    chair_actions_table(
        tmp_path,
        {"kind": "meter", "ts": "2026-09-29T00:00:00+00:00", "action_json": json.dumps(older)},
        {"kind": "meter", "ts": "2026-09-29T00:05:00+00:00", "action_json": json.dumps(METER_DOC)},
    )
    assert run_store.latest_chair_meter(tmp_path) == METER_DOC


def test_latest_chair_meter_ignores_a_newer_row_of_another_kind(tmp_path):
    chair_actions_table(
        tmp_path,
        {"kind": "meter", "ts": "2026-09-29T00:00:00+00:00", "action_json": json.dumps(METER_DOC)},
        {"kind": "housekeeping", "ts": "2026-09-29T00:05:00+00:00", "action_json": "{}"},
    )
    assert run_store.latest_chair_meter(tmp_path) == METER_DOC


def test_latest_chair_meter_is_none_with_no_meter_row(tmp_path):
    chair_actions_table(tmp_path, {"kind": "housekeeping", "ts": "2026-09-29T00:00:00+00:00", "action_json": "{}"})
    assert run_store.latest_chair_meter(tmp_path) is None


def test_latest_chair_meter_is_none_without_a_store(tmp_path):
    assert run_store.latest_chair_meter(tmp_path) is None


class _DictRowConn:
    """Postgres's shape: a `dict_row` with `action_json` already decoded, and no integer index."""

    def execute(self, sql, params):
        return types.SimpleNamespace(fetchone=lambda: {"action_json": METER_DOC})

    def close(self):
        pass


def test_latest_chair_meter_reads_a_postgres_dict_row_with_a_decoded_cell(monkeypatch, tmp_path):
    monkeypatch.setattr(run_store, "open_store", lambda runs_dir: (_DictRowConn(), "%s"))
    assert run_store.latest_chair_meter(tmp_path) == METER_DOC


def test_last_call_at_maps_each_run_to_its_own_newest_ts(tmp_path):
    store(
        tmp_path,
        {**ROW, "call_id": "a", "run_id": "r1", "ts": "2026-09-25T04:00:00+00:00"},
        {**ROW, "call_id": "b", "run_id": "r1", "ts": "2026-09-25T05:00:00+00:00"},
        {**ROW, "call_id": "c", "run_id": "r2", "ts": "2026-09-25T02:00:00+00:00"},
    )
    assert run_store.last_call_at(tmp_path, ["r1", "r2"]) == {
        "r1": "2026-09-25T05:00:00+00:00",
        "r2": "2026-09-25T02:00:00+00:00",
    }


def test_build_counts_counts_build_calls_per_task_and_leaves_out_tasks_with_none(tmp_path):
    store(tmp_path, {**ROW, "call_id": "a", "role": "build", "task_id": "t1"}, {**ROW, "call_id": "b", "role": "build", "task_id": "t2"},
          {**ROW, "call_id": "c", "role": "build", "task_id": "t2"}, {**ROW, "call_id": "d", "role": "review", "task_id": "t3"})
    assert run_store.build_counts(tmp_path, ["t1", "t2", "t3", "t4"]) == {"t1": 1, "t2": 2}


def test_build_counts_returns_the_composites_of_the_given_run_and_not_a_run_it_prefixes(tmp_path):
    store(tmp_path, {**ROW, "call_id": "a", "role": "build", "task_id": "epic-x-5:seams:seams-t"},
          {**ROW, "call_id": "b", "role": "build", "task_id": "epic-x-5:seams:seams-t"},
          {**ROW, "call_id": "c", "role": "build", "task_id": "epic-x-50:seams:other-t"})
    assert run_store.build_counts(tmp_path, [], ["epic-x-5"]) == {"epic-x-5:seams:seams-t": 2}


def test_build_counts_treats_underscore_and_percent_in_a_run_name_literally(tmp_path):
    store(tmp_path, {**ROW, "call_id": "a", "role": "build", "task_id": "epicAx-5:seams:wild-t"},
          {**ROW, "call_id": "b", "role": "build", "task_id": "epic-x-5:seams:seams-t"})
    assert run_store.build_counts(tmp_path, [], ["epic_x-5", "epic%5"]) == {}


def test_build_counts_is_empty_without_a_store(tmp_path):
    assert run_store.build_counts(tmp_path, ["t1"]) == {}


def test_build_counts_is_empty_for_a_store_without_a_node_calls_table(tmp_path):
    runs_table(tmp_path, run_row("a-1"))
    assert run_store.build_counts(tmp_path, ["t1"]) == {}


def test_build_counts_is_empty_for_no_ids_and_no_runs(tmp_path):
    store(tmp_path, {**ROW, "role": "build", "task_id": "t1"})
    assert run_store.build_counts(tmp_path, []) == {}


def test_the_file_is_returned_unchanged_even_when_the_store_has_rows(tmp_path):
    body = {"run_id": "r1", "calls": [], "summary": {"calls": 99}}
    (tmp_path / "r1.usage.json").write_text(json.dumps(body))
    store(tmp_path, ROW)
    assert run_store.usage(tmp_path, "r1") == body


def test_the_store_answers_when_the_file_is_absent_and_its_summary_matches(tmp_path):
    store(tmp_path, ROW, {**ROW, "call_id": "c2", "seq": 2, "model_alias": "opus", "cost_usd": 1.0})
    runs_table(tmp_path, run_row("r1"))
    got = run_store.usage(tmp_path, "r1")
    assert got["run_id"] == "r1"
    assert [c["model"] for c in got["calls"]] == ["haiku", "opus"]
    assert got["summary"] == run_store.summarize(got["calls"])
    assert got["summary"]["calls"] == 2


def test_the_store_answers_when_the_file_is_not_a_json_object(tmp_path):
    (tmp_path / "r1.usage.json").write_text("[1, 2]")
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1"))
    assert run_store.usage(tmp_path, "r1")["calls"][0]["id"] == "c3a75b39"


def test_calls_come_back_ordered_by_ts_then_seq(tmp_path):
    store(
        tmp_path,
        {**ROW, "call_id": "b", "seq": 2},
        {**ROW, "call_id": "a", "seq": 1},
        {**ROW, "call_id": "z", "seq": 9, "ts": "2026-09-25T00:00:00+00:00"},
    )
    runs_table(tmp_path, run_row("r1"))
    assert [c["id"] for c in run_store.usage(tmp_path, "r1")["calls"]] == ["z", "a", "b"]


def test_another_runs_rows_are_not_returned(tmp_path):
    store(tmp_path, {**ROW, "run_id": "other"})
    assert run_store.usage(tmp_path, "r1") is None


def test_neither_source_gives_none(tmp_path):
    assert run_store.usage(tmp_path, "r1") is None


def test_a_store_without_node_calls_gives_none(tmp_path):
    sqlite3.connect(tmp_path / "cox.db").close()
    assert run_store.usage(tmp_path, "r1") is None


def test_a_row_becomes_the_usage_file_call():
    assert run_store.call_from_row(ROW | {"decision_json": None}) == {
        "role": "scope_epic", "task_id": None, "tier": "cheap", "model": "haiku", "model_id": "claude-haiku-4-5-20251001", "cost_usd": 0.05987000000000001,
        "ceiling_usd": 0.15, "ceiling_source": "profile", "turns": 2, "duration_ms": 79013, "input_tokens": 10,
        "cache_read_tokens": 0, "cache_creation_tokens": 11595, "input_total": 11605, "output_tokens": 7334,
        "id": "c3a75b39", "ts": "2026-09-25T04:34:43.390253+00:00", "ok": True, "decision": None,
    }


def test_a_set_decision_json_parses():
    assert run_store.call_from_row(ROW | {"decision_json": '{"tier": "cheap"}'})["decision"] == {"tier": "cheap"}


def test_detail_json_carries_its_summary_and_commands_run_onto_the_call():
    detail = {"summary": {"result": "success"}, "commands_run": [{"command": "ls"}], "other": 1}
    got = run_store.call_from_row(ROW | {"detail_json": json.dumps(detail)})
    assert got["summary"] == {"result": "success"} and got["commands_run"] == [{"command": "ls"}]
    assert "other" not in got


@pytest.mark.parametrize("detail", [None, "[1, 2]", "not json", "{}"])
def test_a_row_with_no_usable_detail_json_is_unchanged(detail):
    assert run_store.call_from_row(ROW | {"detail_json": detail}) == run_store.call_from_row(ROW)
    assert "summary" not in run_store.call_from_row(ROW | {"detail_json": detail})


def test_a_stored_row_carries_its_detail_through_usage(tmp_path):
    store(tmp_path, ROW | {"detail_json": json.dumps({"summary": {"result": "success"}})})
    runs_table(tmp_path, run_row("r1"))
    assert run_store.usage(tmp_path, "r1")["calls"][0]["summary"] == {"result": "success"}


@pytest.mark.parametrize(("stored", "expected"), [(1, True), (0, False)])
def test_ok_becomes_a_bool(stored, expected):
    assert run_store.call_from_row(ROW | {"ok": stored})["ok"] is expected


def test_summarize_totals_and_groups_by_model():
    calls = [
        {"model": "haiku", "cost_usd": 0.5, "turns": 2, "input_total": 10, "input_tokens": 1, "output_tokens": 3},
        {"model": "opus", "cost_usd": 1.0, "turns": 1, "input_tokens": 7, "output_tokens": 4},
    ]
    got = run_store.summarize(calls)
    assert (got["calls"], got["cost_usd"], got["turns"], got["input_total"], got["output_tokens"]) == (2, 1.5, 3, 17, 7)
    assert got["by_model"]["opus"]["input_total"] == 7


def test_connect_readonly_returns_none_without_a_store_and_creates_nothing(tmp_path):
    assert run_store.connect_readonly(tmp_path) is None
    assert not (tmp_path / "cox.db").exists()


def test_connect_readonly_refuses_writes(tmp_path):
    store(tmp_path, ROW)
    conn = run_store.connect_readonly(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM node_calls")
    conn.close()


def test_usages_lists_file_runs_and_store_only_runs(tmp_path):
    body = {"run_id": "f1", "calls": [], "summary": {"calls": 0}}
    (tmp_path / "f1.usage.json").write_text(json.dumps(body))
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1"))
    got = run_store.usages(tmp_path)
    assert list(got) == ["f1", "r1"]
    assert got["f1"] == body
    assert got["r1"] == run_store.usage(tmp_path, "r1")


def test_usages_takes_a_run_in_both_from_its_file(tmp_path):
    body = {"run_id": "r1", "calls": [], "summary": {"calls": 99}}
    (tmp_path / "r1.usage.json").write_text(json.dumps(body))
    store(tmp_path, ROW)
    assert run_store.usages(tmp_path) == {"r1": body}


def test_usages_without_a_database_lists_the_files_only(tmp_path):
    (tmp_path / "f1.usage.json").write_text('{"run_id": "f1"}')
    (tmp_path / "bad.usage.json").write_text("[1]")
    assert run_store.usages(tmp_path) == {"f1": {"run_id": "f1"}}
    assert not (tmp_path / "cox.db").exists()


def test_usages_lets_the_store_answer_for_a_file_that_is_not_an_object(tmp_path):
    (tmp_path / "r1.usage.json").write_text("[1, 2]")
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1"))
    assert run_store.usages(tmp_path)["r1"]["calls"][0]["id"] == "c3a75b39"


def test_a_live_run_with_calls_and_no_ended_at_is_none_and_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None: True)
    store(tmp_path, ROW, {**ROW, "call_id": "c9", "run_id": "r2"})
    runs_table(tmp_path, run_row("r1", None), run_row("r2", None))
    (tmp_path / "r1.pid").write_text("4242")
    (tmp_path / "r2.pid").write_text("4243")
    assert run_store.usage(tmp_path, "r1") is None
    assert run_store.usages(tmp_path) == {}


def test_a_run_with_no_ended_at_and_no_pidfile_reads_as_ended(tmp_path):
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1", None))
    assert [c["id"] for c in run_store.usage(tmp_path, "r1")["calls"]] == ["c3a75b39"]


def test_a_pidfile_decides_by_run_live(tmp_path, monkeypatch):
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1", None))
    (tmp_path / "r1.pid").write_text("4242\n")
    seen = []
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None: seen.append((pid, pidfile)) or True)
    assert run_store.usage(tmp_path, "r1") is None
    assert seen == [(4242, tmp_path / "r1.pid")]
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None: False)
    assert [c["id"] for c in run_store.usage(tmp_path, "r1")["calls"]] == ["c3a75b39"]


def test_a_pidfile_that_is_not_an_int_reads_as_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None: True)
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1", None))
    (tmp_path / "r1.pid").write_text("not-a-pid")
    assert run_store.usage(tmp_path, "r1")["calls"][0]["id"] == "c3a75b39"


def test_usages_lists_the_dead_run_and_omits_the_live_one_checking_each_run_once(tmp_path, monkeypatch):
    store(tmp_path, ROW, {**ROW, "call_id": "c2", "seq": 2}, {**ROW, "call_id": "c9", "run_id": "r2"})
    runs_table(tmp_path, run_row("r1", None), run_row("r2", None))
    (tmp_path / "r1.pid").write_text("1")
    (tmp_path / "r2.pid").write_text("2")
    seen = []
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None: seen.append(pid) or pid == 2)
    got = run_store.usages(tmp_path)
    assert list(got) == ["r1"] and len(got["r1"]["calls"]) == 2
    assert sorted(seen) == [1, 2]


def test_a_run_with_calls_and_no_runs_row_is_none_and_left_out(tmp_path):
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("other"))
    assert run_store.usage(tmp_path, "r1") is None
    assert run_store.usages(tmp_path) == {}


def test_the_same_run_is_returned_once_ended_at_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(epic, "run_live", lambda pid, pidfile, log=None: True)
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1", None))
    (tmp_path / "r1.pid").write_text("4242")
    assert run_store.usage(tmp_path, "r1") is None
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("UPDATE runs SET ended_at = ?, status = 'ok' WHERE run_id = 'r1'", (ENDED,))
    conn.commit()
    conn.close()
    got = run_store.usage(tmp_path, "r1")
    assert [c["id"] for c in got["calls"]] == ["c3a75b39"]
    assert run_store.usages(tmp_path) == {"r1": got}


def test_a_usage_file_is_returned_whatever_the_store_says(tmp_path):
    body = {"run_id": "r1", "calls": [], "summary": {"calls": 99}}
    (tmp_path / "r1.usage.json").write_text(json.dumps(body))
    store(tmp_path, ROW)
    runs_table(tmp_path, run_row("r1", None))
    assert run_store.usage(tmp_path, "r1") == body
    assert run_store.usages(tmp_path) == {"r1": body}


def test_run_started_reads_launched_at(tmp_path):
    runs_table(tmp_path, RUN)
    assert run_store.run_started(tmp_path, "storage-sqlite-run-records-14") == "2026-09-25T04:30:00.123456+00:00"


def test_run_started_is_none_for_a_run_the_store_does_not_have(tmp_path):
    runs_table(tmp_path, RUN)
    assert run_store.run_started(tmp_path, "other") is None
    assert run_store.run_started(tmp_path / "missing", "other") is None


def day_file(runs_dir, day, run_id, *frames):
    """Write one zstd file, one frame per append, as graphs does: `traces/YYYY/MM/DD/<run_id>.jsonl.zst`."""
    import zstandard

    def frame(rows):
        text = "".join(json.dumps({"run_id": run_id, **r}, sort_keys=True, separators=(",", ":")) + "\n" for r in rows)
        return zstandard.ZstdCompressor().compress(text.encode())

    path = runs_dir / "traces" / day / f"{run_id}.jsonl.zst"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(frame(rows) for rows in frames))


def test_call_events_reads_the_loose_file_when_the_stores_hold_nothing_and_skips_a_line_that_is_not_an_object(tmp_path):
    loose = tmp_path / "r1-trace" / "scope_epic-1.jsonl"
    loose.parent.mkdir()
    loose.write_text('{"type":"a"}\nnot json\n[1]\n\n{"type":"b"}\n')
    (tmp_path / "traces").mkdir()
    assert run_store.call_events(tmp_path, "r1", {"id": "c1", "trace": str(loose)}) == [{"type": "a"}, {"type": "b"}]


def test_call_events_reads_the_store_in_seq_order_across_days_ignoring_other_calls(tmp_path):
    pytest.importorskip("zstandard")

    def row(call, seq, name):
        return {"call_id": call, "seq": seq, "event": {"n": name}}

    day_file(tmp_path, "2026/09/24", "r1", [row("c1", 1, "b"), row("c2", 0, "x")], [row("c1", 0, "a")])
    day_file(tmp_path, "2026/09/25", "r1", [row("c1", 3, "d"), row("c1", 2, "c")])
    day_file(tmp_path, "2026/09/25", "r2", [row("c1", 0, "other run")])
    missing = {"id": "c1", "trace": str(tmp_path / "gone.jsonl")}
    assert run_store.call_events(tmp_path, "r1", missing) == [{"n": "a"}, {"n": "b"}, {"n": "c"}, {"n": "d"}]
    assert run_store.call_events(tmp_path, "r1", {"id": "c2"}) == [{"n": "x"}]


def test_call_events_store_glue_runs_without_zstandard_against_a_stand_in_decompressor(tmp_path, monkeypatch):
    """Runs where zstandard is absent: the day files hold plain lines and the stand-in passes bytes through."""
    stand_in = types.SimpleNamespace(
        ZstdDecompressor=lambda: types.SimpleNamespace(stream_reader=lambda fh, read_across_frames: fh)
    )
    monkeypatch.setitem(sys.modules, "zstandard", stand_in)
    for day, seqs in (("2026/09/24", (1, 0)), ("2026/09/25", (2,))):
        path = tmp_path / "traces" / day / "r1.jsonl.zst"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"run_id": "r1", "call_id": "c1", "seq": s, "event": {"n": s}} for s in seqs]
        rows.append({"run_id": "r1", "call_id": "c2", "seq": 0, "event": {"n": "other"}})
        path.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows))
    assert run_store.call_events(tmp_path, "r1", {"id": "c1"}) == [{"n": 0}, {"n": 1}, {"n": 2}]


def test_call_events_is_none_with_no_loose_file_and_no_trace_store(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", None)
    assert run_store.call_events(tmp_path, "r1", {"id": "c1", "trace": str(tmp_path / "gone.jsonl")}) is None
    assert run_store.call_events(tmp_path, "r1", {"id": "c1"}) is None


def test_call_events_without_zstandard_raises_naming_the_extra(tmp_path, monkeypatch):
    (tmp_path / "traces").mkdir()
    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(run_store.TracesUnavailable, match=r"coxswain\[traces\]"):
        run_store.call_events(tmp_path, "r1", {"id": "c1"})


PHASES_COLUMNS = (
    "run_id TEXT, phase_id TEXT, ts TEXT, principal TEXT, human_minutes REAL, totals_json TEXT, record_json TEXT, "
    "PRIMARY KEY (run_id, phase_id)"
)


def manifest(run_id, phase, ts="2026-09-25T04:40:00+00:00"):
    return {
        "cartridge_sha": "abc123", "cartridge_team": "example-team", "gate_diffs": [], "human_minutes": 1.5, "overlay_sha": None,
        "principal": "epic-swarm", "proposals": [], "provider_profile": "default", "run_id": f"{run_id}:{phase}",
        "totals": {"cost_usd": 0.1}, "ts": ts,
    }


def phase_row(run_id, phase, ts, record=None):
    record = {"phase": phase, "status": "ok", "ts": ts} if record is None else record
    return {"run_id": run_id, "phase_id": phase, "ts": ts, "record_json": json.dumps(record)}


def with_record(run_id, phase, ts):
    record = {"phase": phase, "status": "ok", "ts": ts, "manifest": f"{run_id}:{phase}", "manifest_record": manifest(run_id, phase, ts)}
    return phase_row(run_id, phase, ts, record)


def phases_table(runs_dir, *rows):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute(f"CREATE TABLE phases ({PHASES_COLUMNS})")
    for row in rows:
        conn.execute(f"INSERT INTO phases ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    conn.close()


def test_manifest_files_win_over_the_store_and_come_back_sorted_by_file_name(tmp_path):
    for phase in ("scope", "build", "plan"):
        (tmp_path / f"r1:{phase}.json").write_text(json.dumps(manifest("r1", phase)))
    (tmp_path / "r1:broken.json").write_text("[1, 2]")
    (tmp_path / "r1.usage.json").write_text(json.dumps({"run_id": "r1"}))
    runs_table(tmp_path, run_row("r1"))
    phases_table(tmp_path, with_record("r1", "store", "2026-09-25T04:41:00+00:00"))
    got = run_store.phase_manifests(tmp_path, "r1")
    assert [m["run_id"] for m in got] == ["r1:build", "r1:plan", "r1:scope"]


def test_the_store_answers_when_no_file_exists_ordered_by_row_ts(tmp_path):
    runs_table(tmp_path, run_row("r1"))
    phases_table(
        tmp_path,
        with_record("r1", "a-late", "2026-09-25T04:50:00+00:00"),
        with_record("r1", "z-early", "2026-09-25T04:40:00+00:00"),
    )
    got = run_store.phase_manifests(tmp_path, "r1")
    assert [m["run_id"] for m in got] == ["r1:z-early", "r1:a-late"]


def test_the_store_answers_for_a_run_that_has_not_ended_and_for_one_with_no_runs_row(tmp_path):
    runs_table(tmp_path, run_row("r1", ended_at=None))
    phases_table(tmp_path, with_record("r1", "plan", "2026-09-25T04:40:00+00:00"), with_record("r2", "plan", "2026-09-25T04:40:00+00:00"))
    assert run_store.phase_manifests(tmp_path, "r1") == [manifest("r1", "plan")]
    assert [m["run_id"] for m in run_store.phase_manifests(tmp_path, "r2")] == ["r2:plan"]


def test_phase_names_come_from_files_oldest_mtime_first(tmp_path):
    for phase, mtime in (("a-new", 2000), ("z-old", 1000)):
        path = tmp_path / f"r1:{phase}.json"
        path.write_text("{}")
        os.utime(path, (mtime, mtime))
    (tmp_path / "r2:other.json").write_text("{}")
    assert run_store.phase_names(tmp_path, "r1") == ["z-old", "a-new"]


def test_phase_names_come_from_store_rows_by_ts_for_a_run_with_no_ended_at(tmp_path):
    runs_table(tmp_path, run_row("r1", ended_at=None))
    phases_table(
        tmp_path,
        phase_row("r1", "a-late", "2026-09-25T04:50:00+00:00"),
        phase_row("r1", "z-early", "2026-09-25T04:40:00+00:00"),
        phase_row("r2", "other", "2026-09-25T04:30:00+00:00"),
    )
    assert run_store.phase_names(tmp_path, "r1") == ["z-early", "a-late"]


def test_phase_names_files_win_over_the_store(tmp_path):
    (tmp_path / "r1:file-phase.json").write_text("{}")
    phases_table(tmp_path, phase_row("r1", "store-phase", "2026-09-25T04:40:00+00:00"))
    assert run_store.phase_names(tmp_path, "r1") == ["file-phase"]


def test_phase_names_with_neither_files_nor_store_is_empty(tmp_path):
    assert run_store.phase_names(tmp_path, "r1") == []


def test_a_phase_row_without_manifest_record_is_skipped(tmp_path):
    runs_table(tmp_path, run_row("r1"))
    phases_table(
        tmp_path,
        phase_row("r1", "plan", "2026-09-25T04:40:00+00:00"),
        with_record("r1", "build", "2026-09-25T04:41:00+00:00"),
    )
    assert [m["run_id"] for m in run_store.phase_manifests(tmp_path, "r1")] == ["r1:build"]


def test_no_files_and_no_store_return_empty(tmp_path):
    assert run_store.phase_manifests(tmp_path, "r1") == []
    assert run_store.all_phase_manifests(tmp_path) == {}


def test_all_phase_manifests_lists_file_runs_and_store_only_runs_once_each(tmp_path):
    (tmp_path / "r1:plan.json").write_text(json.dumps(manifest("r1", "plan")))
    runs_table(tmp_path, run_row("r1"), run_row("r2"), run_row("r3", ended_at=None))
    phases_table(
        tmp_path,
        with_record("r1", "store-only-phase", "2026-09-25T04:40:00+00:00"),
        with_record("r2", "build", "2026-09-25T04:41:00+00:00"),
        with_record("r3", "build", "2026-09-25T04:42:00+00:00"),
    )
    got = run_store.all_phase_manifests(tmp_path)
    assert sorted(got) == ["r1", "r2", "r3"]
    assert [m["run_id"] for m in got["r1"]] == ["r1:plan"]
    assert [m["run_id"] for m in got["r2"]] == ["r2:build"]
    assert [m["run_id"] for m in got["r3"]] == ["r3:build"]


def _three_runs(tmp_path):
    store(tmp_path, ROW, {**ROW, "call_id": "c2", "run_id": "r2"}, {**ROW, "call_id": "c3", "run_id": "r3"})
    runs_table(
        tmp_path,
        run_row("r1", "2026-09-01T00:00:00+00:00"),
        run_row("r2", "2026-09-20T00:00:00+00:00"),
        run_row("r3", None),
    )


def test_store_usages_never_checks_an_excluded_run(tmp_path, monkeypatch):
    _three_runs(tmp_path)
    seen = []
    real = run_store._run_ended
    monkeypatch.setattr(run_store, "_run_ended", lambda d, rid, ended: seen.append(rid) or real(d, rid, ended))
    got = run_store.store_usages(tmp_path, exclude={"r1", "r3"})
    assert list(got) == ["r2"]
    assert seen == ["r2"]


def test_store_usages_since_drops_a_run_that_ended_before_it_and_keeps_the_rest(tmp_path):
    _three_runs(tmp_path)
    assert list(run_store.store_usages(tmp_path)) == ["r1", "r2", "r3"]
    assert list(run_store.store_usages(tmp_path, since="2026-09-10T00:00:00+00:00")) == ["r2", "r3"]


def test_usages_equals_the_files_plus_every_store_run_without_a_file(tmp_path):
    _three_runs(tmp_path)
    body = {"run_id": "r2", "calls": [], "summary": {"calls": 99}}
    (tmp_path / "r2.usage.json").write_text(json.dumps(body))
    rest = {rid: run_store._store_usage(rid, calls) for rid, calls in run_store._store_runs(tmp_path).items() if rid != "r2"}
    assert run_store.usages(tmp_path) == {"r2": body, **rest}
    assert list(run_store.usages(tmp_path)) == ["r2", "r1", "r3"]


def leases_table(runs_dir, *rows):
    """Rows are (name, holder, expires_at) or (name, holder, expires_at, heartbeat_at)."""
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.executemany("INSERT INTO leases VALUES (?, ?, 1, ?, ?)", [(n, h, (beat or ["2026-09-25T00:00:00Z"])[0], e) for n, h, e, *beat in rows])
    conn.commit()
    conn.close()


def test_lease_reads_the_prefix_row_for_any_run_of_the_prefix(tmp_path):
    leases_table(tmp_path, ("runs:x", "x-3", "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z"), ("runs:y", "y-1", "1970-01-01T00:00:00Z"))
    assert run_store.lease(tmp_path, "x-3") == ("x-3", "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z")
    assert run_store.lease(tmp_path, "x-4") == ("x-3", "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z")
    assert run_store.lease(tmp_path, "z-1") is None


def test_lease_is_none_with_no_store_or_no_leases_table(tmp_path):
    assert run_store.lease(tmp_path, "x-3") is None
    store(tmp_path, ROW)
    assert run_store.lease(tmp_path, "x-3") is None


def test_lease_reads_the_leases_table_once_per_window(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.execute("INSERT INTO leases VALUES ('runs:x', 'x-3', 1, '2026-09-25T12:00:00Z', '2026-09-25T12:02:00Z')")
    conn.commit()
    conn.close()
    opens = []
    real_open = run_store.open_store
    monkeypatch.setattr(run_store, "open_store", lambda runs_dir: opens.append(runs_dir) or real_open(runs_dir))
    assert run_store.lease(tmp_path, "x-3") == ("x-3", "2026-09-25T12:02:00Z", "2026-09-25T12:00:00Z")
    assert run_store.lease(tmp_path, "x-4") == ("x-3", "2026-09-25T12:02:00Z", "2026-09-25T12:00:00Z")
    assert run_store.lease(tmp_path, "y-1") is None
    assert len(opens) == 1


def lease_rows_table(runs_dir, extra_columns, *rows):
    """Rows are (name, holder, epoch, expires_at, heartbeat_at, *extra values)."""
    conn = sqlite3.connect(runs_dir / "cox.db")
    columns = "".join(f", {c} TEXT" for c in extra_columns)
    conn.execute(f"CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, expires_at TEXT, heartbeat_at TEXT{columns})")
    marks = ", ".join("?" * (5 + len(extra_columns)))
    conn.executemany(f"INSERT INTO leases VALUES ({marks})", rows)
    conn.commit()
    conn.close()


def test_lease_row_reads_every_column(tmp_path):
    lease_rows_table(
        tmp_path, ("status", "progress"),
        ("runs:x", "host-a", 4, "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z", "running", "build 3/5"),
    )
    assert run_store.lease_row(tmp_path, "x-3") == run_store.LeaseRow(
        "runs:x", "host-a", 4, "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z", "running", "build 3/5"
    )


def test_lease_row_gives_none_progress_when_the_column_is_absent(tmp_path):
    lease_rows_table(tmp_path, ("status",), ("runs:x", "host-b", 2, "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z", "running"))
    row = run_store.lease_row(tmp_path, "x-1")
    assert (row.epoch, row.status, row.progress) == (2, "running", None)


def test_lease_row_gives_none_for_both_when_the_table_is_not_migrated(tmp_path):
    lease_rows_table(tmp_path, (), ("runs:x", "host-a", 1, "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z"))
    row = run_store.lease_row(tmp_path, "x-1")
    assert (row.epoch, row.status, row.progress) == (1, None, None)


def test_lease_row_is_none_with_no_store(tmp_path):
    assert run_store.lease_row(tmp_path, "x-3") is None


def test_lease_row_is_none_for_a_store_with_no_leases_table(tmp_path, monkeypatch):
    store(tmp_path, ROW)
    opens = []
    real_open = run_store.open_store
    monkeypatch.setattr(run_store, "open_store", lambda runs_dir: opens.append(runs_dir) or real_open(runs_dir))
    assert run_store.lease_row(tmp_path, "x-3") is None
    assert len(opens) == 1


class _ColumnsConn:
    """Records the column query and answers it as Postgres's information_schema would."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return types.SimpleNamespace(fetchall=lambda: [{"name": "epoch"}, {"name": "progress"}])


def test_leases_columns_asks_postgres_for_the_leases_table_by_bind_parameter():
    conn = _ColumnsConn()
    assert run_store._leases_columns(conn, "%s") == {"epoch", "progress"}
    sql, params = conn.calls[0]
    assert "information_schema.columns WHERE table_name = %s" in sql
    assert params == ("leases",)


def test_lease_row_is_none_for_a_prefix_without_a_row(tmp_path):
    lease_rows_table(tmp_path, (), ("runs:y", "host-a", 1, "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z"))
    assert run_store.lease_row(tmp_path, "x-3") is None


def test_lease_row_is_shared_by_x_2_and_x_3(tmp_path):
    lease_rows_table(tmp_path, ("status", "progress"), ("runs:x", "host-a", 7, "2026-09-25T06:00:00Z", "2026-09-25T05:59:48Z", "running", "p"))
    first = run_store.lease_row(tmp_path, "x-2")
    assert first is not None
    assert first == run_store.lease_row(tmp_path, "x-3")


def test_lease_row_reads_the_leases_table_once_per_window(tmp_path, monkeypatch):
    lease_rows_table(tmp_path, ("status", "progress"), ("runs:x", "host-a", 1, "2026-09-25T12:02:00Z", "2026-09-25T12:00:00Z", "running", "p"))
    opens = []
    real_open = run_store.open_store
    monkeypatch.setattr(run_store, "open_store", lambda runs_dir: opens.append(runs_dir) or real_open(runs_dir))
    assert run_store.lease_row(tmp_path, "x-3") is not None
    assert run_store.lease_row(tmp_path, "x-4") is not None
    assert run_store.lease_row(tmp_path, "y-1") is None
    assert len(opens) == 1


NOW = "2026-09-25T12:00:00Z"
LIVE = "2026-09-25T12:02:00Z"


def lane_run(run_id, launched_at):
    return {"run_id": run_id, "launched_at": launched_at}


def lane_store(runs_dir, runs, leases, host_column=False):
    runs_table(runs_dir, *runs)
    if host_column:
        conn = sqlite3.connect(runs_dir / "cox.db")
        conn.execute("ALTER TABLE runs ADD COLUMN host TEXT")
        conn.execute("UPDATE runs SET host = ?", ("h",))
        conn.commit()
        conn.close()
    leases_table(runs_dir, *leases)


def test_a_live_lease_joins_to_the_newest_of_two_run_rows_of_its_prefix(tmp_path):
    lane_store(
        tmp_path,
        [lane_run("x-3", "2026-09-25T09:00:00Z"), lane_run("x-4", "2026-09-25T10:00:00Z"), lane_run("x-y-1", "2026-09-25T11:00:00Z")],
        [("runs:x", "x-4", LIVE, "2026-09-25T11:59:50Z")],
    )
    lane = run_store.Lane("x-4", None, "2026-09-25T10:00:00Z", "2026-09-25T11:59:50Z")
    assert run_store.live_lanes(tmp_path, NOW) == [lane]
    with pytest.raises(dataclasses.FrozenInstanceError):
        lane.run = "x-5"


def test_an_expired_lease_is_dropped(tmp_path):
    lane_store(tmp_path, [lane_run("x-3", "2026-09-25T09:00:00Z")], [("runs:x", "x-3", NOW)])
    assert run_store.live_lanes(tmp_path, NOW) == []


def test_a_store_without_a_host_column_gives_host_none(tmp_path):
    lane_store(tmp_path, [lane_run("x-3", "2026-09-25T09:00:00Z")], [("runs:x", "x-3", LIVE)])
    assert [lane.host for lane in run_store.live_lanes(tmp_path, NOW)] == [None]


def test_a_store_with_a_host_column_gives_the_host(tmp_path):
    lane_store(tmp_path, [lane_run("x-3", "2026-09-25T09:00:00Z")], [("runs:x", "x-3", LIVE)], host_column=True)
    assert [lane.host for lane in run_store.live_lanes(tmp_path, NOW)] == ["h"]


def test_a_live_lease_with_no_run_row_of_its_prefix_is_skipped(tmp_path):
    lane_store(tmp_path, [lane_run("y-1", "2026-09-25T09:00:00Z")], [("runs:x", "x-3", LIVE)])
    assert run_store.live_lanes(tmp_path, NOW) == []


def test_live_lanes_is_empty_with_no_store(tmp_path):
    assert run_store.live_lanes(tmp_path, NOW) == []


def test_lost_lanes_lists_a_lease_that_lapsed_after_its_last_beat_and_not_a_live_one(tmp_path):
    lane_store(
        tmp_path,
        [lane_run("x-3", "2026-09-25T09:00:00Z"), lane_run("y-1", "2026-09-25T09:00:00Z")],
        [("runs:x", "x-3", "2026-09-25T11:42:00Z", "2026-09-25T11:40:00Z"), ("runs:y", "y-1", LIVE, "2026-09-25T11:59:50Z")],
    )
    assert [lane.run for lane in run_store.lost_lanes(tmp_path, NOW)] == ["x-3"]
    assert [lane.run for lane in run_store.live_lanes(tmp_path, NOW)] == ["y-1"]


def test_lost_lanes_skips_a_released_lease(tmp_path):
    lane_store(tmp_path, [lane_run("x-3", "2026-09-25T09:00:00Z")], [("runs:x", "x-3", "1970-01-01T00:00:00Z", "2026-09-25T11:40:00Z")])
    assert run_store.lost_lanes(tmp_path, NOW) == []


def test_newest_run_hosts_maps_each_initiative_to_its_newest_runs_host(tmp_path):
    lane_store(tmp_path, [lane_run("demo-1", "2026-09-25T09:00:00Z")], [], host_column=True)
    assert run_store.newest_run_hosts(tmp_path, ["demo"]) == {"demo": "h"}


def test_remote_lanes_drops_a_local_run_and_keeps_the_rest_in_order():
    a, b, c = (run_store.Lane(run, None, "t", "t") for run in ("a-1", "b-1", "c-1"))
    assert run_store.remote_lanes([a, b, c], {"b-1"}) == [a, c]


def _real_store(runs_dir):
    """A real migrated sqlite store at `<runs_dir>/cox.db`, opened for writes."""
    from harness.store_migrate import open_store

    return open_store(run_store.store_url_for(runs_dir), NOW)


def _seed(runs_dir, write):
    """Run `write(conn)` against the real store at `runs_dir`, then close it."""
    conn = _real_store(runs_dir)
    try:
        write(conn)
    finally:
        conn.close()


def _record_tasks(*rows):
    from harness.store_write import Store

    def write(conn):
        for run, phase, task, record in rows:
            Store(conn).record_task_record(run, phase, task, record, NOW)

    return write


def test_task_record_reads_one_row_and_is_none_for_another_key(tmp_path):
    _seed(tmp_path, _record_tasks(("r-1", "p1", "t1", {"task": "t1"})))
    assert run_store.task_record(tmp_path, "r-1", "p1", "t1") == {"task": "t1"}
    assert run_store.task_record(tmp_path, "r-1", "p1", "t2") is None
    assert run_store.task_record(tmp_path, "r-2", "p1", "t1") is None


def test_task_record_is_none_with_no_store(tmp_path):
    assert run_store.task_record(tmp_path, "r-1", "p1", "t1") is None


def _short_ids(*, initiatives=(), runs=()):
    """Number `initiatives` (slug, short id) through their intake rows and `runs` (run id, short id) in the real store."""
    from harness.store_write import Store, upsert_work_item

    def write(conn):
        mark = conn.dialect.placeholder
        for slug, short in initiatives:
            upsert_work_item(conn, slug, "intake", "", "ready", [], NOW, "me")
            with conn.transaction():
                conn.execute(f"UPDATE work_items SET short_id = {mark} WHERE initiative = {mark} AND task_id = 'intake'", (short, slug))
        for run_id, short in runs:
            Store(conn).record_run({"run_id": run_id, "graph": "g", "status": "running", "started_at": NOW})
            with conn.transaction():
                conn.execute(f"UPDATE runs SET short_id = {mark} WHERE run_id = {mark}", (short, run_id))

    return write


def test_resolve_run_id_maps_a_short_id_and_an_old_key_to_the_same_run(tmp_path):
    _seed(tmp_path, _short_ids(runs=[("2026-09-29-widget-run", "I412-7")]))
    assert run_store.resolve_run_id(tmp_path, "I412-7") == "2026-09-29-widget-run"
    assert run_store.resolve_run_id(tmp_path, "2026-09-29-widget-run") == "2026-09-29-widget-run"


def test_resolve_run_id_is_none_for_an_unknown_token_and_with_no_store(tmp_path):
    assert run_store.resolve_run_id(tmp_path, "I412-7") is None
    _seed(tmp_path, _short_ids(runs=[("some-run", "I1-1")]))
    assert run_store.resolve_run_id(tmp_path, "I999-1") is None


_TWO_INITIATIVES = [("widget-feed", "I412"), ("lake-sync", "I7")]


def test_initiative_short_ids_maps_every_numbered_initiative(tmp_path):
    _seed(tmp_path, _short_ids(initiatives=_TWO_INITIATIVES))
    assert run_store.initiative_short_ids(tmp_path) == {"widget-feed": "I412", "lake-sync": "I7"}


def test_resolve_initiative_maps_a_short_id_to_its_slug_and_leaves_other_tokens_alone(tmp_path):
    _seed(tmp_path, _short_ids(initiatives=_TWO_INITIATIVES))
    assert run_store.resolve_initiative(tmp_path, "I412") == "widget-feed"
    assert run_store.resolve_initiative(tmp_path, "widget-feed") == "widget-feed"
    assert run_store.resolve_initiative(tmp_path, "I999") == "I999"


def test_initiative_lookups_with_no_store_read_empty(tmp_path):
    assert run_store.initiative_short_ids(tmp_path) == {}
    assert run_store.resolve_initiative(tmp_path, "I412") == "I412"


def _work_item_rows(*rows):
    from harness.store_write import upsert_work_item

    def write(conn):
        for initiative, task, phase, state in rows:
            upsert_work_item(conn, initiative, task, phase, state, [], "2026-09-25", "me")

    return write


def test_work_items_reads_every_row_and_filters_by_initiative(tmp_path):
    _seed(tmp_path, _work_item_rows(("wsts", "t1", "p1", "done"), ("other", "t2", "p1", "open")))
    assert [r["task_id"] for r in run_store.work_items(tmp_path)] == ["t1", "t2"]
    assert [r["task_id"] for r in run_store.work_items(tmp_path, "wsts")] == ["t1"]
    assert run_store.work_items(tmp_path, "nobody") == []


def test_work_items_is_empty_with_no_store_and_for_a_store_without_the_table(tmp_path):
    assert run_store.work_items(tmp_path) == []
    sqlite3.connect(tmp_path / "cox.db").close()
    assert run_store.work_items(tmp_path) == []


def test_task_state_is_the_state_of_the_matching_row():
    rows = [{"initiative": "wsts", "task_id": "b", "state": "open"}, {"initiative": "wsts", "task_id": "a", "state": "done"}]
    assert run_store.task_state(rows, "wsts", "a") == "done"


def test_task_state_is_none_when_no_row_matches():
    rows = [{"initiative": "wsts", "task_id": "a", "state": "done"}]
    assert run_store.task_state(rows, "wsts", "zzz") is None
    assert run_store.task_state([], "wsts", "a") is None


def test_task_state_ignores_the_same_task_under_another_initiative():
    other = {"initiative": "other", "task_id": "a", "state": "open"}
    assert run_store.task_state([other], "wsts", "a") is None
    assert run_store.task_state([other, {"initiative": "wsts", "task_id": "a", "state": "done"}], "wsts", "a") == "done"


def test_task_state_of_reads_the_initiative_rows_and_applies_task_state(tmp_path, monkeypatch):
    asked = []

    def fake(runs_dir, initiative=None):
        asked.append((runs_dir, initiative))
        return [{"initiative": "wsts", "task_id": "a", "state": "done"}]

    monkeypatch.setattr(run_store, "work_items", fake)
    assert run_store.task_state_of(tmp_path, "wsts", "a") == "done"
    assert asked == [(tmp_path, "wsts")]


def test_task_state_of_is_none_when_the_reader_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None: [])
    assert run_store.task_state_of(tmp_path, "wsts", "a") is None


def test_task_record_phases_lists_each_phase_once_in_order(tmp_path):
    _seed(tmp_path, _record_tasks(("r-1", "b", "t1", {}), ("r-1", "a", "t1", {}), ("r-2", "c", "t1", {}), ("r-1", "d", "t2", {})))
    assert run_store.task_record_phases(tmp_path, "r-1", "t1") == ["a", "b"]
    assert run_store.task_record_phases(tmp_path / "missing", "r-1", "t1") == []


def test_run_task_ids_lists_each_task_of_the_run_once(tmp_path):
    _seed(tmp_path, _record_tasks(("r-1", "b", "t2", {}), ("r-1", "a", "t1", {}), ("r-1", "c", "t1", {}), ("r-2", "a", "t3", {})))
    assert run_store.run_task_ids(tmp_path, "r-1") == ["t1", "t2"]
    assert run_store.run_task_ids(tmp_path / "missing", "r-1") == []


CLAIM_NOW = "2026-09-26T00:00:00Z"
_QUEUE_ROW = {"kind": "task", "initiative": "i1", "task_id": "t1", "phase": "p1", "state": "ready", "needs": [],
              "surfaces": [], "title": "T", "body": ""}


def test_read_queue_returns_rows_with_every_queue_and_claim_key(tmp_path):
    assert run_store.upsert_row(tmp_path, _QUEUE_ROW)
    assert run_store.upsert_row(tmp_path, {**_QUEUE_ROW, "task_id": "t2"})
    rows = run_store.read_queue(tmp_path, "i1", "task")
    assert [r["task_id"] for r in rows] == ["t1", "t2"]
    assert (rows[0]["holder"], rows[0]["expires_at"]) == (None, None)
    assert run_store.read_queue(tmp_path, "other") == []


def test_upsert_row_is_true_and_repeating_it_changes_nothing(tmp_path):
    assert (run_store.upsert_row(tmp_path, _QUEUE_ROW), run_store.upsert_row(tmp_path, _QUEUE_ROW)) == (True, True)
    assert len(run_store.read_queue(tmp_path)) == 1
    assert run_store.upsert_row_detail(tmp_path, _QUEUE_ROW) == ""


def test_upsert_row_detail_names_why_an_unopenable_store_refused(tmp_path, store_at):
    store_at("nosuch://x")
    assert "unsupported store URL" in run_store.upsert_row_detail(tmp_path, _QUEUE_ROW)
    assert run_store.upsert_row(tmp_path, _QUEUE_ROW) is False


def test_claim_outcome_takes_a_free_row_and_another_holder_finds_it_held(tmp_path):
    assert run_store.upsert_row(tmp_path, _QUEUE_ROW)
    claim = run_store.claim_outcome(tmp_path, "i1", "t1", "me", 300, CLAIM_NOW)[0]
    assert claim is not None and (claim["holder"], claim["epoch"]) == ("me", 1)
    assert run_store.claim_outcome(tmp_path, "i1", "t1", "you", 300, CLAIM_NOW)[0] is None
    assert run_store.claim_outcome(tmp_path, "i1", "t1", "you", 300, CLAIM_NOW) == (None, "held")


def test_claim_outcome_retakes_an_expired_claim_at_the_now_it_was_given(tmp_path):
    assert run_store.upsert_row(tmp_path, _QUEUE_ROW)
    assert run_store.claim_outcome(tmp_path, "i1", "t1", "me", 300, CLAIM_NOW)[0]
    later = "2026-09-26T01:00:00Z"
    claim, why = run_store.claim_outcome(tmp_path, "i1", "t1", "you", 300, later)
    assert why is None and claim["holder"] == "you" and claim["expires_at"] == "2026-09-26T01:05:00Z"


def test_release_row_is_true_whether_or_not_the_holder_held_it(tmp_path):
    assert run_store.upsert_row(tmp_path, _QUEUE_ROW)
    assert run_store.claim_outcome(tmp_path, "i1", "t1", "me", 300, CLAIM_NOW)[0]
    assert run_store.release_row(tmp_path, "i1", "t1", "you") is True
    assert run_store.claim_outcome(tmp_path, "i1", "t1", "you", 300, CLAIM_NOW) == (None, "held")
    assert run_store.release_row(tmp_path, "i1", "t1", "me") is True
    assert run_store.claim_outcome(tmp_path, "i1", "t1", "you", 300, CLAIM_NOW)[0] is not None


def test_an_unopenable_store_reads_as_unavailable_everywhere_and_never_raises(tmp_path, store_at):
    store_at("nosuch://x")
    assert run_store.read_queue(tmp_path) == []
    assert run_store.upsert_row(tmp_path, {"initiative": "i1", "task_id": "t1"}) is False
    assert run_store.release_row(tmp_path, "i1", "t1", "me") is False
    assert run_store.claim_outcome(tmp_path, "i1", "t1", "me", 300, CLAIM_NOW) == (None, "unavailable")


def test_runs_started_reads_every_launched_at_in_one_map(tmp_path):
    runs_table(tmp_path, RUN)
    assert run_store.runs_started(tmp_path) == {"storage-sqlite-run-records-14": "2026-09-25T04:30:00.123456+00:00"}
    assert run_store.runs_started(tmp_path / "missing") == {}


def test_a_queue_row_value_json_cannot_hold_is_written_as_its_text():
    from datetime import date
    from pathlib import PurePosixPath

    row = {"task_id": "t1", "extra": {"path": PurePosixPath("/home/user/x"), "on": date(2026, 11, 17)}}
    assert run_store.plain_row(row) == {"extra": {"on": "2026-11-17", "path": "/home/user/x"}, "task_id": "t1"}
