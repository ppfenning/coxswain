import json
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools import cli
from agent_tools.pacing import Window

# Taken at import, before conftest's autouse fixture replaces them for every test.
_REAL_IMPLIED = cli.usage_meter.implied_ceiling
_REAL_RECORD = cli.usage_meter.record_implied_ceiling
_REAL_RECONCILE = cli.usage_meter.reconcile_implied_ceilings

# Fixed in the past so `_usage_assess`'s own `datetime.now(UTC)` always falls
# after `_END`, pinning `elapsed_fraction` at 1.0 regardless of wall-clock
# time when the test runs.
_START = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
_END = datetime(2020, 1, 1, 10, 0, 0, tzinfo=UTC)


def _unmeasured_window() -> Window:
    return Window(start=_START, end=_END, spent_usd=1.0, ceiling_usd=None,
                  burn_usd_per_hour=0.5, runs_in_flight=1)


def _stopped_window() -> Window:
    return Window(start=_START, end=_END, spent_usd=95.0, ceiling_usd=10.0,
                  burn_usd_per_hour=90.0, runs_in_flight=3)


def test_go_against_an_unmeasured_window_exits_zero_and_names_the_verdict(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    code = cli.main(["usage", "assess", "--runs-dir", "runs", "--profile", str(tmp_path / "no-profile.yaml")])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("go: ")


def test_stop_against_an_exhausted_window_exits_four(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _stopped_window())
    code = cli.main(["usage", "assess", "--profile", str(tmp_path / "no-profile.yaml")])
    out = capsys.readouterr().out
    assert code == 4
    assert out.startswith("stop: ")


def test_json_flag_prints_the_full_assessment(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path),
                      "--profile", str(tmp_path / "no-profile.yaml")])
    d = json.loads(capsys.readouterr().out)
    assert code == 0
    assert d["verdict"] == "go" and d["hold_until"] is None
    # No policy.pacing.json in tmp_path: falls back to DEFAULT_POLICY's own
    # ladders (deep/high), not a guessed tighter one.
    assert d["tier_ceiling"] == "deep" and d["effort_ceiling"] == "high"


def test_a_resolved_policy_file_in_the_runs_dir_changes_the_reported_ceilings(capsys, monkeypatch, tmp_path):
    (tmp_path / "policy.pacing.json").write_text(
        json.dumps({"tier_ladder": ["cheap"], "effort_ladder": ["low"]}), encoding="utf-8"
    )
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path),
                      "--profile", str(tmp_path / "no-profile.yaml")])
    d = json.loads(capsys.readouterr().out)
    assert code == 0
    assert d["tier_ceiling"] == "cheap" and d["effort_ceiling"] == "low"


def test_a_resolved_policy_files_hard_stop_fraction_is_honoured(capsys, monkeypatch, tmp_path):
    (tmp_path / "policy.pacing.json").write_text(
        json.dumps({"hard_stop_fraction": 0.9}), encoding="utf-8"
    )
    window = Window(start=_START, end=_END, spent_usd=95.0, ceiling_usd=100.0,
                     burn_usd_per_hour=0.0, runs_in_flight=1)
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: window)
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path),
                      "--profile", str(tmp_path / "no-profile.yaml")])
    d = json.loads(capsys.readouterr().out)
    assert code == 4
    assert d["verdict"] == "stop"
    assert "hard stop at 90%" in d["reason"]


