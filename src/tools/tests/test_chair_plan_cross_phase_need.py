"""A phase-two task needing an approved phase-one task of its initiative, planned end to end under each land_at."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools import cli, pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import FactsDeps, docket_by_land_at, gather_facts, idle_stall_inputs, land_at_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_read_idle_backlog import read_idle_backlog

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)
ITEMS = [
    {"id": "a", "initiative": "i", "phase": "p1", "state": "approved", "needs": []},
    {"id": "b", "initiative": "i", "phase": "p2", "state": "ready", "needs": ["a"]},
]
APPROVED = [
    {"id": "a", "initiative": "i", "repo": "r", "phase": "p1", "phase_done": True, "needs": [], "run": "i-1", "needs_fetch": False}
]
LAUNCHES = {"launch_epic", "relaunch"}
LANDS = {"land", "land_phase"}


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def _planned(land_at: str, tmp_path: Path) -> tuple[list[dict], dict]:
    """The actions `plan_tick` gives facts gathered the way the chair wires them, and the idle backlog under them."""
    mapping = land_at_facts({}, {"i": land_at}, ["i"])
    docket = docket_by_land_at(ITEMS, 0, 2, mapping)
    progress = [{"kind": "launch_epic", "ts": (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
    backlog = read_idle_backlog(
        tmp_path, "files", 2, NOW, actions=progress, queued=[], items=ITEMS, live=lambda probe: [], land_at=mapping
    )
    deps = FactsDeps(
        lease=lambda: {"holder": lease_holder("s", 7, "h"), "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(10.0, 168),
        policy=lambda: POLICY,
        docket=lambda: docket,
        approved=lambda: APPROVED,
        quarantined=lambda: [],
        stranded=lambda: [],
        attempts=lambda: [],
        has_patch=lambda initiative, task: False,
        live_initiatives=lambda: [],
        intake=lambda: [],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
        tickets=lambda: ITEMS,
        phase_tasks=lambda: [{"initiative": "i", "phase": "p1", "tasks": [("a", "approved")]}],
        land_at_values=lambda: {"i": land_at},
        run_exited=lambda: {"i": True},
        idle_stall=lambda n: idle_stall_inputs(backlog, [], [], (None, None), 15),
    )
    return plan_tick(gather_facts(deps, NOW), NOW), backlog


def _kinds(actions: list[dict], kinds: set[str]) -> list[dict]:
    return [a for a in actions if a["kind"] in kinds]


@pytest.mark.parametrize("land_at", ["ticket", "level"])
def test_an_approved_need_counts_met_so_phase_two_launches_without_a_wait(land_at, tmp_path):
    actions, backlog = _planned(land_at, tmp_path)
    assert [(a["kind"], a["initiative"]) for a in _kinds(actions, LAUNCHES)] == [("relaunch", "i")]
    assert _kinds(actions, {"waiting", "needs_chair"}) == []
    assert backlog["blocked_ready"] == []


def test_land_at_ticket_plans_no_land_for_phase_one_while_the_epic_is_incomplete(tmp_path):
    actions, _ = _planned("ticket", tmp_path)
    assert _kinds(actions, LANDS) == []


def test_land_at_task_lands_phase_one_and_holds_phase_two(tmp_path):
    actions, backlog = _planned("task", tmp_path)
    assert [a["kind"] for a in actions][:1] == ["land_phase"]
    assert _kinds(actions, LAUNCHES) == []
    assert backlog["blocked_ready"] == [{"task": "b", "unlanded_needs": ["a"]}]


@pytest.mark.parametrize(("land_at", "lines"), [("ticket", []), ("level", []), ("task", ["b waits on a (approved)"])])
def test_the_launch_path_reports_a_wait_on_an_approved_need_only_at_land_at_task(land_at, lines, tmp_path):
    assert cli._waiting_lines(tmp_path / "runs", "i", ITEMS, land_at) == lines
