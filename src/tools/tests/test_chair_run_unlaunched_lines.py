from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from agent_tools import chair_exec, chair_report
from agent_tools.chair_run import RunDeps, _attempt

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)
MINE = {"holder": "me", "host": "box", "epoch": 3, "mine": True, "released": False, "stale": False}
FOREIGN = {**MINE, "holder": "other", "mine": False}
TWO = [
    {"kind": "unlaunched", "initiative": "alpha", "reason": "waiting on beta"},
    {"kind": "unlaunched", "initiative": "gamma", "reason": "launch cap reached"},
]
STAMP = chair_report.unlaunched_lines(TWO[:1], NOW)[0].split(" not launched")[0]
REASONS = [f"{STAMP} not launched alpha: waiting on beta", f"{STAMP} not launched gamma: launch cap reached"]


def _facts(lease: dict) -> dict:
    return {
        "lease": lease,
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
        "last_housekeeping_at": NOW.isoformat(),
        "housekeeping_hours": 24,
    }


def _deps(actions: list[dict], lease: dict = MINE, echo=None, **over) -> tuple[RunDeps, list[str], list[dict]]:
    lines: list[str] = []
    recorded: list[dict] = []
    exec_deps = chair_exec.Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda action: "run-1",
        repo_for=lambda action: "r",
        launch_stagger_s=0.0,
    )
    deps = RunDeps(
        facts_deps=object(),
        exec_deps=exec_deps,
        report_deps=chair_report.Deps(echo=echo or lines.append, notify=None),
        beat=lambda: None,
        current_epoch=lambda: 3,
        holds=lambda: False,
        release=lambda: None,
        sleep=lambda _s: None,
        now=lambda: NOW,
        gather=lambda _d, _n: _facts(lease),
        plan=lambda _facts, _now: list(actions),
        **over,
    )
    return deps, lines, recorded


def _reasons(lines: list[str]) -> list[str]:
    return [line for line in lines if " not launched " in line]


def test_two_unlaunched_actions_echo_two_reason_lines_then_the_status_line() -> None:
    deps, lines, _ = _deps(TWO)
    _attempt(deps, False)
    assert lines[0].startswith("timing:")
    assert lines[1:3] == REASONS
    assert len(lines) == 4
    assert lines[-1].startswith("chair ")


def test_a_tick_with_none_echoes_no_reason_line() -> None:
    deps, lines, _ = _deps([])
    _attempt(deps, False)
    assert _reasons(lines) == []
    assert len(lines) == 2
    assert lines[-1].startswith("chair ")


def test_the_last_echoed_line_is_the_status_line_with_and_without_reasons() -> None:
    for actions in (TWO, []):
        deps, lines, _ = _deps(actions)
        _attempt(deps, False)
        assert " not launched " not in lines[-1]
        assert not lines[-1].startswith("timing:")
        assert lines[-1].startswith("chair ")


def test_a_forced_dry_run_shows_the_same_reason_lines_as_a_live_tick_and_records_nothing() -> None:
    live, live_lines, _ = _deps(TWO, MINE)
    dry, dry_lines, dry_recorded = _deps(TWO, FOREIGN, force_holder=True)
    _attempt(live, False)
    _attempt(dry, True)
    assert _reasons(dry_lines) == _reasons(live_lines) == REASONS
    assert dry_lines[-1].startswith("chair ")
    assert dry_recorded == []


def test_a_failing_echo_of_a_reason_line_does_not_stop_the_status_line() -> None:
    seen: list[str] = []

    def echo(line: str) -> None:
        if " not launched " in line:
            raise OSError("closed")
        seen.append(line)

    deps, _, _ = _deps(TWO, echo=echo)
    _attempt(deps, False)
    assert _reasons(seen) == []
    assert seen[-1].startswith("chair ")


def test_reason_lines_are_cleared_when_the_next_tick_plans_none() -> None:
    plans = [TWO, []]
    deps, lines, _ = _deps([])
    deps = replace(deps, plan=lambda _f, _n: list(plans.pop(0)))
    _attempt(deps, False)
    _attempt(deps, False)
    assert _reasons(lines) == REASONS
