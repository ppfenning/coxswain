"""C6: the queue alarms leave the tick. Once no tick holds the chair lease, the first live worker in `QUEUE_KINDS` order
raises `queue_unowned` for every other listed kind with no live worker, so no one dead process silences them, the
housekeeping worker included. The stall alarm and the host checks go with housekeeping. Each worker pushes its own
pass's events; the watcher also pushes the chair-wide ones.

Pure planners over the C1 split's scenarios (`test_chair_plan_kinds`); nothing is stubbed but the loop's ports in the
two loop tests, which are the worker's real seam.
"""

from __future__ import annotations

import datetime as dt
import itertools
from pathlib import PurePosixPath

import pytest

from agent_tools import chair_kinds, chair_queue, chair_worker_notify
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_queue import planned_pass
from agent_tools.chair_types import Action, Facts
from agent_tools.notify_core import Event
from agent_tools.notify_events import QUEUE_EMPTY
from tests.test_chair_plan_kinds import _SCENARIOS

KINDS = chair_kinds.QUEUE_KINDS
_NOW = "2026-11-20T12:00:00Z"
_LIVE = "2026-11-20T12:01:00Z"
_DEAD = "2026-11-20T11:58:00Z"  # expired two minutes ago: past the lease's time to live
_NO_TICK = {"holder": "", "host": "", "epoch": 0, "mine": False, "released": True, "stale": False, "expired": False, "until": ""}
_TICK = {"holder": "chair@m:1", "host": "m", "epoch": 3, "mine": True, "released": False, "stale": False, "expired": False,
         "until": ""}


def _facts(live: frozenset[str], *, tick: bool = False, listed: tuple[str, ...] = KINDS, base: str = "launch fills free lanes") -> Facts:
    facts, _now = _SCENARIOS[base]
    leases = {kind: (_LIVE if kind in live else _DEAD) for kind in KINDS}
    return {**facts, "lease": _TICK if tick else _NO_TICK, "chair_queues": list(listed), "queue_leases": leases,  # type: ignore[return-value]
            "queue_lease_now": _NOW}


def _now() -> dt.datetime:
    return _SCENARIOS["launch fills free lanes"][1]


def _unowned(actions: list[Action] | None) -> set[str]:
    return {str(a["cause"]).removeprefix(f"{chair_kinds.UNOWNED_CAUSE} ") for a in actions or []
            if str(a.get("cause", "")).startswith(chair_kinds.UNOWNED_CAUSE)}


_LIVE_SETS = [frozenset(c) for n in range(1, len(KINDS) + 1) for c in itertools.combinations(KINDS, n)]


@pytest.mark.parametrize("live", _LIVE_SETS, ids=lambda k: "+".join(sorted(k)))
def test_with_the_tick_stopped_exactly_one_live_worker_reports_every_dead_kind(live: frozenset[str]):
    """Every way the five workers can be partly up: one live worker, the first in order, raises the alarms, and they
    name exactly the kinds that are down. So killing any one process, housekeeping included, never silences them."""
    facts = _facts(live)
    reporters = {kind: _unowned(planned_pass(kind, facts, _now(), 1, dry_run=False)) for kind in live}
    raising = [kind for kind, dead in reporters.items() if dead]
    dead = set(KINDS) - live
    if not dead:
        assert raising == []
        return
    assert raising == [next(k for k in KINDS if k in live)]
    assert reporters[raising[0]] == dead


def test_housekeeping_down_is_reported_by_the_watch_worker():
    facts = _facts(frozenset(KINDS) - {"housekeeping"})
    assert chair_kinds.watcher("watch", facts) and not chair_kinds.watcher("launch", facts)
    assert _unowned(chair_kinds.watcher_actions("watch", facts)) == {"housekeeping"}


def test_while_a_tick_holds_the_chair_lease_it_raises_them_and_no_worker_does():
    facts = _facts(frozenset({"watch"}), tick=True)
    assert not any(chair_kinds.watcher(kind, facts) for kind in KINDS)
    assert _unowned(plan_tick(facts, _now())) == {"housekeeping", "quarantine", "decompose", "launch"}


def test_a_session_holding_the_chair_lease_plans_no_tick_so_the_workers_still_watch():
    facts = {**_facts(frozenset({"housekeeping"})), "lease": {**_TICK, "session_held": True}}
    assert chair_kinds.watcher("housekeeping", facts)  # type: ignore[arg-type]


def test_a_stale_chair_lease_is_no_tick():
    facts = {**_facts(frozenset({"housekeeping"})), "lease": {**_TICK, "stale": True}}
    assert not chair_kinds.tick_watching(facts)  # type: ignore[arg-type]


def test_an_unlisted_kind_never_watches_and_is_never_reported():
    facts = _facts(frozenset({"watch", "launch"}), listed=("watch", "launch"))
    assert chair_kinds.watcher("watch", facts) and not chair_kinds.watcher("housekeeping", facts)
    assert chair_kinds.watcher_actions("watch", facts) == []  # both listed kinds are live


