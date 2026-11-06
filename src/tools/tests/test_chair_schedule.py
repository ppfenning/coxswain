import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import chair_schedule
from agent_tools.chair_schedule import (
    SCHEDULE_FILE,
    HostSlot,
    InitiativeRow,
    LaneRow,
    Placement,
    Release,
    Running,
    Schedule,
    schedule,
    schedule_updated_action,
    to_feed,
)
from agent_tools.chair_schedule_graph import Initiative, Phase, build_graph
from agent_tools.chair_schedule_path import critical_paths

FIXTURE = Path(__file__).parent / "fixtures" / "dash_feed_schedule_v1.json"
NOW = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
HOUR = 3600.0
TWO_HOSTS = [HostSlot("host-a", ("l1",)), HostSlot("host-b", ("l1",))]


def _ini(id: str, priority: int, *phases: Phase) -> Initiative:
    return Initiative(id, priority, "r", phases)


def _ph(id: str, status: str = "ready", parents: tuple[str, ...] = (), surfaces: tuple[str, ...] = ()) -> Phase:
    return Phase(id, status, parents, "S", surfaces)


def _plan(initiatives, hosts=TWO_HOSTS, mode="building", load=None, running=None, target=None, hours=1.0):
    nodes, dependents = build_graph(initiatives)
    durations = dict.fromkeys(nodes, hours * HOUR)
    paths = critical_paths(durations, dependents)
    return schedule(
        nodes, dependents, durations, paths.values, paths.chain, hosts, mode, load or {}, NOW, running or {}, target
    )


def _by_key(plan: Schedule) -> dict:
    return {p.key: p for row in plan.lanes for p in row.next}


def _three_initiatives():
    return [
        _ini("a", 3, _ph("a1"), _ph("a2", "waiting", ("a1",)), _ph("a3", "waiting", ("a2",))),
        _ini("b", 4, _ph("b1", surfaces=("src/x.py",)), _ph("b2", "waiting", ("b1",))),
        _ini("c", 1, _ph("c1", surfaces=("src/x.py",))),
    ]


def test_critical_path_phase_launches_first():
    plan = _plan(_three_initiatives())
    assert plan.launches[0].key == ("a", "a1")
    assert plan.critical_path == (("a", "a1"), ("a", "a2"), ("a", "a3"))


def test_independent_phases_of_one_wave_land_on_different_hosts():
    placed = _by_key(_plan(_three_initiatives()))
    assert placed[("a", "a1")].host != placed[("b", "b1")].host


def test_shared_file_conflict_orders_the_two_phases():
    placed = _by_key(_plan(_three_initiatives()))
    assert placed[("c", "c1")].start >= placed[("b", "b1")].finish
    assert ("c", "c1") not in {p.key for p in _plan(_three_initiatives()).launches}


def test_priority_breaks_an_otherwise_equal_tie():
    plan = _plan([_ini("low", 1, _ph("x")), _ini("high", 5, _ph("y"))], hosts=[HostSlot("host-a", ("l1",))])
    assert [p.key for p in plan.launches] == [("high", "y")]
    assert plan.ties == ()


def test_equal_weights_and_priority_are_listed_as_a_tie_broken_by_queue_order():
    plan = _plan([_ini("one", 1, _ph("x")), _ini("two", 1, _ph("y"))])
    assert plan.ties == ((("one", "x"), ("two", "y")),)
    assert plan.launches[0].key == ("one", "x")


def test_overdue_running_parent_holds_its_lane_and_its_child():
    inis = [_ini("p", 2, _ph("p1", "running"), _ph("p2", "waiting", ("p1",))), _ini("q", 1, _ph("q1"))]
    overdue = {("p", "p1"): Running("host-a", "l1", NOW - timedelta(hours=1))}
    plan = _plan(inis, running=overdue)
    placed = _by_key(plan)
    assert [p.key for p in plan.launches] == [("q", "q1")]
    assert all((p.host, p.lane) != ("host-a", "l1") for p in plan.launches)
    assert placed[("p", "p1")].finish == NOW + timedelta(seconds=chair_schedule.MIN_REMAINING_SECONDS)
    assert placed[("p", "p2")].start == placed[("p", "p1")].finish


def test_orchestrating_mode_keeps_the_chair_host_idle():
    hosts = [HostSlot("chair", ("l1",), chair=True), HostSlot("host-b", ("l1",))]
    plan = _plan([_ini("a", 1, _ph("a1"), _ph("a2"))], hosts=hosts, mode="orchestrating")
    assert {p.host for p in _by_key(plan).values()} == {"host-b"}


def test_live_load_steers_a_node_to_the_quieter_host():
    plan = _plan([_ini("a", 1, _ph("a1"))], load={"host-a": 2, "host-b": 0})
    assert plan.launches[0].host == "host-b"


def test_node_without_a_capable_host_is_unplaced():
    hosts = [HostSlot("host-a", ("l1",), frozenset({"other"}))]
    assert _plan([_ini("a", 1, _ph("a1"))], hosts=hosts).unplaced == (("a", "a1"),)


def test_release_finish_and_misses_target():
    inis = [_ini("a", 1, _ph("a1"), _ph("a2", "waiting", ("a1",)))]
    assert _plan(inis).release == Release(None, NOW + timedelta(hours=2), None)
    assert _plan(inis, target=NOW + timedelta(hours=1)).release.misses_target is True
    assert _plan(inis, target=NOW + timedelta(hours=3)).release.misses_target is False


def _t(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 6, hour, minute, tzinfo=UTC)


def _fixture_schedule() -> Schedule:
    wave, sched, docs = (
        "plan-an-initiative-as-waves-of-phases",
        "scheduler-plans-the-whole-queue-as-one-graph",
        "refresh-the-install-docs",
    )
    p1 = Placement((wave, "p1-schema"), "host-a", "wave-run-1", _t(8), _t(10), True)
    p2 = Placement((wave, "p2-feed"), "host-a", "wave-run-1", _t(10), _t(13), True)
    core = Placement((sched, "p1-core"), "host-b", "wave-run-2", _t(8), _t(11, 30), False)
    page = Placement((docs, "p1-docs"), "host-b", "wave-run-3", _t(8, 30), _t(9, 30), False)
    return Schedule(
        launches=(),
        lanes=(
            LaneRow("host-a", "wave-run-1", (p1, p2)),
            LaneRow("host-b", "wave-run-2", (core,)),
            LaneRow("host-b", "wave-run-3", (page,)),
        ),
        initiatives=(
            InitiativeRow(wave, 1, _t(13), True),
            InitiativeRow(sched, 2, _t(11, 30), False),
            InitiativeRow(docs, 3, _t(9, 30), False),
        ),
        release=Release(_t(12), _t(13), True),
        critical_path=((wave, "p1-schema"), (wave, "p2-feed")),
        ties=(),
        explain=(),
    )


def test_feed_form_equals_the_committed_fixture_key_for_key():
    fixture = json.loads(FIXTURE.read_text())
    advice = fixture["schedule"]["advice"]
    feed = to_feed(_fixture_schedule(), _t(0), advice)
    assert feed == fixture
    assert feed["action"] == schedule_updated_action(feed["schedule"])


def test_schedule_file_name():
    assert SCHEDULE_FILE == "schedule.json"


def test_module_reads_no_clock_file_or_process():
    source = Path(chair_schedule.__file__).read_text()
    assert not re.search(
        r"datetime\.(now|utcnow|today)|\btime\.|\bopen\(|\bPath\b|subprocess|os\.environ|\bimport os\b", source
    )
