"""C3: the queue worker's pure rules (when a pass is due, how long to wait, what its line says), the setting's parse,
the worker leases the tick reads, the profile reader and the notify-queue doctor row. No stubs: each is a pure
function, or reads a real file."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_kinds, chair_read_queues, doctor, route
from agent_tools.chair_queue import (
    Beat,
    Idle,
    Loop,
    Pass,
    Timing,
    Wait,
    beaten,
    decide,
    heard,
    idled,
    lost,
    pass_due,
    pass_line,
    pass_reason,
    passed,
)
from agent_tools.chair_types import Facts

T = Timing(poll=60.0, min_gap=10.0, beat=10.0, settle=20.0)
NOW = "2026-11-20T12:00:30Z"


def test_the_first_pass_is_due_at_once_and_says_so():
    assert pass_due(Loop(), 0.0, T) and pass_reason(Loop()) == "start"


def test_a_notification_on_a_table_the_kind_reads_is_kept_and_one_on_another_is_dropped():
    loop = heard(Loop(last_pass=0.0), "housekeeping", frozenset({"work_items", "land_queue", "hosts"}))
    assert loop.woke == {"work_items"}  # a host beat is not a reason to gather again
    assert heard(Loop(last_pass=0.0), "watch", frozenset({"work_items"})).woke == frozenset()
    assert heard(loop, "housekeeping", frozenset({"runs"})).woke == {"work_items", "runs"}


def test_a_kept_notification_makes_a_pass_due_once_the_gap_has_passed():
    loop = heard(Loop(last_pass=100.0), "housekeeping", frozenset({"runs"}))
    assert not pass_due(loop, 105.0, T)
    assert pass_due(loop, 110.0, T)
    assert pass_reason(loop) == "notified: runs"


def test_with_no_notification_the_poll_is_the_fallback():
    loop = Loop(last_pass=100.0)
    assert not pass_due(loop, 159.0, T)
    assert pass_due(loop, 160.0, T) and pass_reason(loop) == "poll"


def _run(outcomes: list[int | None], *, start: Loop | None = None, steps: int = 400, queued: bool = True) -> list[tuple[float, object]]:
    """Drive `decide` the way `run` does, with the clock moving by each wait. `outcomes` are the lease attempts' answers
    in order (an epoch, or None: refused, failed or timed out); a pass takes one second. Pure, nothing stubbed."""
    loop, now, answers, trace = start or Loop(), 0.0, list(outcomes), []
    for _ in range(steps):
        step = decide(loop, now, queued, False, T)
        trace.append((now, step))
        if isinstance(step, Beat):
            loop = beaten(loop, now, answers.pop(0) if answers else None)
        elif isinstance(step, Idle):
            loop, now = idled(loop), now + step.wait
        elif isinstance(step, Wait):
            now += step.seconds
        else:
            now += 1.0
            loop = passed(loop, now)
    return trace


def _waits(trace: list[tuple[float, object]]) -> list[float]:
    return [s.seconds for _, s in trace if isinstance(s, Wait)]


def test_a_standby_worker_waits_a_whole_beat_between_tries_and_never_spins():
    trace = _run([None] * 400)  # another worker holds the kind throughout
    assert all(isinstance(s, Beat | Wait) for _, s in trace)
    assert all(w == T.beat for w in _waits(trace)), _waits(trace)[:10]
    assert trace[-1][0] >= 190 * T.beat  # 400 steps cover about 200 beats of wall time, not 400 instants


def test_a_lease_lost_long_after_the_last_pass_stands_by_without_spinning():
    """The reviewer's case: the poll deadline is far in the past when the lease goes, which made the old wait zero."""
    holding = Loop(last_pass=0.0, last_beat=500.0, epoch=3, held_since=0.0)
    after = beaten(holding, 510.0, None)
    step = decide(after, 510.0, True, False, T)
    assert step == Wait(T.beat, True)
    assert decide(lost(holding), 505.0, True, False, T) == Wait(5.0, True)  # lost mid-pass: the rest of the beat


def test_a_store_outage_stands_by_then_the_worker_retakes_the_lease_settles_and_passes():
    trace = _run([1, None, None, None, *[2] * 20], steps=40)
    passes = [t for t, s in trace if isinstance(s, Pass)]
    beats = [t for t, s in trace if isinstance(s, Beat)]
    assert beats[:5] == [0.0, 10.0, 20.0, 30.0, 40.0]  # one try a beat through the outage, never sooner
    # Epoch 1 (0 s) is lost to the outage at 10 s, inside its settle, so it never passes. Epoch 2 (40 s) settles for
    # its own 20 s, then makes its own first pass.
    assert passes[0] == 60.0
    assert all(w > 0 for w in _waits(trace))


