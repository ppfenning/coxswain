"""C6: the watchdog's timer is the line behind the workers. While `chair.queues` lists a kind it judges that kind's
worker lease on the store's clock, and with every kind listed (the tick stopped) it no longer judges the tick's lease or
status lines, which would read as a loop down from the moment of the stop."""

from __future__ import annotations

from datetime import UTC, datetime

from agent_tools.chair_kinds import QUEUE_KINDS
from agent_tools.chair_watchdog import (
    LOOP_DOWN_TITLE,
    QUEUES_DOWN_TITLE,
    evaluate_with_queues,
    queues_down,
    run_watchdog,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
ALL = frozenset(QUEUE_KINDS)
LIVE = "2026-11-21T12:00:40Z"
LONG_DEAD = "2026-11-21T11:40:00Z"  # twenty minutes ago


def _leases(dead: set[str]) -> dict[str, str]:
    return {kind: (LONG_DEAD if kind in dead else LIVE) for kind in QUEUE_KINDS}


def test_every_worker_up_is_no_alert():
    assert queues_down(ALL, _leases(set()), NOW) is None


def test_a_dead_worker_is_named_and_one_missing_its_row_too():
    alert = queues_down(ALL, {k: v for k, v in _leases({"housekeeping"}).items() if k != "launch"}, NOW)
    assert alert is not None and alert.title == QUEUES_DOWN_TITLE
    assert "housekeeping, launch" in alert.body


def test_a_worker_that_lapsed_moments_ago_is_not_down_yet():
    assert queues_down(ALL, {**_leases(set()), "watch": "2026-11-21T11:55:00Z"}, NOW, stale_minutes=10) is None


def test_an_unlisted_kind_is_never_judged():
    assert queues_down(frozenset({"watch"}), _leases({"housekeeping"}), NOW) is None


def test_every_worker_and_the_tick_dead_still_alerts():
    alerts = evaluate_with_queues(ALL, None, None, [], _leases(ALL), NOW, NOW)
    assert [a.title for a in alerts] == [QUEUES_DOWN_TITLE]


def test_with_every_kind_listed_the_stopped_tick_is_not_a_loop_down():
    assert evaluate_with_queues(ALL, None, None, [], _leases(set()), NOW, NOW) == ()


def test_with_some_kinds_listed_the_tick_is_still_judged_and_so_are_they():
    alerts = evaluate_with_queues(frozenset({"watch"}), None, None, [], _leases({"watch"}), NOW, NOW)
    assert [a.title for a in alerts] == [LOOP_DOWN_TITLE, QUEUES_DOWN_TITLE]


def test_nothing_listed_reads_no_worker_lease(tmp_path):
    def never(_dir):
        raise AssertionError("read the leases with nothing listed")

    code = run_watchdog(
        tmp_path, notify_config=None, state_path=tmp_path / "s.json", clock=lambda: NOW, dry_run=True,
        read_lease=lambda _d: ("host-a", NOW), read_rows=lambda _d, _n, _m: [], read_queues=never,
    )
    assert code == 0


def test_the_timer_pushes_the_queue_alert_on_the_store_clock(tmp_path):
    sent: list = []
    code = run_watchdog(
        tmp_path, notify_config=None, state_path=tmp_path / "s.json", clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
        notifier=lambda events, *_a: sent.extend(events) or [], read_lease=lambda _d: (None, None),
        read_rows=lambda _d, _n, _m: [], queued=ALL,
        read_queues=lambda _d: (_leases({"decompose"}), "2026-11-21T12:00:00Z"),
    )
    # The machine's clock says 2030, so every lease would read as dead; the store's says only decompose is.
    assert code == 0 and [e.key for e in sent] == [f"watchdog:{QUEUES_DOWN_TITLE}"]
    assert "decompose" in sent[0].body and "housekeeping" not in sent[0].body


def test_unreadable_worker_leases_are_an_unreadable_store(tmp_path, capsys):
    code = run_watchdog(
        tmp_path, notify_config=None, state_path=tmp_path / "s.json", clock=lambda: NOW, queued=ALL,
        read_lease=lambda _d: (None, None), read_rows=lambda _d, _n, _m: [], read_queues=lambda _d: (None, ""),
    )
    assert code == 1 and "chair:<kind>" in capsys.readouterr().err
