"""Guard: free lanes with ready, unheld initiatives always launch, and no unlaunched line remains for a launched one.

Symptom, 2026-11-07 02:20 to 02:51: lanes 0/4, steward off, land_at ticket, eleven initiatives with ready tasks, and the
chair planned no launch after the commit-carry hold cleared.

No cause could be shown in chair_plan.py, chair_plan_fill.py or chair_carry.py, so no source changed and this file is a
guard. Reasoned about from the code, with no test here: `_launch_cap` is 0 only for go_degraded or smoke_hold, and
`_free_lanes` and `_dispatch_room` read max_in_flight minus live_runs. Tested below with literal facts, each beside the
four-lane control that launches four:

- A cleared carry: the fixture plans a carry_phase for i00, and the other ten still fill the four lanes.
- A launch cap of 0 with lanes free: nothing launches, and all eleven carry an unlaunched line.
- Land backoff: a refusal inside its window holds an unstarted initiative as "land backoff", and the lanes still fill.
  A started initiative in the same case relaunches through plan_recover, which does not read backoff_held. That launches
  more, not fewer, so it is no cause of the stall and is left alone.
- A relaunch the launch budget refuses: it holds only its own initiative and takes no lane.
- A steer-held head: it yields its lane and the walk goes on to the tail.
- A land in flight on another repo holds nothing. A land in flight on their repo holds all eleven as "its repo is
  landing". `_landing_initiatives` makes that hold on purpose, and test_chair_plan_withhold.py pins it.

Open lead, not a finding: a stale land row would hold a whole repo for good. `_landing_initiatives` trusts
facts["landing"], which chair_facts.py fills from `deps.landing`, the land worker's `WorkerLands.landing`. That source
lives outside the three planning modules and its rows carry no age, so the planner cannot tell a stale land from a
live one. If the symptom was this hold, the fix is in the land worker's landing rows, and the unlaunched line now names it.
Reasons are matched by the hold they name, not by their full wording.
"""

from datetime import UTC, datetime, timedelta  # timedelta builds the launch history

from agent_tools.chair_plan import plan_tick
from tests.test_chair_plan import _facts

_NOW = datetime(2026, 11, 7, 2, 30, tzinfo=UTC)
_IDS = [f"i{n:02}" for n in range(11)]
_STARTED = 4  # i00..i03 are in flight (started); i04..i10 have never run
_LIMITS = {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "go_degraded": False}
_CARRIED = {
    "id": "t1", "initiative": "i00", "repo": "repo-a", "phase": "p", "phase_done": True, "needs": [], "run": "i00-1",
    "needs_fetch": False,
}


def _ready(id: str, started: bool, repo: str, **extra) -> dict:
    return {
        "id": id, "started": started, "repo": repo, "land_at": "ticket", "landed": set(),
        "ready_tasks": [{"id": f"{id}-t", "needs": []}], **extra,
    }


def _symptom(launch_cap: int = 4, surfaces: dict[str, list[str]] | None = None, approved: list[dict] | None = None, **extra) -> dict:
    return _facts(
        limits={**_LIMITS, "launch_cap": launch_cap},
        dispatch={"max_in_flight": 4, "live_runs": 0, "hosts": []},
        initiatives=[
            _ready(i, n < _STARTED, "repo-a", **({"ready_surfaces": surfaces[i]} if surfaces and i in surfaces else {}))
            for n, i in enumerate(_IDS)
        ],
        run_exited=dict.fromkeys(_IDS, True),
        # The commit-carry hold has just cleared: the phase is stranded with nothing pending, and its carry is planned.
        approved=[_CARRIED, *(approved or [])],
        stranded=[{"initiative": "i00", "phase": "p", "pending": [], "approved": [{"task": "t1", "run_seq": 1, "needs": []}]}],
        **extra,
    )


