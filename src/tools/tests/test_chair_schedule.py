import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools.chair_schedule import (
    MIN_REMAINING_SECONDS,
    SCHEDULE_FILE,
    Host,
    Running,
    Tie,
    plan_schedule,
    remaining_seconds,
    schedule_feed,
    schedule_updated_action,
)
from agent_tools.chair_schedule_durations import Estimate
from agent_tools.chair_schedule_graph import Initiative, Phase, build_graph

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_schedule_v1.json").read_text())
HOUR = 3600.0
AT = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


def _ph(id: str, status: str = "ready", parents: tuple[str, ...] = (), surfaces: tuple[str, ...] = ()) -> Phase:
    return Phase(id, status, parents, "S", surfaces)


def _plan(initiatives, hours, hosts, load=None, running=None, target=None, now=NOW, mode="building"):
    nodes, edges = build_graph(initiatives)
    durations = {k: Estimate(h * HOUR, "default", 0) for k, h in hours.items()}
    return plan_schedule(nodes, edges, durations, hosts, mode, load or {}, now, running, target)


def _why(schedule):
    return {e.key: e for e in schedule.explanations}


def _by_key(schedule):
    return {p.key: p for lane in schedule.lanes for p in lane.next}


TWO_HOSTS = (Host("host-a", ("lane-1",)), Host("host-b", ("lane-1",)))
# a1 and b1 hold src/shared.py strictly, so a1 (the higher priority) goes before b1; c1 is independent.
WAVE = [
    Initiative("a", 3, "r", (_ph("a1", surfaces=("src/shared.py",)), _ph("a2", parents=("a1",)))),
    Initiative("b", 2, "r", (_ph("b1", surfaces=("src/shared.py",)),)),
    Initiative("c", 1, "r", (_ph("c1", surfaces=("src/other.py",)),)),
]
WAVE_HOURS = {("a", "a1"): 2, ("a", "a2"): 1, ("b", "b1"): 3, ("c", "c1"): 4}


def test_the_critical_path_phase_launches_first():
    schedule = _plan(WAVE, WAVE_HOURS, TWO_HOSTS)
    assert schedule.critical_path == (("a", "a1"), ("b", "b1"))
    assert schedule.launches[0].key == ("a", "a1")
    assert schedule.launches[0].critical


def test_two_independent_phases_of_one_wave_land_on_different_hosts():
    schedule = _plan(WAVE, WAVE_HOURS, TWO_HOSTS)
    assert [(p.key, p.host, p.start) for p in schedule.launches] == [
        (("a", "a1"), "host-a", NOW),
        (("c", "c1"), "host-b", NOW),
    ]


def test_the_shared_file_conflict_orders_the_two_phases():
    placed = _by_key(_plan(WAVE, WAVE_HOURS, TWO_HOSTS))
    assert placed[("b", "b1")].start == placed[("a", "a1")].finish == NOW + timedelta(hours=2)


def test_priority_breaks_an_otherwise_equal_tie():
    low_first = [Initiative("low", 1, "r1", (_ph("p"),)), Initiative("high", 5, "r2", (_ph("p"),))]
    hours = {("low", "p"): 2, ("high", "p"): 2}
    placed = _by_key(_plan(low_first, hours, (Host("host-a", ("lane-1",)),)))
    assert placed[("high", "p")].start == NOW
    assert placed[("low", "p")].start == NOW + timedelta(hours=2)


def test_equal_priority_falls_to_queue_order_and_is_reported_as_a_tie():
    same = [Initiative("x", 1, "r1", (_ph("p"),)), Initiative("y", 1, "r2", (_ph("p"),))]
    schedule = _plan(same, {("x", "p"): 2, ("y", "p"): 2}, (Host("host-a", ("lane-1",)),))
    assert [p.key for p in schedule.launches] == [("x", "p")]
    assert schedule.ties == (Tie((("x", "p"), ("y", "p"))),)
    assert _plan(WAVE, WAVE_HOURS, TWO_HOSTS).ties == ()


def test_remaining_seconds_is_the_minimum_once_overdue():
    assert remaining_seconds(NOW + timedelta(seconds=120), NOW) == 120.0
    assert remaining_seconds(NOW - timedelta(hours=1), NOW) == MIN_REMAINING_SECONDS
    assert remaining_seconds(NOW, NOW) == MIN_REMAINING_SECONDS
    assert remaining_seconds(None, NOW) == MIN_REMAINING_SECONDS