def test_takeover_a_standby_worker_takes_a_released_kind_at_its_next_beat_and_passes_after_settling():
    trace = _run([None, None, 7, 7, 7, 7], steps=12)
    took = next(t for t, s in trace if isinstance(s, Beat) and t == 20.0)
    first = next(t for t, s in trace if isinstance(s, Pass))
    assert (took, first) == (20.0, 40.0)


def test_a_kind_the_profile_does_not_list_idles_lets_its_lease_go_and_waits_a_beat():
    holding = Loop(last_beat=0.0, epoch=4, held_since=0.0)
    assert decide(holding, 1.0, False, False, T) == Idle(release=True, wait=T.beat)
    assert decide(idled(holding), 11.0, False, False, T) == Idle(release=False, wait=T.beat)
    assert decide(idled(holding), 12.0, True, False, T) == Beat()  # listed again: take the lease at once


def test_a_held_worker_waits_for_the_sooner_of_the_beat_and_the_next_pass():
    loop = Loop(last_pass=100.0, last_beat=103.0, epoch=1, held_since=0.0)
    assert decide(loop, 105.0, True, False, T) == Wait(8.0, False)  # the beat at 113
    woke = heard(loop, "housekeeping", frozenset({"runs"}))
    assert decide(woke, 108.0, True, False, T) == Wait(2.0, False)  # the gap ends at 110
    assert decide(woke, 110.0, True, False, T) == Pass("notified: runs")


def test_a_dry_run_takes_no_lease_and_passes_on_the_poll():
    assert decide(Loop(), 0.0, False, True, T) == Pass("start")
    assert decide(Loop(last_pass=0.0), 30.0, False, True, T) == Wait(30.0, False)


def test_the_pass_line_names_the_kind_the_reason_and_what_was_performed():
    action = {"kind": "housekeeping", "reason": "due", "epoch": 4}
    results = [{"action": action, "status": "done", "reason": ""}]
    line = pass_line("housekeeping", "notified: work_items", [action], results, False)  # type: ignore[list-item]
    assert line == "chair worker housekeeping | notified: work_items | planned 1 | 1x housekeeping done"
    assert pass_line("watch", "poll", [], [], True) == "chair worker watch (dry run) | poll | nothing due"
    assert "not queued" in pass_line("watch", "poll", None, [], False)
    at = datetime(2026, 11, 20, 12, 0, 5, 123456, tzinfo=UTC)
    assert pass_line("watch", "poll", [], [], False, at) == "chair worker watch 2026-11-20T12:00:05.123Z | poll | nothing due"


@pytest.mark.parametrize(
    ("raw", "kinds"),
    [
        (None, frozenset()),
        ([], frozenset()),
        (["housekeeping"], {"housekeeping"}),
        (["watch", "housekeeping", "watch"], {"watch", "housekeeping"}),
        (["launch", "housekeeping", 3], {"housekeeping"}),  # a kind with no worker yet stays on the tick
        ("housekeeping", frozenset()),  # not a list: none, so a typo never leaves a kind unplanned
    ],
)
def test_the_setting_lists_only_kinds_a_worker_can_run(raw: object, kinds: frozenset[str]):
    assert chair_kinds.queued_kinds(raw) == kinds


def test_a_worker_lease_counts_while_it_is_unexpired_by_the_stores_clock_and_only_for_a_known_kind():
    leases = {"housekeeping": "2026-11-20T12:01:00Z", "watch": "2026-11-20T11:59:00Z", "launch": "2026-11-20T12:05:00Z"}
    assert chair_kinds.live_kinds(leases, NOW) == {"housekeeping"}
    assert chair_kinds.lease_name("watch") == "chair:watch"


def _facts(**kw: object) -> Facts:
    return kw  # type: ignore[return-value]


def test_the_tick_skips_listed_kinds_kinds_a_live_worker_holds_and_every_kind_when_the_leases_are_unreadable():
    leases = {"watch": "2026-11-20T12:01:00Z"}
    facts = _facts(chair_queues=["housekeeping"], queue_leases=leases, queue_lease_now=NOW)
    assert chair_kinds.tick_skips(facts) == {"housekeeping", "watch"}
    assert chair_kinds.tick_skips(_facts(queue_leases=leases, queue_lease_now=NOW)) == {"watch"}  # whatever the profile says
    assert chair_kinds.tick_skips(_facts()) == frozenset()
    assert chair_kinds.tick_skips(_facts(queue_leases=None, queue_lease_now="")) == set(chair_kinds.QUEUE_KINDS)  # fails closed
    assert chair_kinds.worker_acts("housekeeping", facts) and not chair_kinds.worker_acts("watch", facts)