def _outcome(facts: dict) -> tuple[set[str], dict[str, str], list[str]]:
    actions = plan_tick(facts, _NOW)
    launched = {a["initiative"] for a in actions if a["kind"] in {"launch_epic", "relaunch"}}
    unlaunched = {a["initiative"]: a["reason"] for a in actions if a["kind"] == "unlaunched"}
    return launched, unlaunched, [a["initiative"] for a in actions if a["kind"] == "carry_phase"]


def _four_launch_and_none_overlap(facts: dict, held: str) -> dict[str, str]:
    launched, unlaunched, _ = _outcome(facts)
    assert len(launched) == 4 and held not in launched
    assert not launched & unlaunched.keys()
    assert held in unlaunched
    return unlaunched


def test_four_free_lanes_launch_four_of_eleven_ready_initiatives_after_the_carry_is_planned():
    launched, unlaunched, carried = _outcome(_symptom())
    assert carried == ["i00"]
    assert len(launched) == 4
    assert not launched & unlaunched.keys()
    assert set(unlaunched) == set(_IDS) - launched
    assert len(set(unlaunched.values())) == 1 and all(unlaunched.values())


def test_a_launch_cap_of_zero_with_lanes_free_launches_nothing_and_every_initiative_has_a_reason():
    launched, unlaunched, _ = _outcome(_symptom(launch_cap=0))
    assert launched == set()
    assert set(unlaunched) == set(_IDS) and all(unlaunched.values())


def test_a_land_backoff_holds_its_unstarted_initiative_and_the_four_lanes_still_fill():
    refusal = {"initiative": "i05", "phase": "p", "run": "i05-1", "attempts": 1, "last_refused_at": (_NOW - timedelta(minutes=5)).isoformat()}
    task = {**_CARRIED, "id": "t5", "initiative": "i05", "run": "i05-1", "needs_fetch": True}
    facts = _symptom(approved=[task], land_refusals=[refusal])
    # land_at task: an incomplete initiative-grain epic plans no land, so there would be no backoff to hold i05.
    facts["initiatives"] = [{**i, "land_at": "task"} if i["id"] == "i05" else i for i in facts["initiatives"]]
    unlaunched = _four_launch_and_none_overlap(facts, "i05")
    assert "backoff" in unlaunched["i05"]


def test_a_relaunch_the_budget_refuses_holds_its_own_initiative_and_takes_no_lane():
    recent = [{"at": (_NOW - timedelta(minutes=m)).isoformat(), "kind": "relaunch"} for m in (10, 20)]
    facts = _symptom(launch_history={"i01": {"launches": recent}}, max_launches_per_hour=2)
    unlaunched = _four_launch_and_none_overlap(facts, "i01")
    assert unlaunched["i01"] and "lane" not in unlaunched["i01"]


def test_a_steer_held_head_yields_its_lane_and_the_walk_goes_on_to_the_tail():
    facts = _symptom(surfaces={"i01": ["src/a.rs"]}, running=[{"id": "zz", "repo": "repo-a", "surfaces": ["src/a.rs"]}])
    unlaunched = _four_launch_and_none_overlap(facts, "i01")
    assert "lane" not in unlaunched["i01"]


def test_a_land_in_flight_on_another_repo_does_not_hold_the_ready_initiatives():
    other = [{"initiative": "zz", "phase": "p", "repo": "repo-b"}]
    assert _outcome(_symptom(landing=other)) == _outcome(_symptom())


def test_a_land_of_another_initiative_in_their_repo_holds_none_of_them():
    same = [{"initiative": "zz", "phase": "p", "repo": "repo-a"}]
    assert _outcome(_symptom(landing=same)) == _outcome(_symptom())


def test_a_land_naming_no_initiative_on_their_repo_holds_all_of_them_and_names_the_hold():
    same = [{"initiative": "", "phase": "", "repo": "repo-a"}]
    launched, unlaunched, _ = _outcome(_symptom(landing=same))
    assert launched == set()
    assert set(unlaunched) == set(_IDS)
    assert all("landing" in reason for reason in unlaunched.values())
