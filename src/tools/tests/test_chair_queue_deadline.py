"""C6: a hung worker pass must not look live. The beat renews the kind lease only while the pass is within its
deadline; past it the lease lapses (the watchdog and the other workers then see the kind as down), the rest of the
pass is fenced, and once the lease has surely lapsed the worker abandons the process for its supervisor to restart.

The loop and the beat thread are the real ones (`chair_queue.run`); the store is a recording stand-in."""

from __future__ import annotations

import threading
import time

from agent_tools import chair_queue
from agent_tools.chair_queue import Abandon, Lapse, Renew, pass_beat
from tests.test_chair_queue_alarms import KINDS, _facts, _now


def test_the_beat_renews_within_the_deadline_lapses_past_it_and_abandons_once_the_lease_has_lapsed():
    assert pass_beat(0.0, 899.0, 900.0, 60.0) == Renew()
    assert pass_beat(0.0, 900.0, 900.0, 60.0) == Lapse()
    assert pass_beat(0.0, 959.0, 900.0, 60.0) == Lapse()
    assert pass_beat(0.0, 960.0, 900.0, 60.0) == Abandon()


def test_every_kind_has_a_deadline_and_watch_outlasts_a_revert_waiting_on_checks():
    assert set(chair_queue.PASS_DEADLINE_SECONDS) == set(KINDS)
    assert chair_queue.PASS_DEADLINE_SECONDS["watch"] > 30 * 60
    assert all(d > chair_queue.LEASE_TTL_SECONDS for d in chair_queue.PASS_DEADLINE_SECONDS.values())


class Store:
    """The kind lease, as the store keeps it: renews are recorded, and `holds` says whether the epoch still holds."""

    def __init__(self) -> None:
        self.renews: list[float] = []
        self.lapsed = False

    def renew(self, epoch: int) -> bool:
        self.renews.append(time.monotonic())
        return True


def test_a_hung_pass_stops_renewing_at_its_deadline_fences_the_rest_and_abandons_after_one_ttl():
    store = Store()
    hang, abandoned = threading.Event(), threading.Event()
    lines: list[str] = []
    performed: list[str] = []
    started = time.monotonic()

    def perform_one(action, current):
        performed.append(str(action.get("kind")))
        if len(performed) == 1:
            hang.wait(5)  # the first action hangs past the deadline, the lapse and the abandon
        return [{"action": action, "status": "recorded", "reason": ""}]

    facts = _facts(frozenset({"housekeeping"}))  # four dead kinds: four alarms, so a rest to fence
    ports = chair_queue.Ports(
        queued=lambda: frozenset(KINDS), acquire=lambda: 7, renew=store.renew, holds=lambda e: e == 7,
        release=lambda e: None, gather=lambda at: facts, perform_one=perform_one,
        wait=lambda s: frozenset(), pause=lambda: None, clock=time.monotonic, now=_now, echo=lines.append,
        abandon=lambda: (abandoned.set(), hang.set()),
    )
    timing = chair_queue.Timing(poll=60, settle=0, beat=0.02, deadline=0.3, ttl=0.3)
    assert chair_queue.run("housekeeping", ports, timing, once=True, dry_run=False) == 0
    assert abandoned.is_set()
    deadline_at = started + 0.3
    assert store.renews and max(store.renews) < deadline_at + 0.05, store.renews  # no renew past the deadline
    assert any("pass over its 0s deadline" in line and "no longer renewed and lapses" in line for line in lines), lines
    assert any("exiting for the supervisor to restart" in line for line in lines), lines
    assert len(performed) == 1 and "lease lost: the rest fenced" in lines[-1], (performed, lines)


def test_a_pass_within_its_deadline_renews_throughout_and_is_never_abandoned():
    store = Store()
    abandoned: list[bool] = []

    def perform_one(action, current):
        time.sleep(0.05)
        return [{"action": action, "status": "recorded", "reason": ""}]

    facts = _facts(frozenset(KINDS), listed=("housekeeping",))
    lines: list[str] = []
    ports = chair_queue.Ports(
        queued=lambda: frozenset(KINDS), acquire=lambda: 7, renew=store.renew, holds=lambda e: e == 7,
        release=lambda e: None, gather=lambda at: facts, perform_one=perform_one,
        wait=lambda s: frozenset(), pause=lambda: None, clock=time.monotonic, now=_now, echo=lines.append,
        abandon=lambda: abandoned.append(True),
    )
    timing = chair_queue.Timing(poll=60, settle=0, beat=0.02, deadline=5.0, ttl=5.0)
    assert chair_queue.run("housekeeping", ports, timing, once=True, dry_run=False) == 0
    assert not abandoned and store.renews and "fenced" not in lines[-1], lines