def test_usage_assess_resolves_the_profiles_window_ceiling_usd_not_unmeasured(capsys, monkeypatch, tmp_path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("workspace_dir: /tmp/ws\nspend:\n  window_ceiling_usd: 50\n", encoding="utf-8")
    captured = {}

    def _gather(runs_dir, now, ceiling_usd=None, usage=None):
        captured["ceiling_usd"] = ceiling_usd
        return Window(start=_START, end=_END, spent_usd=5.0, ceiling_usd=ceiling_usd,
                      burn_usd_per_hour=0.0, runs_in_flight=1)

    monkeypatch.setattr(cli.usage_window, "gather", _gather)
    code = cli.main(["usage", "assess", "--profile", str(profile_path), "--runs-dir", "runs"])
    out = capsys.readouterr().out
    assert code == 0
    assert captured["ceiling_usd"] == 50.0
    assert "unmeasured" not in out


def test_usage_assess_json_passes_the_profiles_parsed_weekly_reset_to_gather_weekly(capsys, monkeypatch, tmp_path):
    # `route.parse_profile`'s own grammar for `spend.weekly_reset` is out of
    # scope here; the profile dict it would produce is supplied directly so
    # this test exercises only `_usage_assess`'s wiring of that value through
    # to `gather_weekly`.
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("workspace_dir: /tmp/ws\n", encoding="utf-8")
    monkeypatch.setattr(cli.route, "parse_profile", lambda text: {"weekly_reset": "Sun 04:00 America/New_York"})
    captured = {}

    def _gather_weekly(runs_dir, now, weekly_ceiling_usd=None, usage=None, store_spend=None, reset=None):
        captured["reset"] = reset
        return Window(start=_START, end=_END, spent_usd=5.0, ceiling_usd=weekly_ceiling_usd,
                      burn_usd_per_hour=0.0, runs_in_flight=1)

    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    monkeypatch.setattr(cli.usage_window, "gather_weekly", _gather_weekly)
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path), "--profile", str(profile_path)])
    d = json.loads(capsys.readouterr().out)
    assert code == 0
    assert d["verdict"] == "go"
    assert captured["reset"] == cli.usage_window.parse_weekly_reset("Sun 04:00 America/New_York")


def test_a_fresh_meter_calibrates_against_the_runs_dir(tmp_path, monkeypatch):
    import datetime as _dt
    from pathlib import Path as _Path

    from agent_tools import cli as _cli
    from agent_tools import usage_meter as _um

    now = _dt.datetime.now(_dt.UTC)
    entry = _um.MeterEntry(used_percentage=40.0, resets_at=now + _dt.timedelta(hours=1))
    monkeypatch.setattr(_um, "read", lambda *a, **k: _um.Meter(five_hour=entry, seven_day=entry, observed_at=now))
    seen = []
    monkeypatch.setattr(_um, "record_implied_ceiling", lambda kind, e, runs_dir, when, *a, **k: seen.append((kind, runs_dir)))
    _cli._usage_assessment(tmp_path / "runs", window_ceiling_usd=200.0, weekly_ceiling_usd=1380.0)
    assert seen == [("five_hour", _Path(tmp_path / "runs")), ("weekly", _Path(tmp_path / "runs"))]


@pytest.fixture(autouse=True)
def _reconcile_into_tmp(monkeypatch, tmp_path):
    """A fresh reading reconciles, which writes the state file; no test here may write the box's real one."""
    monkeypatch.setattr(
        cli.usage_meter, "reconcile_implied_ceilings",
        lambda meter, runs_dir: _REAL_RECONCILE(meter, runs_dir, tmp_path / "default-implied-ceilings.json"),
    )


def _chair_doc(age_minutes: float) -> dict:
    now = datetime.now(UTC)
    entry = {"used_percentage": 84.0, "resets_at": int((now + timedelta(hours=1)).timestamp())}
    return {"five_hour": entry, "seven_day": entry, "observed_at": (now - timedelta(minutes=age_minutes)).isoformat()}


def _estimate_only(monkeypatch):
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: _unmeasured_window())
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: None)


def test_a_fresh_chair_reading_with_no_local_one_sets_the_window(monkeypatch, tmp_path):
    _estimate_only(monkeypatch)
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: _chair_doc(5))
    assert "weekly 84% of weekly ceiling" in cli._usage_assessment(tmp_path).reason


def test_a_chair_reading_older_than_fifteen_minutes_gives_the_estimate_result(monkeypatch, tmp_path):
    _estimate_only(monkeypatch)
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: None)
    expected = cli._usage_assessment(tmp_path)
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: _chair_doc(16))
    stale = cli._usage_assessment(tmp_path)
    assert stale == expected and "84%" not in stale.reason


def test_a_chair_reading_never_writes_an_implied_ceiling(monkeypatch, tmp_path):
    _estimate_only(monkeypatch)
    seen = []
    monkeypatch.setattr(cli.usage_meter, "record_implied_ceiling", lambda *a, **k: seen.append(a))
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: _chair_doc(5))
    cli._usage_assessment(tmp_path)
    assert seen == []


