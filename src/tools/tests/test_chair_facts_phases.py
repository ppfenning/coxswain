from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import chair_read_docket, pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import (
    FactsDeps,
    gather_facts,
    initiative_facts,
    phase_facts,
    read_ticket_items,
    running_initiatives,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)


def _row(task: str, phase: str, state: str, surfaces: list[str], needs: list[str] | None = None, merged: bool = False) -> dict:
    return {
        "initiative": "x", "id": task, "phase": phase, "state": state, "surfaces": surfaces,
        "needs": needs or [], "requires": [], "merged": merged,
    }


TWO_PHASES = [_row("a", "p1", "ready", ["a.rs"]), _row("b", "p2", "ready", ["b.rs", "a2.rs"])]


def test_two_independent_ready_phases_are_listed_with_their_own_surfaces():
    assert phase_facts(TWO_PHASES, "ticket") == [
        {"phase": "p1", "ready_tasks": [{"id": "a", "phase": "p1", "needs": [], "requires": []}], "surfaces": ["a.rs"]},
        {"phase": "p2", "ready_tasks": [{"id": "b", "phase": "p2", "needs": [], "requires": []}], "surfaces": ["a2.rs", "b.rs"]},
    ]


def test_a_phase_behind_an_unmet_phase_is_not_listed():
    rows = [_row("a", "p1", "ready", ["a.rs"]), _row("b", "p2", "ready", ["b.rs"], needs=["a"])]
    assert [p["phase"] for p in phase_facts(rows, "ticket")] == ["p1"]


def _docket() -> dict:
    return {"initiatives": [{"id": "x", "started": False, "ready_tasks": [], "landed": []}]}


def test_initiative_facts_carries_phases_only_when_rows_were_read():
    (with_rows,) = initiative_facts(_docket(), set(), rows={"x": TWO_PHASES})
    assert [p["phase"] for p in with_rows["phases"]] == ["p1", "p2"]
    (without,) = initiative_facts(_docket(), set())
    (other,) = initiative_facts(_docket(), set(), rows={"y": TWO_PHASES})
    assert "phases" not in without
    assert "phases" not in other


def test_a_scalar_requires_reads_as_one_capability_as_the_docket_reads_it():
    rows = [{**_row("a", "p1", "ready", ["a.rs"]), "requires": "gpu"}]
    (phase,) = phase_facts(rows, "ticket")
    assert phase["ready_tasks"][0]["requires"] == ["gpu"]


def test_a_phase_whose_only_ready_task_is_claimed_is_not_listed():
    assert [p["phase"] for p in phase_facts(TWO_PHASES, "ticket", {"a"})] == ["p2"]
    (entry,) = initiative_facts(_docket(), set(), rows={"x": TWO_PHASES}, claimed={("x", "b"), ("y", "a")})
    assert [p["phase"] for p in entry["phases"]] == ["p1"]


def test_a_phase_with_its_own_live_run_is_not_listed_as_launchable():
    (entry,) = initiative_facts(_docket(), set(), rows={"x": TWO_PHASES}, live_phases={("x", "p1"), ("y", "p2")})
    assert [p["phase"] for p in entry["phases"]] == ["p2"]
    (none_left,) = initiative_facts(_docket(), set(), rows={"x": TWO_PHASES}, live_phases={("x", "p1"), ("x", "p2")})
    assert "phases" not in none_left


def test_rows_with_no_phase_leave_the_initiative_without_a_phases_key():
    unphased = [{**_row("a", "", "ready", ["a.rs"]), "phase": None}]
    (entry,) = initiative_facts(_docket(), set(), rows={"x": unphased})
    assert "phases" not in entry


def test_initiative_facts_without_rows_is_unchanged():
    (entry,) = initiative_facts(_docket(), set(), {"x": ["a.rs"]}, {"x": "/r"}, {"x": "ticket"})
    assert set(entry) == {"id", "started", "ready_tasks", "waiting_tasks", "landed", "ready_surfaces", "repo", "repo_dir", "land_at"}


def test_a_live_phase_entry_holds_only_that_phases_in_progress_and_ready_surfaces():
    rows = [
        _row("a", "p1", "in_progress", ["a.rs"]),
        _row("a2", "p1", "ready", ["a2.rs"]),
        _row("a3", "p1", "approved", ["a3.rs"]),
        _row("b", "p2", "ready", ["b.rs"]),
    ]
    assert running_initiatives(set(), rows, {"x": "/r"}, live_phases=[("x", "p1")]) == [
        {"id": "x", "repo": "/r", "surfaces": ["a.rs", "a2.rs"], "phase": "p1", "live": True, "state": "running"}
    ]


