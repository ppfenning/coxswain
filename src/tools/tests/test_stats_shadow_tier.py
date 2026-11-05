import datetime
import json
import sqlite3

import pytest

from agent_tools import stats_shadow_tier as sst
from agent_tools import stats_system_one
from agent_tools.cli import main


def call(i, *, role="handoff", agreed=True, costly=False, verdict=True, model="cheap-1", cost=0.10, shadow_cost=0.01, day=1):
    ts = datetime.datetime(2026, 9, day, tzinfo=datetime.UTC) + datetime.timedelta(minutes=i)
    return {
        "role": role, "ts": ts.isoformat(), "cost_usd": cost,
        "decision": {
            "role": role, "shadow_model_id": model, "shadow_tier": "haiku", "shadow_verdict": verdict,
            "shadow_agreed": agreed, "shadow_costly": costly, "shadow_cost_usd": shadow_cost,
        },
    }


def rows_for(calls):
    return sst.rows_from_usage([{"calls": calls}])


def only(calls, role="handoff", since=None):
    (s,) = sst.summaries(rows_for(calls), since, role)
    return s


def test_bars_are_the_system_one_definitions():
    assert (sst.MIN_ROWS, sst.MIN_AGREEMENT, sst.MAX_COSTLY_RATE) == (
        stats_system_one.MIN_ROWS, stats_system_one.MIN_AGREEMENT, stats_system_one.MAX_COSTLY_RATE,
    )


def test_agreement_costly_rate_and_saving_are_per_role():
    calls = [
        call(0, agreed=True), call(1, agreed=True), call(2, agreed=False, verdict=True, costly=True), call(3, agreed=True),
        call(0, role="review_charter", agreed=True, cost=0.5, shadow_cost=0.1),
        call(1, role="review_charter", agreed=False, cost=0.5, shadow_cost=0.1),
    ]
    by_role = {s.role: s for s in sst.summaries(rows_for(calls), None, None)}
    h, r = by_role["handoff"], by_role["review_charter"]
    assert (h.rows, h.agreed, h.costly) == (4, 3, 1)
    assert h.agreement == 0.75 and h.costly_rate == 0.25
    assert h.acting_cost == pytest.approx(0.4) and h.shadow_cost == pytest.approx(0.04)
    assert h.saved == pytest.approx(0.36) and h.saved_per_call == pytest.approx(0.09)
    assert (r.rows, r.agreed, r.costly) == (2, 1, 0)
    assert r.saved == pytest.approx(0.8) and r.saved_per_call == pytest.approx(0.4)


def test_shadow_passing_what_acting_failed_is_costly_and_the_reverse_is_not():
    passes_what_acting_failed = call(0, agreed=False, verdict=True, costly=True)
    stops_what_acting_passed = call(1, agreed=False, verdict=False, costly=False)
    s = only([passes_what_acting_failed, stops_what_acting_passed])
    assert (s.rows, s.agreed, s.costly) == (2, 0, 1)


def test_rows_without_shadow_fields_are_ignored():
    plain = {"role": "handoff", "ts": "2026-09-01T00:00:00+00:00", "cost_usd": 1.0, "decision": {"role": "handoff", "model_id": "m"}}
    null_agreed = call(1)
    null_agreed["decision"]["shadow_agreed"] = None
    cheap_call = {"role": "handoff", "ts": "2026-09-01T00:02:00+00:00", "cost_usd": 0.01}
    no_decision = {"role": "handoff", "ts": "2026-09-01T00:03:00+00:00", "decision": None}
    kept = call(4)
    rows = rows_for([plain, null_agreed, cheap_call, no_decision, kept])
    assert len(rows) == 1 and rows[0].acting_cost == 0.10


def test_missing_or_non_numeric_costs_count_as_zero():
    c = call(0)
    c["cost_usd"] = None
    c["decision"]["shadow_cost_usd"] = "x"
    s = only([c])
    assert (s.acting_cost, s.shadow_cost) == (0.0, 0.0)


def test_window_keeps_the_trailing_run_of_one_shadow_model_id():
    calls = [call(0, model="old"), call(1, model="cheap-1"), call(2, model="cheap-1")]
    assert only(calls).rows == 2
    assert sst.summaries(rows_for(calls), "2026-09-02", None) == []


