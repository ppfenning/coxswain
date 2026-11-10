from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent_tools import chair_exec, chair_report, cli
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_run import RunDeps, as_holder, run, tick_timed

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)
MINE = {"holder": "me", "host": "box", "epoch": 3, "mine": True, "released": False, "stale": False}
FOREIGN = {**MINE, "holder": "other", "mine": False}


def _facts(lease: dict) -> dict:
    return {
        "lease": lease,
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
        "approved": [
            {"id": "t1", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": "x-1", "needs_fetch": False}
        ],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
        "last_housekeeping_at": NOW.isoformat(),
        "housekeeping_hours": 24,
    }


class Rig:
    """Recording fakes for every side effect a tick could have."""

    def __init__(self, lease: dict) -> None:
        self.lease, self.lines, self.commands, self.acquired, self.recorded, self.statuses, self.planned = lease, [], [], [], [], [], []

    def _run(self, argv):
        self.commands.append(argv)
        return 0, "merge: ok mark_done: ok"

    def _acquire(self, holder, host):
        self.acquired.append((holder, host))
        return ""

    def _plan(self, facts, _now):
        self.planned.extend(plan_tick(facts))
        return self.planned

    def deps(self, tmp_path, **over) -> RunDeps:
        exec_deps = chair_exec.Deps(
            run=self._run,
            delete_branches=lambda repo, pattern: ([], ""),
            acquire_lease=self._acquire,
            record=self.recorded.append,
            run_id=lambda action: "run-1",
            repo_for=lambda action: "r",
            launch_stagger_s=0.0,
        )
        base = RunDeps(
            facts_deps=object(),
            exec_deps=exec_deps,
            report_deps=chair_report.Deps(echo=self.lines.append, notify=None),
            beat=lambda: None,
            current_epoch=lambda: 3,
            holds=lambda: False,
            release=lambda: None,
            sleep=lambda _s: None,
            now=lambda: NOW,
            gather=lambda _d, _n: _facts(self.lease),
            plan=self._plan,
            runs_dir=tmp_path,
            write_tick_status=lambda *a: self.statuses.append(a) or True,
        )
        return replace(base, **over)

    def kinds(self) -> list[str]:
        return [a["kind"] for a in self.planned]


def test_as_holder_forces_mine_on_a_live_foreign_lease_without_mutating_its_input() -> None:
    facts = _facts(FOREIGN)
    assert as_holder(facts) == facts
    assert as_holder(facts, force=True)["lease"]["mine"] is True
    assert facts["lease"]["mine"] is False


def test_a_dry_run_behind_a_live_foreign_holder_plans_standby_without_the_flag(tmp_path) -> None:
    rig = Rig(FOREIGN)
    tick_timed(rig.deps(tmp_path), True, NOW)
    assert rig.kinds() == ["standby"]


def test_force_holder_plans_the_same_actions_as_a_lease_that_is_mine(tmp_path) -> None:
    mine, forced = Rig(MINE), Rig(FOREIGN)
    tick_timed(mine.deps(tmp_path), True, NOW)
    tick_timed(forced.deps(tmp_path, force_holder=True), True, NOW)
    assert forced.kinds() == mine.kinds() == ["land_phase"]


def test_force_holder_is_ignored_on_a_live_tick(tmp_path) -> None:
    rig = Rig(FOREIGN)
    tick_timed(rig.deps(tmp_path, force_holder=True), False, NOW)
    assert rig.kinds() == ["standby"]


def test_a_forced_dry_run_takes_no_lease_performs_nothing_and_publishes_no_status(tmp_path) -> None:
    rig = Rig(FOREIGN)
    run(True, 60, True, rig.deps(tmp_path, force_holder=True))
    assert rig.kinds() == ["land_phase"]
    assert (rig.acquired, rig.commands, rig.recorded, rig.statuses) == ([], [], [], [])
    assert list(tmp_path.iterdir()) == []


def test_the_parser_accepts_as_holder_and_defaults_it_off() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["chair", "run", "--dry-run", "--as-holder"]).as_holder is True
    assert parser.parse_args(["chair", "run", "--dry-run"]).as_holder is False


def test_as_holder_without_dry_run_refuses_with_2_and_takes_no_lease(monkeypatch, tmp_path, capsys) -> None:
    def boom(name: str):
        return lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(f"{name} ran"))

    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", boom("runs dir"))
    monkeypatch.setattr(cli.chair, "renew_lease", boom("beat"))
    monkeypatch.setattr(cli.chair_run, "run", boom("loop"))
    args = cli.build_parser().parse_args(["chair", "run", "--once", "--as-holder"])
    assert args.fn(args) == 2
    assert capsys.readouterr().out.startswith("chair run: refusing to start, no lease taken")
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("argv, expected", [(["--dry-run", "--as-holder"], True), (["--dry-run"], False)])
def test_the_command_hands_the_flag_to_the_loop_as_force_holder(monkeypatch, tmp_path, argv, expected) -> None:
    deps = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", True, print, tmp_path / "p.yaml", "files")
    seen = []
    monkeypatch.setattr(cli, "_lake_provider", lambda _a: ({}, None))
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda _a: ({}, tmp_path, None))
    monkeypatch.setattr(cli, "_chair_run_deps", lambda *_a: deps)
    monkeypatch.setattr(cli, "_chair_unwired_sources", lambda _d: [])
    monkeypatch.setattr(cli.chair_run, "run", lambda *a: seen.append(a[3].force_holder))
    args = cli.build_parser().parse_args(["chair", "run", "--once", *argv])
    args.fn(args)
    assert seen == [expected]
