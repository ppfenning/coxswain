import datetime
import json
import sys
import types

import pytest

from agent_tools import cli, pacing, usage_window

# Mid-month, so elapsed is 15.5 of 30 days and the projection is a fixed number.
_NOW = datetime.datetime(2026, 11, 16, 12, 0, 0, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    class _Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    monkeypatch.setattr(cli, "datetime", types.SimpleNamespace(**{**vars(datetime), "datetime": _Frozen}))


def _profile(tmp_path, spend_lines: str = "  monthly_budget_usd: 100\n"):
    path = tmp_path / "profile.yaml"
    path.write_text(f"workspace_dir: {tmp_path}/ws\nspend:\n{spend_lines}", encoding="utf-8")
    return path


def _store_spend(monkeypatch, usd: float | None) -> list[tuple]:
    calls: list[tuple] = []

    def _cost_since(runs_dir, since, until=None):
        calls.append((since, until))
        return usd

    monkeypatch.setattr(cli.run_store, "cost_since", _cost_since)
    return calls


def _no_window_gather(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("the five-hour and weekly windows must not be gathered")

    monkeypatch.setattr(cli.usage_window, "gather", _boom)
    monkeypatch.setattr(cli.usage_window, "gather_weekly", _boom)


def test_month_only_stops_at_the_hard_stop_and_prints_the_exact_line(capsys, monkeypatch, tmp_path):
    _no_window_gather(monkeypatch)
    calls = _store_spend(monkeypatch, 99.5)
    code = cli.main(["usage", "assess", "--runs-dir", str(tmp_path), "--profile", str(_profile(tmp_path))])
    assert code == 4
    assert capsys.readouterr().out == (
        "stop: month spend 100% of monthly budget (hard stop at 93%)"
        " [month to date $99.50, projected $192.58, over_pace]\n"
    )
    assert calls == [("2026-11-01T00:00:00+00:00", "2026-11-16T12:00:00+00:00")]


def test_month_only_under_the_hard_stop_goes_and_prints_the_exact_line(capsys, monkeypatch, tmp_path):
    _no_window_gather(monkeypatch)
    _store_spend(monkeypatch, 10.0)
    code = cli.main(["usage", "assess", "--runs-dir", str(tmp_path), "--profile", str(_profile(tmp_path))])
    assert code == 0
    assert capsys.readouterr().out == (
        "go: month spent 10% of budget at 52% elapsed, projected $19.35 of $100.00; on track"
        " [month to date $10.00, projected $19.35, on_track]\n"
    )


def test_json_output_carries_the_month_fields(capsys, monkeypatch, tmp_path):
    _no_window_gather(monkeypatch)
    _store_spend(monkeypatch, 150.0)
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path), "--profile", str(_profile(tmp_path))])
    d = json.loads(capsys.readouterr().out)
    assert code == 4
    assert d["verdict"] == "stop" and d["month_verdict"] == "over_budget"
    assert d["month_spent_usd"] == 150.0 and d["month_projected_usd"] > 150.0


def test_no_budget_set_reads_the_windows_as_before_and_reports_no_month(capsys, monkeypatch, tmp_path):
    calls = _store_spend(monkeypatch, 500.0)
    gathered = []

    def _gather(runs_dir, now, ceiling_usd=None, usage=None):
        gathered.append(ceiling_usd)
        return pacing.Window(start=now, end=now, spent_usd=1.0, ceiling_usd=None, burn_usd_per_hour=0.0, runs_in_flight=0)

    monkeypatch.setattr(cli.usage_window, "gather", _gather)
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: None)
    real_assess = pacing.assess
    # No `month` parameter: with no budget the call keeps its pre-month shape, as the older tests' stand-ins expect.
    monkeypatch.setattr(cli.pacing, "assess", lambda window, policy, now, weekly=None: real_assess(window, policy, now, weekly=weekly))
    profile = _profile(tmp_path, "  window_ceiling_usd: 50\n")
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path), "--profile", str(profile)])
    d = json.loads(capsys.readouterr().out)
    assert code == 0
    assert gathered == [50.0] and calls == []
    assert d["month_verdict"] is None and d["month_spent_usd"] is None