def test_ready_at_100_rows_96_agreed_no_costly():
    calls = [call(i, agreed=i >= 4, costly=False, verdict=False) for i in range(100)]
    s = only(calls)
    assert sst.verdict(s) == ("READY", [])
    assert "handoff: READY" in sst.render_report([s])


def test_not_yet_names_the_costly_rate():
    calls = [call(i, agreed=i >= 2, costly=i < 2) for i in range(100)]
    label, unmet = sst.verdict(only(calls))
    assert label == "NOT YET"
    assert unmet == ["costly rate 0.020 > 0.01"]


def test_not_yet_lists_every_unmet_number():
    calls = [call(i, agreed=i >= 10) for i in range(50)]
    label, unmet = sst.verdict(only(calls))
    assert label == "NOT YET"
    assert unmet == ["rows 50 < 100", "agreement 0.800 < 0.95"]


def test_text_report_says_it_changes_nothing():
    text = sst.render_report([only([call(0)])])
    assert "changes nothing" in text and "Only an operator moves a role" in text
    assert "changes nothing" in sst.render_report([])


def test_json_parses_and_holds_the_same_numbers():
    s = only([call(0), call(1, agreed=False, costly=True)])
    (out,) = json.loads(json.dumps(sst.to_json([s])))["roles"]
    assert out["rows"] == 2 and out["agreed"] == 1 and out["costly"] == 1
    assert out["agreement"] == s.agreement and out["costly_rate"] == s.costly_rate
    assert out["acting_cost"] == s.acting_cost and out["shadow_cost"] == s.shadow_cost
    assert out["saved"] == s.saved and out["saved_per_call"] == s.saved_per_call
    assert out["verdict"] == "NOT YET" and out["unmet"][0] == "rows 2 < 100"


def seed_store(runs, run_id, calls):
    """An ended `runs` row and one `node_calls` row per call, with no usage file."""
    columns = (
        "call_id", "run_id", "seq", "role", "task_id", "tier", "model_alias", "cost_usd", "ceiling_usd", "ceiling_source",
        "turns", "duration_ms", "input_tokens", "cache_read_tokens", "cache_creation_tokens", "input_total", "output_tokens",
        "ok", "ts", "decision_json",
    )
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, launched_at TEXT, ended_at TEXT)")
    conn.execute("CREATE TABLE node_calls (" + ", ".join(columns) + ")")
    conn.execute("INSERT INTO runs VALUES (?, ?, ?)", (run_id, "2026-09-01T00:00:00+00:00", "2026-09-01T01:00:00+00:00"))
    for seq, c in enumerate(calls):
        row = (f"{run_id}-{seq}", run_id, seq, c["role"], None, None, "haiku", c["cost_usd"], None, None, 1, 1, 0, 0, 0, 0, 0, 1, c["ts"], json.dumps(c["decision"]))
        conn.execute("INSERT INTO node_calls VALUES (" + ", ".join("?" * len(columns)) + ")", row)
    conn.commit()
    conn.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    profile = tmp_path / "profile.yaml"
    profile.write_text("assume: a\n")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    runs = tmp_path / "runs"
    runs.mkdir()
    calls = [call(i, agreed=i >= 4, verdict=False) for i in range(100)]
    calls += [call(i, role="review_charter") for i in range(3)]
    calls += [{"role": "handoff", "ts": "2026-09-01T05:00:00+00:00", "cost_usd": 0.01, "decision": {"role": "handoff", "model_id": "m"}}]
    seed_store(runs, "r1", calls)
    return runs


def test_the_handler_prints_a_report_for_a_seeded_store(store, capsys):
    assert main(["stats", "shadow-tier", str(store)]) == 0
    out = capsys.readouterr().out
    assert "handoff: READY" in out
    assert "review_charter: NOT YET (rows 3 < 100)" in out
    assert "Only an operator moves a role" in out


def test_the_handler_filters_by_role_and_emits_json(store, capsys):
    assert main(["stats", "shadow-tier", str(store), "--role", "handoff", "--json"]) == 0
    (out,) = json.loads(capsys.readouterr().out)["roles"]
    assert out["role"] == "handoff" and out["rows"] == 100 and out["agreed"] == 96


def test_the_handler_honours_since_and_writes_nothing(store, capsys):
    assert main(["stats", "shadow-tier", str(store), "--since", "2026-10-01"]) == 0
    assert capsys.readouterr().out.startswith("no shadow rows in any usage file")
    assert not (store.parent / "plans").exists()