def test_an_overdue_running_parent_keeps_its_lane_and_its_child():
    initiatives = [
        Initiative("a", 1, "r1", (_ph("a1", "running"), _ph("a2", parents=("a1",)))),
        Initiative("c", 2, "r2", (_ph("c1"),)),
    ]
    overdue = {("a", "a1"): Running("host-a", "lane-1", NOW - timedelta(hours=1))}
    schedule = _plan(initiatives, {("a", "a2"): 1, ("c", "c1"): 1}, TWO_HOSTS, load={"host-a": 1}, running=overdue)
    placed = _by_key(schedule)
    held_until = NOW + timedelta(seconds=MIN_REMAINING_SECONDS)
    # The idle host takes the independent phase; neither the child nor the overdue parent's lane is launched.
    assert [(p.key, p.host) for p in schedule.launches] == [(("c", "c1"), "host-b")]
    assert ("host-a", "lane-1") not in {(p.host, p.lane) for p in schedule.launches}
    assert placed[("a", "a2")].start == held_until
    assert placed[("a", "a2")].finish == held_until + timedelta(hours=1)


def test_an_idle_host_takes_independent_work_instead_of_a_far_future_child():
    initiatives = [
        Initiative("x", 1, "r1", (_ph("x1"), _ph("x2", parents=("x1",)))),
        Initiative("y", 1, "r2", (_ph("y1"),)),
    ]
    schedule = _plan(initiatives, {("x", "x1"): 10, ("x", "x2"): 20, ("y", "y1"): 1}, TWO_HOSTS)
    placed = _by_key(schedule)
    assert [(p.key, p.host) for p in schedule.launches] == [(("x", "x1"), "host-a"), (("y", "y1"), "host-b")]
    assert placed[("x", "x2")].start == NOW + timedelta(hours=10)
    assert _why(schedule)[("y", "y1")].binding == "now"


def test_an_overdue_running_node_alone_on_its_host_blocks_every_launch():
    initiatives = [
        Initiative("a", 1, "r1", (_ph("a1", "running"),)),
        Initiative("c", 2, "r2", (_ph("c1"),)),
    ]
    overdue = {("a", "a1"): Running("host-a", "lane-1", NOW - timedelta(hours=1))}
    schedule = _plan(initiatives, {("c", "c1"): 1}, (Host("host-a", ("lane-1",)),), running=overdue)
    assert schedule.launches == ()
    assert _by_key(schedule)[("c", "c1")].start == NOW + timedelta(seconds=MIN_REMAINING_SECONDS)


def test_explanations_name_the_pick_order_host_load_and_what_set_the_start():
    why = _why(_plan(WAVE, WAVE_HOURS, TWO_HOSTS))
    got = {k: (e.order, e.host, e.host_load, e.binding) for k, e in why.items()}
    assert got == {
        ("a", "a1"): (0, "host-a", 0, "now"),
        ("c", "c1"): (1, "host-b", 0, "now"),
        ("b", "b1"): (2, "host-a", 1, "parent"),
        ("a", "a2"): (3, "host-b", 1, "lane"),
    }
    assert why[("a", "a1")].value == 5 * HOUR


def test_a_running_node_is_explained_as_running_on_its_lane():
    initiatives = [Initiative("a", 1, "r1", (_ph("a1", "running"),))]
    running = {("a", "a1"): Running("host-a", "lane-1", NOW + timedelta(hours=1))}
    why = _why(_plan(initiatives, {}, TWO_HOSTS, running=running))[("a", "a1")]
    assert (why.order, why.host, why.lane, why.binding) == (None, "host-a", "lane-1", "running")


def test_no_capable_host_leaves_the_node_and_what_waits_on_it_unplaced():
    initiatives = [
        Initiative("x", 1, "r-other", (_ph("x1"), _ph("x2", parents=("x1",)))),
        Initiative("y", 1, "r-here", (_ph("y1"),)),
    ]
    hosts = (Host("host-a", ("lane-1",), repos=("r-here",)),)
    schedule = _plan(initiatives, {("x", "x1"): 1, ("x", "x2"): 1, ("y", "y1"): 1}, hosts)
    assert schedule.unplaced == (("x", "x1"), ("x", "x2"))
    assert [p.key for p in schedule.launches] == [("y", "y1")]
    assert {k: e.binding for k, e in _why(schedule).items() if e.order is None} == {
        ("x", "x1"): "unplaced",
        ("x", "x2"): "unplaced",
    }