def test_a_window_ceiling_beside_the_budget_still_gathers_the_windows(monkeypatch, tmp_path):
    _store_spend(monkeypatch, 1.0)
    gathered = []

    def _gather(runs_dir, now, ceiling_usd=None, usage=None):
        gathered.append(ceiling_usd)
        return pacing.Window(start=now, end=now, spent_usd=1.0, ceiling_usd=ceiling_usd, burn_usd_per_hour=0.0, runs_in_flight=0)

    monkeypatch.setattr(cli.usage_window, "gather", _gather)
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: None)
    result = cli._usage_assessment(tmp_path, 50.0, None, None, 100.0, None)
    assert gathered == [50.0]
    assert result.month_spent_usd == 1.0


def _launch_profile(tmp_path):
    harness_dir = tmp_path / "harness"
    (harness_dir / ".venv" / "bin").mkdir(parents=True)
    (harness_dir / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (harness_dir / "shell.py").write_text("")
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nharness_dir: {harness_dir}\nworkspace_dir: {ws}\ncartridges_dir: /opt/cartridges\n"
        "provider_profile: /opt/providers/acme.yaml\nspend:\n  monthly_budget_usd: 100\n"
    )
    return profile


def test_route_launch_refuses_at_the_month_stop_and_force_overrides_it(capsys, monkeypatch, tmp_path):
    _no_window_gather(monkeypatch)
    _store_spend(monkeypatch, 99.5)
    profile = _launch_profile(tmp_path)
    rc = cli.main(["route", "launch", "cos", "--dry-run", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "routing: usage stop: month spend 100% of monthly budget (hard stop at 93%)" in out
    assert not (tmp_path / "ws" / "runs" / "cos-1.pid").exists()

    rc = cli.main(["route", "launch", "cos", "--dry-run", "--force", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "overridden by --force" in out


def test_route_context_docket_line_narrates_the_month(capsys, monkeypatch, tmp_path):
    _no_window_gather(monkeypatch)
    _store_spend(monkeypatch, 99.5)
    (tmp_path / "ws" / "runs").mkdir(parents=True)
    code = cli.main(["route", "context", "--json", "--profile", str(_profile(tmp_path))])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "month spend 100% of monthly budget" in doc["usage"]


@pytest.mark.parametrize(("budget_timezone", "weekly_reset", "zone"), [
    ("Asia/Tokyo", "Sun 04:00 America/New_York", "Asia/Tokyo"),
    (None, "Sun 04:00 America/New_York", "America/New_York"),
    (None, None, "UTC"),
])
def test_the_budget_zone_defaults_to_the_weekly_reset_zone_then_utc(budget_timezone, weekly_reset, zone):
    assert cli._budget_zone(budget_timezone, weekly_reset) == zone


def test_the_profile_month_reads_the_three_profile_keys(monkeypatch, tmp_path):
    zones = []
    real = usage_window.gather_month

    def _spy(runs_dir, now, tz_name, budget_usd, *a, **k):
        zones.append((tz_name, budget_usd))
        return real(runs_dir, now, tz_name, budget_usd, *a, **k)

    monkeypatch.setattr(cli.usage_window, "gather_month", _spy)
    _store_spend(monkeypatch, 10.0)
    month = cli._profile_month(tmp_path, {"monthly_budget_usd": 40.0, "weekly_reset": "Sun 04:00 Asia/Tokyo"}, _NOW, 0.1)
    assert zones == [("Asia/Tokyo", 40.0)] and month.spent_usd == 10.0
    assert cli._profile_month(tmp_path, {}, _NOW, 0.1) is None
