from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agent_tools import cli, route_schedule
from agent_tools.chair_schedule import Explanation, InitiativePlan, LanePlan, Placement, Schedule, Tie
from agent_tools.chair_schedule_durations import Estimate
from agent_tools.chair_schedule_graph import Node

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
A, B = ("alpha", "build"), ("beta", "build")
END_A, END_B = T0 + timedelta(hours=2), T0 + timedelta(hours=3)
PA, PB = Placement(A, "h", "h-1", T0, END_A, True), Placement(B, "h", "h-1", END_A, END_B, True)
SCHEDULE = Schedule(
    launches=(PA,),
    lanes=(LanePlan("h", "h-1", (PA, PB)),),
    initiatives=(InitiativePlan("alpha", 5, END_A, True), InitiativePlan("beta", 1, END_B, True)),
    release_finish=END_B,
    target=END_B + timedelta(hours=1),
    misses_target=False,
    critical_path=(A, B),
    ties=(Tie((A, B)),),
    explanations=(
        Explanation(A, 10800.0, 0, "h", "h-1", 0, "now"),
        Explanation(B, 3600.0, 1, "h", "h-1", 0, "parent"),
    ),
)
CONTEXT = route_schedule.Context(
    nodes={
        A: Node("alpha", "build", "ready", "S", ("src/a.py",), 5, "r"),
        B: Node("beta", "build", "waiting", "S", ("src/a.py",), 1, "r"),
    },
    dependents={A: frozenset({B}), B: frozenset()},
    durations={A: Estimate(7200.0, "size_mix", 4), B: Estimate(3600.0, "size", 2)},
)
INPUTS = {
    "initiatives": [
        {
            "id": "alpha",
            "priority": 0,
            "repo": "r",
            "phases": [{"id": "build", "status": "ready", "parents": [], "size": "S", "mix": "plain", "surfaces": []}],
        }
    ],
    "records": [],
    "hosts": [{"name": "h", "lanes": ["h-1"], "local": True, "repos": []}],
    "mode": "building",
    "load": {},
    "target": None,
}


def test_plain_output_lists_lanes_initiatives_release_and_path():
    lines = route_schedule.format_plain(SCHEDULE)
    assert (
        "h/h-1: alpha/build 2026-11-09 12:00 to 2026-11-09 14:00; beta/build 2026-11-09 14:00 to 2026-11-09 15:00"
        in lines
    )
    assert "beta: finishes 2026-11-09 15:00 (priority 1) on the critical path" in lines
    assert "release: 2026-11-09 15:00, target 2026-11-09 16:00 meets target" in lines
    assert "critical path: alpha/build -> beta/build" in lines


def test_plain_output_prints_recorded_advice():
    advice = [{"kind": "tie", "text": "alpha first", "at": "t"}]
    assert "advice (tie, t): alpha first" in route_schedule.format_plain(SCHEDULE, advice)


def test_explain_names_wait_file_priority_tier_and_tie():
    lines = route_schedule.explain_lines(SCHEDULE, CONTEXT, "beta")
    assert "beta: priority 1" in lines
    assert "beta/build: critical path value 1.0h, pick 1 on h/h-1, bound by parent" in lines
    assert "  waits behind alpha/build on src/a.py" in lines
    assert "  duration 1.0h from tier size (2 records)" in lines
    assert "tie: alpha/build, beta/build" in lines


def test_explain_of_the_leader_has_no_wait_line():
    assert not [x for x in route_schedule.explain_lines(SCHEDULE, CONTEXT, "alpha") if "waits behind" in x]


def test_explain_unknown_initiative_is_none():
    assert route_schedule.explain_lines(SCHEDULE, CONTEXT, "ghost") is None


def test_should_jump_with_asker_prints_only_the_advice():
    prompts: list[str] = []

    def asker(prompt: str) -> str:
        prompts.append(prompt)
        return "yes, beta should jump"

    lines = route_schedule.should_jump_lines(SCHEDULE, CONTEXT, "beta", asker, "t")
    assert lines == ["yes, beta should jump"]
    assert len(prompts) == 1  # the tie request is also built, but only the reorder prompt reaches the asker
    assert "Should beta jump the queue?" in prompts[0]


def test_should_jump_without_asker_prints_prompt_and_says_so():
    lines = route_schedule.should_jump_lines(SCHEDULE, CONTEXT, "beta", None, "t")
    assert "Should beta jump the queue?" in lines[0]
    assert lines[1:] == ["no asker ran"]


def test_should_jump_unknown_initiative_is_none():
    assert route_schedule.should_jump_lines(SCHEDULE, CONTEXT, "ghost", None, "t") is None


def test_plan_from_inputs_places_the_ready_phase_with_the_default_tier():
    schedule, context = route_schedule.plan_from_inputs(INPUTS, T0)
    assert [p.key for p in schedule.launches] == [("alpha", "build")]
    assert context.durations[("alpha", "build")].tier == "default"


def _two_initiatives(**extra):
    phase = {"parents": [], "size": "S", "mix": "plain", "surfaces": []}
    return {
        **INPUTS,
        "initiatives": [
            {"id": "alpha", "priority": 0, "repo": "r", "phases": [{"id": "build", "status": "running", **phase}]},
            {"id": "beta", "priority": 0, "repo": "r", "phases": [{"id": "build", "status": "ready", **phase}]},
        ],
        **extra,
    }


