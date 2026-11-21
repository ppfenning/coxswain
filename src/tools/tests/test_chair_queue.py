"""C3: the queue worker's pure rules (when a pass is due, how long to wait, what its line says), the setting's parse,
the worker leases the tick reads, the profile reader and the notify-queue doctor row. No stubs: each is a pure
function, or reads a real file."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair_kinds, chair_read_queues, doctor, route
from agent_tools.chair_queue import Loop, Timing, heard, lease_due, pass_due, pass_line, pass_reason, wait_seconds

T = Timing(poll=60.0, min_gap=10.0, beat=10.0)


def test_the_first_pass_is_due_at_once_and_says_so():
    assert pass_due(Loop(), 0.0, T) and pass_reason(Loop()) == "start"


def test_a_notification_on_a_table_the_kind_reads_is_kept_and_one_on_another_is_dropped():
    loop = heard(Loop(last_pass=0.0), "housekeeping", frozenset({"work_items", "land_queue"}))
    assert loop.woke == {"work_items"}
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


def test_the_wait_ends_at_the_soonest_of_the_beat_the_poll_and_a_kept_notification_gap():
    assert wait_seconds(Loop(last_pass=100.0, last_beat=100.0), 103.0, Timing(poll=5.0, beat=10.0)) == 2.0
    assert wait_seconds(Loop(last_pass=100.0, last_beat=100.0), 103.0, T) == 7.0  # the beat, 10 s after the last
    woke = Loop(last_pass=100.0, last_beat=108.0, woke=frozenset({"runs"}))
    assert wait_seconds(woke, 108.0, T) == 2.0  # the gap ends at 110
    assert wait_seconds(Loop(last_pass=0.0, last_beat=0.0), 500.0, T) == 0.0  # overdue is never negative
    assert wait_seconds(Loop(), 3.0, T) == 0.0  # no beat yet: take the lease first


def test_the_lease_is_renewed_every_beat():
    assert lease_due(Loop(), 0.0, T)
    assert not lease_due(Loop(last_beat=0.0), 9.9, T)
    assert lease_due(Loop(last_beat=0.0), 10.0, T)


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


def test_a_worker_lease_counts_while_it_is_unexpired_and_only_for_a_known_kind():
    table = {
        "chair:housekeeping": ("h", "2026-11-20T12:01:00Z", "2026-11-20T12:00:00Z"),
        "chair:watch": ("w", "2026-11-20T11:59:00Z", "2026-11-20T11:58:00Z"),
        "chair:launch": ("l", "2026-11-20T12:05:00Z", "2026-11-20T12:00:00Z"),
        "chair": ("tick", "2026-11-20T12:05:00Z", "2026-11-20T12:00:00Z"),
    }
    assert chair_kinds.live_kinds(table, "2026-11-20T12:00:30Z") == {"housekeeping"}
    assert chair_kinds.lease_name("watch") == "chair:watch"


def test_the_tick_skips_the_queued_kinds_and_those_a_live_worker_holds():
    facts = {"chair_queues": ["housekeeping"], "queue_workers": ["watch", "launch"]}
    assert chair_kinds.tick_skips(facts) == {"housekeeping", "watch"}  # type: ignore[arg-type]
    assert chair_kinds.tick_skips({}) == frozenset()  # type: ignore[arg-type]
    assert chair_kinds.worker_acts("housekeeping", facts) and not chair_kinds.worker_acts("watch", facts)  # type: ignore[arg-type]


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