def test_unreadable_leases_make_every_listed_worker_say_so_once_it_has_failed_past_the_ttl():
    facts = {**_facts(frozenset()), "queue_leases": None, "queue_leases_failing_s": 61.0}
    for kind in KINDS:
        causes = [a["cause"] for a in chair_kinds.watcher_actions(kind, facts)]  # type: ignore[arg-type]
        assert causes == [chair_kinds.UNREADABLE_CAUSE]
    early = {**facts, "queue_leases_failing_s": 5.0}
    assert all(chair_kinds.watcher_actions(kind, early) == [] for kind in KINDS)  # type: ignore[arg-type]


def test_a_dry_run_pass_watches_no_one():
    facts = _facts(frozenset({"housekeeping"}))
    assert _unowned(planned_pass("housekeeping", facts, _now(), -1, dry_run=True)) == set()
    assert _unowned(planned_pass("housekeeping", facts, _now(), 1, dry_run=False)) == {"watch", "quarantine", "decompose", "launch"}


_HOSTS = [
    {"name": "lane-a", "state": "active", "versions_json": "{}"},
    {"name": "lane-b", "state": "unreachable", "versions_json": "{}"},
]


def test_the_host_checks_go_with_housekeeping():
    facts = {**_facts(frozenset(), tick=True, listed=()), "login_hosts": _HOSTS}
    alone = [a["kind"] for a in plan_tick(facts, _now()) if a["kind"] in ("check_login", "probe_host")]  # type: ignore[arg-type]
    assert sorted(alone) == ["check_login", "probe_host"]
    on = {**facts, "chair_queues": ["housekeeping"]}
    assert [a for a in plan_tick(on, _now()) if a["kind"] in ("check_login", "probe_host")] == []  # type: ignore[arg-type]
    worker = planned_pass("housekeeping", on, _now(), 1, dry_run=False)  # type: ignore[arg-type]
    assert sorted(a["kind"] for a in worker or [] if a["kind"] in ("check_login", "probe_host")) == alone


_CARD = Event("needs_chair", "decision:d1", "A decision", "ship it?")


def test_the_watcher_pushes_the_chair_wide_events_and_the_others_only_their_own():
    facts = {**_facts(frozenset(KINDS)), "initiatives": [], "intake": [], "approved": [],
             "dispatch": {"live_runs": 0, "hosts": []}}
    ask: Action = {"kind": "needs_chair", "initiative": "x", "cause": "held", "reason": "x is held"}  # type: ignore[typeddict-item]
    watcher = chair_worker_notify.worker_events("housekeeping", facts, [], [_CARD])  # type: ignore[arg-type]
    other = chair_worker_notify.worker_events("watch", facts, [ask], [_CARD])  # type: ignore[arg-type]
    assert QUEUE_EMPTY.key in {e.key for e in watcher} and _CARD in watcher
    assert QUEUE_EMPTY.key not in {e.key for e in other} and _CARD not in other
    assert len(other) == 1


def test_with_a_tick_up_no_worker_pushes_the_chair_wide_events():
    facts = _facts(frozenset(KINDS), tick=True)
    assert chair_worker_notify.worker_events("housekeeping", facts, [], [_CARD]) == []


def test_each_kind_keeps_its_own_sent_file():
    root = PurePosixPath("/w")
    assert {chair_worker_notify.sent_path(root, k) for k in KINDS} == {root / f"notify-sent-{k}.json" for k in KINDS}


def _ports(facts: Facts, pushed: list) -> chair_queue.Ports:
    clock = iter(float(n) for n in range(1000))
    return chair_queue.Ports(
        queued=lambda: frozenset(KINDS), acquire=lambda: 7, renew=lambda e: True, holds=lambda e: e == 7,
        release=lambda e: None, gather=lambda at: facts,
        perform_one=lambda a, c: [{"action": a, "status": "recorded", "reason": ""}],
        wait=lambda s: frozenset(), pause=lambda: None, clock=lambda: next(clock), now=_now, echo=lambda line: None,
        notify=lambda kind, f, actions: pushed.append((kind, [a.get("cause") for a in actions])) or "",
    )


def test_a_live_pass_pushes_its_alarms_and_a_dry_run_pushes_nothing():
    facts = _facts(frozenset({"housekeeping"}))
    pushed: list = []
    assert chair_queue.run("housekeeping", _ports(facts, pushed), chair_queue.Timing(poll=60, settle=0), once=True, dry_run=False) == 0
    assert len(pushed) == 1 and pushed[0][0] == "housekeeping"
    assert {f"{chair_kinds.UNOWNED_CAUSE} {k}" for k in ("watch", "quarantine", "decompose", "launch")} <= set(pushed[0][1])
    pushed.clear()
    assert chair_queue.run("housekeeping", _ports(facts, pushed), chair_queue.Timing(poll=60, settle=0), once=True, dry_run=True) == 0
    assert pushed == []


def test_a_push_that_raises_is_a_note_on_the_pass_line_not_a_failed_pass():
    facts = _facts(frozenset(KINDS))
    lines: list[str] = []

    def boom(kind, f, actions):
        raise OSError("no route")

    ports = _ports(facts, [])
    ports = chair_queue.Ports(**{**ports.__dict__, "notify": boom, "echo": lines.append})
    assert chair_queue.run("watch", ports, chair_queue.Timing(poll=60, settle=0), once=True, dry_run=False) == 0
    assert any("notify failed: OSError: no route" in line for line in lines)