def test_a_chair_reading_with_a_naive_observed_at_falls_back_to_the_estimate(monkeypatch, tmp_path):
    _estimate_only(monkeypatch)
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: None)
    expected = cli._usage_assessment(tmp_path)
    naive = {**_chair_doc(5), "observed_at": datetime.now(UTC).replace(tzinfo=None).isoformat()}
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: naive)
    assert cli._usage_assessment(tmp_path) == expected


def test_the_policy_files_weekly_hard_stop_applies_to_the_chair_reading(monkeypatch, tmp_path):
    _estimate_only(monkeypatch)
    (tmp_path / "policy.pacing.json").write_text(json.dumps({"weekly_hard_stop_fraction": 0.80}), encoding="utf-8")
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: _chair_doc(5))
    assert cli._usage_assessment(tmp_path).verdict == "stop"


def _meter(five_hour_pct: float | None, weekly_pct: float, now: datetime):
    def entry(pct: float, hours: int):
        return cli.usage_meter.MeterEntry(used_percentage=pct, resets_at=now + timedelta(hours=hours))

    return cli.usage_meter.Meter(
        five_hour=None if five_hour_pct is None else entry(five_hour_pct, 1),
        seven_day=entry(weekly_pct, 72), observed_at=now,
    )


def _temp_ceilings(monkeypatch, tmp_path, doc: dict):
    path = tmp_path / "implied-ceilings.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    # `usable_percentage` and `reconcile_implied_ceilings` pass their own path, which this redirect replaces.
    monkeypatch.setattr(cli.usage_meter, "implied_ceiling", lambda kind, now, *a: _REAL_IMPLIED(kind, now, path))
    monkeypatch.setattr(
        cli.usage_meter, "record_implied_ceiling",
        lambda kind, entry, runs_dir, now: _REAL_RECORD(kind, entry, runs_dir, now, path),
    )
    monkeypatch.setattr(
        cli.usage_meter, "reconcile_implied_ceilings", lambda meter, runs_dir: _REAL_RECONCILE(meter, runs_dir, path),
    )
    return path


def _no_five_hour_estimate(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the five-hour estimate must not be gathered")

    monkeypatch.setattr(cli.usage_window, "gather", refuse)
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: _unmeasured_window())


def test_a_null_five_hour_is_no_limit_even_with_a_learned_ceiling_and_a_store_row(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    learned = {"five_hour": {"ceiling_usd": 1.0, "observed_at": now.isoformat()}}
    path = _temp_ceilings(monkeypatch, tmp_path, learned)
    _no_five_hour_estimate(monkeypatch)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(None, 4.0, now))
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: _chair_doc(5))
    assessment = cli._usage_assessment(tmp_path)
    policy = cli._resolved_pacing_policy(tmp_path)
    assert assessment.verdict == "go"
    assert "below minimum" not in assessment.reason
    assert (assessment.tier_ceiling, assessment.effort_ceiling) == (policy.tier_ladder[0], policy.effort_ladder[0])
    assert "weekly 4%" in assessment.reason
    assert {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if k != "last_meter"} == learned


def test_a_fresh_local_reading_wins_over_a_store_row_recorded_a_minute_earlier(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    _temp_ceilings(monkeypatch, tmp_path, {})
    _no_five_hour_estimate(monkeypatch)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(None, 4.0, now))
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: _chair_doc(1))
    reason = cli._usage_assessment(tmp_path).reason
    assert "weekly 4%" in reason and "84%" not in reason


def test_a_reading_with_a_five_hour_entry_relearns_the_five_hour_ceiling(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    path = _temp_ceilings(monkeypatch, tmp_path, {})
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: _unmeasured_window())
    monkeypatch.setattr(cli.usage_meter.run_store, "cost_since", lambda runs_dir, since: 20.0)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(40.0, 40.0, now))
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: None)
    cli._usage_assessment(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["five_hour"]["ceiling_usd"] == 50.0


def _last_meter_doc(now: datetime, *, has_five_hour: bool, weekly_used: float) -> dict:
    return {
        "has_five_hour": has_five_hour,
        "weekly_used": weekly_used,
        "weekly_resets_at": (now + timedelta(hours=72)).isoformat(),
        "observed_at": (now - timedelta(minutes=10)).isoformat(),
    }


def _ceiling(usd: float, now: datetime) -> dict:
    return {"ceiling_usd": usd, "observed_at": now.isoformat()}


def _capture_ceilings(monkeypatch) -> dict:
    seen: dict = {}

    def gather(runs_dir, now, ceiling_usd=None, **k):
        seen["five_hour"] = ceiling_usd
        return _unmeasured_window()

    def gather_weekly(runs_dir, now, ceiling_usd=None, **k):
        seen["weekly"] = ceiling_usd
        return _unmeasured_window()

    monkeypatch.setattr(cli.usage_window, "gather", gather)
    monkeypatch.setattr(cli.usage_window, "gather_weekly", gather_weekly)
    monkeypatch.setattr(cli.usage_meter.run_store, "cost_since", lambda runs_dir, since: 1.0)
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: None)
    return seen