def test_orchestrating_mode_keeps_work_off_local_hosts():
    initiatives = [Initiative("a", 1, "r1", (_ph("a1"),))]
    hosts = (Host("chair", ("lane-1",), local=True), Host("host-b", ("lane-1",)))
    assert _plan(initiatives, {("a", "a1"): 1}, hosts, mode="orchestrating").launches[0].host == "host-b"
    assert _plan(initiatives, {("a", "a1"): 1}, hosts, mode="building").launches[0].host == "chair"


def test_a_dependency_cycle_returns_an_empty_schedule_naming_the_cycle():
    initiatives = [Initiative("a", 1, "r1", (_ph("p1", "waiting", ("p2",)), _ph("p2", "waiting", ("p1",))))]
    schedule = _plan(initiatives, {("a", "p1"): 1, ("a", "p2"): 1}, TWO_HOSTS)
    assert schedule.cycle == (("a", "p1"), ("a", "p2"))
    assert (schedule.launches, schedule.lanes, schedule.release_finish) == ((), (), None)


def test_a_phase_with_no_estimate_is_not_projected_and_does_not_launch_its_child():
    initiatives = [Initiative("a", 1, "r1", (_ph("p1"), _ph("p2", parents=("p1",))))]
    schedule = _plan(initiatives, {("a", "p2"): 1}, TWO_HOSTS)
    assert schedule.launches == ()
    assert schedule.unplaced == (("a", "p1"), ("a", "p2"))
    assert {k: e.binding for k, e in _why(schedule).items()} == {("a", "p1"): "no_estimate", ("a", "p2"): "unplaced"}


def test_a_blocked_phase_is_not_projected_and_holds_back_what_waits_on_it():
    initiatives = [
        Initiative("a", 1, "r1", (_ph("a1", "blocked"), _ph("a2", "waiting", ("a1",)))),
        Initiative("c", 2, "r2", (_ph("c1"),)),
    ]
    hours = {("a", "a1"): 1, ("a", "a2"): 1, ("c", "c1"): 1}
    schedule = _plan(initiatives, hours, TWO_HOSTS, target=NOW + timedelta(hours=2))
    assert [p.key for p in schedule.launches] == [("c", "c1")]
    assert list(_by_key(schedule)) == [("c", "c1")]
    assert schedule.unplaced == (("a", "a1"), ("a", "a2"))
    assert schedule.release_finish == NOW + timedelta(hours=1)
    assert _why(schedule)[("a", "a1")].binding == "blocked"
    # A partial projection never claims the release is on time, but it can still prove a miss.
    assert schedule.misses_target is None
    assert _plan(initiatives, hours, TWO_HOSTS, target=NOW + timedelta(minutes=30)).misses_target is True


ONE_LANE = (Host("host-a", ("lane-1",)),)


def test_a_waiting_node_takes_no_lane_and_a_ready_node_starts_at_now():
    initiatives = [
        Initiative("w", 2, "r1", (_ph("w1", "waiting"),)),
        Initiative("r", 1, "r1", (_ph("r1"),)),
    ]
    schedule = _plan(initiatives, {("w", "w1"): 1, ("r", "r1"): 1}, ONE_LANE)
    assert [(p.key, p.lane, p.start) for p in schedule.launches] == [(("r", "r1"), "lane-1", NOW)]
    assert list(_by_key(schedule)) == [("r", "r1")]
    assert _why(schedule)[("w", "w1")].binding == "waiting"
    assert schedule.unplaced == (("w", "w1"),)


def test_a_ready_child_of_a_waiting_parent_is_unplaced_too():
    initiatives = [Initiative("a", 1, "r1", (_ph("a1", "waiting"), _ph("a2", parents=("a1",))))]
    schedule = _plan(initiatives, {("a", "a1"): 1, ("a", "a2"): 1}, ONE_LANE)
    assert (schedule.launches, schedule.lanes) == ((), ())
    assert schedule.unplaced == (("a", "a1"), ("a", "a2"))
    assert _why(schedule)[("a", "a1")].binding == "waiting"
    assert _why(schedule)[("a", "a2")].binding == "unplaced"


def test_a_waiting_node_with_no_dependents_leaves_its_lane_free_at_now():
    nodes, _ = build_graph(
        [
            Initiative("w", 2, "r1", (_ph("w1", "waiting"),)),
            Initiative("r", 1, "r1", (_ph("r1"),)),
        ]
    )
    dependents = {k: frozenset() for k in nodes}
    durations = {k: Estimate(HOUR, "default", 0) for k in nodes}
    schedule = plan_schedule(nodes, dependents, durations, ONE_LANE, "building", {}, NOW)
    assert [(p.key, p.start) for p in schedule.launches] == [(("r", "r1"), NOW)]
    assert schedule.unplaced == (("w", "w1"),)