def test_a_running_phase_holds_the_only_lane_so_a_ready_phase_waits_for_it():
    held = [{"initiative": "alpha", "phase": "build", "host": "h", "lane": "h-1"}]
    schedule, _ = route_schedule.plan_from_inputs(_two_initiatives(running=held), T0)
    beta = next(p for lane in schedule.lanes for p in lane.next if p.key == B)
    assert B not in [p.key for p in schedule.launches]
    assert beta.start == T0 + timedelta(seconds=900)


def test_without_a_running_key_the_same_ready_phase_launches_this_tick():
    schedule, _ = route_schedule.plan_from_inputs(_two_initiatives(), T0)
    assert B in [p.key for p in schedule.launches]


def _patch_edge(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_profile_or_refuse", lambda a: ({}, None))
    monkeypatch.setattr(cli, "_lake_provider", lambda a: ({}, None))
    monkeypatch.setattr(cli.work_state, "work_state_mode", lambda provider: "files")
    monkeypatch.setattr(cli, "_schedule_inputs", lambda profile, mode, now: INPUTS)


def test_cli_unknown_initiative_is_one_line_and_nonzero(monkeypatch, capsys):
    _patch_edge(monkeypatch)
    code = cli._route_schedule(SimpleNamespace(profile=None, explain="ghost", should_jump=None))
    assert (code, capsys.readouterr().out) == (2, "routing: unknown initiative: ghost\n")


def test_cli_plain_prints_the_critical_path(monkeypatch, capsys):
    _patch_edge(monkeypatch)
    assert cli._route_schedule(SimpleNamespace(profile=None, explain=None, should_jump=None)) == 0
    assert "critical path: alpha/build" in capsys.readouterr().out


CLI = "src/tools/agent_tools/cli.py"
README = "src/tools/README.md"


def _queue(*phases, **extra):
    """Phases are (initiative, id, status, parents, surfaces); the host has two free lanes."""
    ids = list(dict.fromkeys(p[0] for p in phases))
    return {
        **INPUTS,
        "hosts": [{"name": "h", "lanes": ["h-1", "h-2"], "local": True, "repos": []}],
        "initiatives": [
            {
                "id": i,
                "priority": 0,
                "repo": "r",
                "phases": [
                    {"id": pid, "status": status, "parents": parents, "size": "S", "mix": "plain", "surfaces": surfaces}
                    for init, pid, status, parents, surfaces in phases
                    if init == i
                ],
            }
            for i in ids
        ],
        **extra,
    }


def _launched(schedule):
    return [p.key for p in schedule.launches]


def test_a_blocked_later_phase_does_not_order_a_ready_phase_behind_it():
    inputs = _queue(
        ("alpha", "one", "ready", [], []),
        ("alpha", "two", "blocked", ["one"], [CLI]),
        ("beta", "build", "ready", [], [CLI]),
    )
    schedule, _ = route_schedule.plan_from_inputs(inputs, T0)
    assert B in _launched(schedule)


def test_a_waiting_only_initiative_does_not_order_a_ready_phase_behind_it():
    inputs = _queue(("alpha", "build", "waiting", [], [CLI]), ("beta", "build", "ready", [], [CLI]))
    schedule, _ = route_schedule.plan_from_inputs(inputs, T0)
    assert B in _launched(schedule)


def test_a_running_phase_orders_a_ready_phase_behind_it_and_explain_says_so():
    inputs = _queue(
        ("alpha", "build", "running", [], [CLI]),
        ("beta", "build", "ready", [], [CLI]),
        running=[{"initiative": "alpha", "phase": "build", "host": "h", "lane": "h-1"}],
    )
    schedule, context = route_schedule.plan_from_inputs(inputs, T0)
    lines = route_schedule.explain_lines(schedule, context, "beta")
    assert B not in _launched(schedule)
    assert f"  waits behind alpha/build on {CLI}" in lines


def test_a_withheld_initiative_does_not_order_work_behind_it():
    inputs = _queue(("alpha", "build", "ready", [], [CLI]), ("beta", "build", "ready", [], [CLI]))
    withheld, _ = route_schedule.plan_from_inputs(inputs, T0, withheld=frozenset({"alpha"}))
    assert B in _launched(withheld)


def test_report_shaped_queue_prints_no_waits_line_behind_a_blocked_or_waiting_phase():
    both = [README, CLI]
    inputs = _queue(
        ("one", "gate", "blocked", [], both),
        ("one", "tail", "waiting", [], both),
        ("two", "build", "ready", [], [CLI]),
    )
    schedule, context = route_schedule.plan_from_inputs(inputs, T0)
    two = route_schedule.explain_lines(schedule, context, "two")
    one = route_schedule.explain_lines(schedule, context, "one")
    assert ("two", "build") in _launched(schedule)
    assert [line for line in two if line.startswith("  waits behind")] == []
    assert [line for line in one if line.startswith("one/") and not line.endswith("not placed")] == []