def test_a_lost_five_hour_window_drops_the_learned_five_hour_ceiling_and_the_verdict_is_go(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    learned = {
        "five_hour": _ceiling(52.81, now),
        "last_meter": _last_meter_doc(now, has_five_hour=True, weekly_used=4.0),
    }
    path = _temp_ceilings(monkeypatch, tmp_path, learned)
    _capture_ceilings(monkeypatch)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(None, 4.0, now))
    assert cli._usage_assessment(tmp_path, window_ceiling_usd=200.0).verdict == "go"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "five_hour" not in doc
    assert doc["last_meter"]["has_five_hour"] is False


def test_a_weekly_fall_beyond_the_spend_drops_both_ceilings_and_the_profile_ceiling_applies(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    learned = {
        "five_hour": _ceiling(52.81, now),
        "weekly": _ceiling(1000.0, now),
        "last_meter": _last_meter_doc(now, has_five_hour=True, weekly_used=25.0),
    }
    path = _temp_ceilings(monkeypatch, tmp_path, learned)
    seen = _capture_ceilings(monkeypatch)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(3.0, 4.0, now))
    cli._usage_assessment(tmp_path, window_ceiling_usd=200.0, weekly_ceiling_usd=1380.0)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert set(doc) == {"last_meter"}
    assert seen == {"five_hour": 200.0, "weekly": 1380.0}


def test_an_ordinary_reading_change_clears_no_ceiling(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    learned = {
        "five_hour": _ceiling(52.81, now),
        "weekly": _ceiling(1000.0, now),
        "last_meter": _last_meter_doc(now, has_five_hour=True, weekly_used=5.0),
    }
    path = _temp_ceilings(monkeypatch, tmp_path, learned)
    seen = _capture_ceilings(monkeypatch)
    # Both percentages stay under the 5% calibration floor, so only reconcile could change the file.
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(3.0, 4.0, now))
    cli._usage_assessment(tmp_path, window_ceiling_usd=200.0, weekly_ceiling_usd=1380.0)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["five_hour"] == learned["five_hour"] and doc["weekly"] == learned["weekly"]
    assert doc["last_meter"]["weekly_used"] == 4.0
    assert seen == {"five_hour": 52.81, "weekly": 1000.0}


def test_a_stale_local_reading_does_not_reconcile(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    learned = {
        "five_hour": _ceiling(52.81, now),
        "last_meter": _last_meter_doc(now, has_five_hour=True, weekly_used=25.0),
    }
    path = _temp_ceilings(monkeypatch, tmp_path, learned)
    _capture_ceilings(monkeypatch)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(None, 4.0, now - timedelta(minutes=20)))
    cli._usage_assessment(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == learned


def test_a_store_sourced_reading_does_not_reconcile(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    learned = {
        "five_hour": _ceiling(52.81, now),
        "last_meter": _last_meter_doc(now, has_five_hour=True, weekly_used=25.0),
    }
    path = _temp_ceilings(monkeypatch, tmp_path, learned)
    _capture_ceilings(monkeypatch)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: None)
    monkeypatch.setattr(cli.run_store, "latest_chair_meter", lambda runs_dir: _chair_doc(5))
    cli._usage_assessment(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == learned


def test_the_chairs_window_and_weekly_reconcile_once_per_tick(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    _temp_ceilings(monkeypatch, tmp_path, {})
    _capture_ceilings(monkeypatch)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _meter(3.0, 4.0, now))
    calls: list = []
    monkeypatch.setattr(cli.usage_meter, "reconcile_implied_ceilings", lambda meter, runs_dir: calls.append(meter))
    deps = cli._chair_run_deps(
        tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files",
    )
    deps.facts_deps.window()
    deps.facts_deps.weekly()
    assert len(calls) == 1