def test_three_equal_nodes_are_reported_as_one_tie_in_queue_order():
    same = [Initiative(n, 1, f"r-{n}", (_ph("p"),)) for n in "xyz"]
    schedule = _plan(same, {(n, "p"): 2 for n in "xyz"}, (Host("host-a", ("lane-1",)),))
    assert schedule.ties == (Tie((("x", "p"), ("y", "p"), ("z", "p"))),)


FEED_INITIATIVES = [
    Initiative(
        "plan-an-initiative-as-waves-of-phases", 1, "r1", (_ph("p1-schema"), _ph("p2-feed", parents=("p1-schema",)))
    ),
    Initiative("scheduler-plans-the-whole-queue-as-one-graph", 2, "r2", (_ph("p1-core"),)),
    Initiative(
        "refresh-the-install-docs", 3, "r3", (_ph("p0-prep", "running"), _ph("p1-docs", parents=("p0-prep",)))
    ),
]
FEED_HOURS = {
    ("plan-an-initiative-as-waves-of-phases", "p1-schema"): 2,
    ("plan-an-initiative-as-waves-of-phases", "p2-feed"): 3,
    ("scheduler-plans-the-whole-queue-as-one-graph", "p1-core"): 3.5,
    ("refresh-the-install-docs", "p1-docs"): 1,
}
FEED_HOSTS = (Host("host-a", ("wave-run-1",)), Host("host-b", ("wave-run-2", "wave-run-3")))


def _feed_schedule():
    prep = {("refresh-the-install-docs", "p0-prep"): Running("host-b", "wave-run-3", NOW + timedelta(minutes=30))}
    target = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    return _plan(FEED_INITIATIVES, FEED_HOURS, FEED_HOSTS, running=prep, target=target)


def test_the_feed_form_equals_the_committed_fixture_key_for_key():
    assert schedule_feed(_feed_schedule(), AT) == FIXTURE


def test_the_action_form_equals_the_fixtures_action():
    assert schedule_updated_action(_feed_schedule(), AT) == FIXTURE["action"]


def test_a_null_target_gives_a_null_misses_target_and_no_advice():
    body = schedule_feed(_plan(WAVE, WAVE_HOURS, TWO_HOSTS), AT)["schedule"]
    assert body["release"]["target"] is None
    assert body["release"]["misses_target"] is None
    assert body["advice"] == []


def test_the_schedule_file_name_is_schedule_json():
    assert SCHEDULE_FILE == "schedule.json"


def test_equal_arguments_give_an_equal_schedule():
    assert _plan(WAVE, WAVE_HOURS, TWO_HOSTS) == _plan(WAVE, WAVE_HOURS, TWO_HOSTS)


def test_a_ready_phase_behind_a_waiting_parent_does_not_hold_back_an_overlapping_initiative():
    # x/p2 cannot place until x/p1 lands, so y, which shares its surface, launches now instead of waiting for good.
    initiatives = [
        Initiative("x", 5, "r", (_ph("p1", "waiting"), _ph("p2", parents=("p1",), surfaces=("src/shared.py",)))),
        Initiative("y", 1, "r", (_ph("q", surfaces=("src/shared.py",)),)),
    ]
    schedule = _plan(initiatives, {("x", "p1"): 1, ("x", "p2"): 1, ("y", "q"): 1}, TWO_HOSTS)
    assert [p.key for p in schedule.launches] == [("y", "q")]
    assert schedule.unplaced == (("x", "p1"), ("x", "p2"))


def test_an_overlapping_initiative_stays_behind_a_placeable_phase():
    initiatives = [
        Initiative("x", 5, "r", (_ph("p1"), _ph("p2", parents=("p1",), surfaces=("src/shared.py",)))),
        Initiative("y", 1, "r", (_ph("q", surfaces=("src/shared.py",)),)),
    ]
    schedule = _plan(initiatives, {("x", "p1"): 1, ("x", "p2"): 1, ("y", "q"): 1}, TWO_HOSTS)
    placed = _by_key(schedule)
    assert [p.key for p in schedule.launches] == [("x", "p1")]
    assert placed[("y", "q")].start == placed[("x", "p2")].finish