def test_an_approved_unlanded_task_of_a_live_phase_stays_held_by_the_whole_initiative_entry():
    rows = [_row("a", "p1", "in_progress", ["a.rs"]), _row("a3", "p1", "approved", ["a3.rs"])]
    got = running_initiatives(set(), rows, {"x": "/r"}, unlanded={"x"}, live_phases=[("x", "p1")])
    assert got == [
        {"id": "x", "repo": "/r", "surfaces": ["a3.rs"], "live": False, "state": "awaiting_land"},
        {"id": "x", "repo": "/r", "surfaces": ["a.rs"], "phase": "p1", "live": True, "state": "running"},
    ]


def test_a_whole_live_initiative_keeps_its_entry_without_phase_beside_a_live_phase_entry():
    rows = [_row("a", "p1", "ready", ["a.rs"]), _row("b", "p2", "ready", ["b.rs"])]
    rows = rows + [{**_row("c", "p1", "ready", ["c.rs"]), "initiative": "y"}]
    got = running_initiatives({"y"}, rows, {"x": "/r", "y": "/s"}, live_phases=[("x", "p2")])
    assert got == [
        {"id": "y", "repo": "/s", "surfaces": ["c.rs"], "live": True, "state": "running"},
        {"id": "x", "repo": "/r", "surfaces": ["b.rs"], "phase": "p2", "live": True, "state": "running"},
    ]


def test_running_initiatives_without_live_phases_is_unchanged():
    rows = [_row("a", "p1", "approved", ["a.rs"]), _row("b", "p2", "ready", ["b.rs"])]
    assert running_initiatives({"x"}, rows, {"x": "/r"}) == [
        {"id": "x", "repo": "/r", "surfaces": ["a.rs", "b.rs"], "live": True, "state": "running"}
    ]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def _deps(tmp_path: Path, **extra) -> FactsDeps:
    return FactsDeps(
        lease=lambda: {"holder": lease_holder("s", 7, "h"), "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(10.0, 168),
        policy=lambda: POLICY,
        docket=lambda: chair_read_docket.read_docket(tmp_path, "files", 2),
        approved=lambda: [],
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
        idle_stall=lambda n: {
            "free_lanes": 0, "ready": 0, "queued": 0, "last_progress_at": None, "stall_minutes": 15, "hosts": [],
            "empty_stubs": [], "lands_waiting": [], "blocked_ready": [], "open_signature": None, "open_diagnosis": None,
        },
        tickets=lambda: read_ticket_items(tmp_path, "files"),
        repos=lambda: {"x": "/r"},
        **extra,
    )


def _tree(tmp_path: Path) -> None:
    _write(tmp_path / "work/x/initiative.md", "---\nid: x\nrepo: /r\n---\n\nbody\n")
    _write(tmp_path / "work/x/p1/a.md", "---\nid: a\nstate: ready\nsurfaces: [src/a.rs]\n---\n\nbody\n")
    _write(tmp_path / "work/x/p2/b.md", "---\nid: b\nstate: ready\nsurfaces: [src/b.rs]\n---\n\nbody\n")


def test_gather_facts_lists_both_independent_phases_with_their_own_surfaces(tmp_path):
    _tree(tmp_path)
    (entry,) = gather_facts(_deps(tmp_path), NOW)["initiatives"]
    assert [(p["phase"], p["surfaces"]) for p in entry["phases"]] == [("p1", ["src/a.rs"]), ("p2", ["src/b.rs"])]


def test_gather_facts_shows_a_live_phase_run_and_lists_only_the_other_phase_as_launchable(tmp_path):
    _tree(tmp_path)
    facts = gather_facts(_deps(tmp_path, live_phases=lambda: [("x", "p1")]), NOW)
    (entry,) = facts["initiatives"]
    assert [(p["phase"], p["surfaces"]) for p in entry["phases"]] == [("p2", ["src/b.rs"])]
    assert facts["live_phases"] == [{"initiative": "x", "phase": "p1"}]
    assert facts["running"] == [{"id": "x", "repo": "/r", "surfaces": ["src/a.rs"], "phase": "p1", "live": True, "state": "running"}]


def test_gather_facts_with_no_phase_rows_gives_initiatives_without_a_phases_key(tmp_path):
    _tree(tmp_path)
    facts = gather_facts(replace(_deps(tmp_path), tickets=lambda: []), NOW)
    assert all("phases" not in entry for entry in facts["initiatives"])


def test_gather_facts_without_live_phases_has_no_live_phases_key_and_no_running_entry(tmp_path):
    _tree(tmp_path)
    facts = gather_facts(_deps(tmp_path), NOW)
    assert "live_phases" not in facts
    assert facts["running"] == []