def test_a_listed_kind_with_no_live_worker_for_longer_than_the_ttl_is_reported_once_as_unowned():
    listed = {"chair_queues": ["housekeeping", "watch"], "queue_lease_now": NOW}
    fresh = _facts(**listed, queue_leases={"housekeeping": "2026-11-20T12:00:40Z", "watch": "2026-11-20T11:59:40Z"})
    assert chair_kinds.unowned_kinds(fresh) == frozenset()  # watch lapsed 50 s ago: within the ttl
    dead = _facts(**listed, queue_leases={"housekeeping": "2026-11-20T12:00:40Z", "watch": "2026-11-20T11:59:00Z"})
    assert chair_kinds.unowned_kinds(dead) == {"watch"}
    never = _facts(**listed, queue_leases={})
    assert chair_kinds.unowned_kinds(never) == {"housekeeping", "watch"}  # no worker ever took them
    assert chair_kinds.unowned_kinds(_facts(**listed, queue_leases=None)) == frozenset()  # unknown is not unowned
    assert chair_kinds.unowned_kinds(_facts(queue_leases={}, queue_lease_now=NOW)) == frozenset()  # nothing listed
    (action,) = chair_kinds.unowned_actions(dead)
    assert (action["kind"], action["cause"]) == ("needs_chair", "queue_unowned watch")
    assert "cox chair worker --kind watch" in action["reason"]


def test_the_unowned_notice_is_pushed_once_while_it_stands():
    from agent_tools import notify_core
    from agent_tools.notify_events import events_from_tick

    dead = _facts(chair_queues=["watch"], queue_leases={}, queue_lease_now=NOW)
    events = events_from_tick({}, chair_kinds.unowned_actions(dead))
    first, sent = notify_core.due(events, {}, 0.0)
    again, _ = notify_core.due(events_from_tick({}, chair_kinds.unowned_actions(dead)), sent, 60.0)
    assert [e.key for e in first] == ["needs_chair:chair-queue:queue_unowned watch"] and again == []


def test_the_profile_reader_keeps_its_last_good_read_through_a_broken_edit(tmp_path: Path):
    profile = tmp_path / "profile.yaml"
    read = chair_read_queues.queues_keeping_last_good(profile)
    assert read() == frozenset()  # no profile: nothing moves
    profile.write_text("team: t\nchair:\n  queues: [housekeeping, watch]\n", encoding="utf-8")
    assert read() == {"housekeeping", "watch"}
    profile.write_text("team: t\nchair:\n  queues: [housekeeping\n", encoding="utf-8")
    assert isinstance(chair_read_queues.read_chair_queues(profile), Exception)
    assert read() == {"housekeeping", "watch"}
    profile.write_text("team: t\nchair:\n  queues: []\n", encoding="utf-8")
    assert read() == frozenset()


def test_the_notify_queue_row_fails_above_the_limit_and_says_why():
    assert doctor.notify_queue_rows(False, None, None) == []
    ok = doctor.notify_queue_rows(True, 0.0001, None)
    assert ok == [{"check": doctor.NOTIFY_QUEUE_CHECK, "ok": True, "detail": "0.0% of the notification queue in use"}]
    assert doctor.notify_queue_rows(True, doctor.NOTIFY_QUEUE_LIMIT, None)[0]["ok"] is True
    (full,) = doctor.notify_queue_rows(True, 0.75, None)
    assert full["ok"] is False and "75.0%" in full["detail"] and "without draining" in full["detail"]
    (unread,) = doctor.notify_queue_rows(True, None, "the store did not answer (OperationalError)")
    assert unread["ok"] is False and "OperationalError" in unread["detail"]


def test_the_profile_takes_chair_queues_as_a_flow_list_and_refuses_any_other_shape():
    parsed = route.parse_profile("team: t\nchair:\n  stale_days: 3\n  queues: [housekeeping, 'watch']\n")
    assert parsed["chair"] == {"stale_days": 3.0, "queues": ["housekeeping", "watch"]}
    assert route.parse_profile("team: t\nchair:\n  queues: []\n")["chair"] == {"queues": []}
    with pytest.raises(route.ProfileError, match="chair queues is a list"):
        route.parse_profile("team: t\nchair:\n  queues: housekeeping\n")
