"""The phase queue's pure rules: matching, choosing, lease math, the stop rule, fencing and the ack or nack."""

import pytest

from harness import phase_pick
from harness.phase_pick import Verdict

NOW = "2026-11-20T10:00:00Z"


def _pick(rows, capabilities, free, now):
    """The row a claim tries first: the first of `choices`."""
    return next(iter(phase_pick.choices(rows, capabilities, free, now)), None)


def _row(phase: str, *, requires=(), state="ready", band=1, seq=0, ready_at="", lease_until="", initiative="i"):
    return {"initiative": initiative, "phase": phase, "requires_json": list(requires), "state": state, "band": band,
            "seq": seq, "ready_at": ready_at, "lease_until": lease_until}


def test_the_store_clock_text_of_either_engine_reads_as_one_stamp():
    assert phase_pick.stamp_of("2026-11-20 10:00:00") == NOW
    assert phase_pick.stamp_of("2026-11-20 10:00:00.987654") == NOW
    assert phase_pick.stamp_of("2026-11-20T10:00:00Z") == NOW


def test_a_lease_is_the_ttl_after_now_and_crosses_midnight():
    assert phase_pick.lease(NOW) == "2026-11-20T10:01:00Z"
    assert phase_pick.lease("2026-11-20T23:59:30Z", 60) == "2026-11-21T00:00:30Z"


def test_the_operator_timings_leave_the_lease_outliving_the_stop():
    assert (phase_pick.HEARTBEAT_S, phase_pick.LEASE_S, phase_pick.STOP_AFTER_S) == (10, 60, 45)
    assert phase_pick.STOP_AFTER_S < phase_pick.LEASE_S


def test_a_worker_stops_once_45_seconds_pass_without_a_good_heartbeat():
    assert not phase_pick.must_stop(100.0, 144.9)
    assert phase_pick.must_stop(100.0, 145.0)


def test_backoff_doubles_from_a_minute_and_stops_at_fifteen():
    assert [phase_pick.backoff_s(n) for n in (1, 2, 3, 4, 5, 9)] == [60, 120, 240, 480, 900, 900]


def test_requirements_are_a_subset_of_capabilities():
    assert phase_pick.meets([], [])
    assert phase_pick.meets(["docker"], ["docker", "os:linux"])
    assert not phase_pick.meets(["os:macos"], ["docker", "os:linux"])


def test_tokens_are_lowercase_names_or_key_values():
    assert phase_pick.bad_tokens(["docker", "os:macos", "repo:app", "toolchain:go1.22"]) == []
    assert phase_pick.bad_tokens(["OS:mac", "a:b:c", ":x", ""]) == ["", ":x", "OS:mac", "a:b:c"]


def test_ready_rows_wait_out_their_backoff_and_claimed_rows_their_lease():
    assert phase_pick.claimable(_row("p"), NOW)
    assert phase_pick.claimable(_row("p", ready_at=NOW), NOW)
    assert not phase_pick.claimable(_row("p", ready_at="2026-11-20T10:00:01Z"), NOW)
    assert phase_pick.claimable(_row("p", state="claimed", lease_until="2026-11-20T09:59:59Z"), NOW)
    assert not phase_pick.claimable(_row("p", state="claimed", lease_until=NOW), NOW)
    assert not phase_pick.claimable(_row("p", state="finished"), NOW)


def test_pick_takes_the_first_in_band_and_seq_order_that_the_worker_meets():
    rows = [
        _row("late", seq=5), _row("mac", band=0, seq=0, requires=["os:macos"]), _row("first", band=0, seq=1),
        _row("held", band=0, seq=0, state="claimed", lease_until="2026-11-20T10:00:30Z"),
    ]
    assert _pick(rows, ["os:linux"], 1, NOW)["phase"] == "first"
    assert _pick(rows, ["os:macos"], 1, NOW)["phase"] == "mac"


def test_pick_takes_nothing_without_a_free_lane_or_a_qualifying_row():
    rows = [_row("mac", requires=["os:macos"])]
    assert _pick(rows, ["os:macos"], 0, NOW) is None
    assert _pick(rows, ["os:linux"], 2, NOW) is None


def test_a_lapsed_claim_is_picked_like_a_ready_row():
    lapsed = _row("p", state="claimed", lease_until="2026-11-20T09:00:00Z")
    assert _pick([lapsed], [], 1, NOW) is lapsed


@pytest.mark.parametrize(
    ("row", "worker", "epoch", "held"),
    [
        ({"state": "claimed", "claimed_by": "a", "claim_epoch": 2, "lease_until": "2026-11-20T10:00:10Z"}, "a", 2, True),
        ({"state": "claimed", "claimed_by": "a", "claim_epoch": 3, "lease_until": "2026-11-20T10:00:10Z"}, "a", 2, False),
        ({"state": "claimed", "claimed_by": "b", "claim_epoch": 2, "lease_until": "2026-11-20T10:00:10Z"}, "a", 2, False),
        ({"state": "claimed", "claimed_by": "a", "claim_epoch": 2, "lease_until": NOW}, "a", 2, False),
        ({"state": "finished", "claimed_by": "a", "claim_epoch": 2, "lease_until": ""}, "a", 2, False),
        (None, "a", 2, False),
    ],
)
def test_a_write_holds_its_claim_only_at_its_epoch_by_its_worker_in_its_lease(row, worker, epoch, held):
    assert phase_pick.fenced(row, worker, epoch, NOW) is held


def test_every_task_approved_is_an_ack():
    assert phase_pick.verdict(["approved", "approved", "dropped"], 1) == Verdict("ack", "approved", "every task of the phase is approved")


def test_a_phase_with_nothing_left_to_build_and_a_quarantine_is_a_permanent_nack():
    found = phase_pick.verdict(["approved", "quarantined"], 1)
    assert (found.kind, found.outcome) == ("quarantine", "quarantined")


def test_a_phase_with_work_left_is_retried_with_backoff_until_its_attempts_run_out():
    assert phase_pick.verdict(["approved", "ready"], 1) == Verdict("retry", "", "tasks left approved, ready", 60)
    assert phase_pick.verdict(["quarantined", "in_progress"], 2).kind == "retry"
    spent = phase_pick.verdict(["ready"], 3)
    assert (spent.kind, spent.outcome) == ("quarantine", "failed")
    assert spent.reason == "3 attempt(s), the last: tasks left ready"


def test_a_phase_whose_tasks_were_all_dropped_is_acked_not_failed():
    assert phase_pick.verdict(["dropped", "dropped"], 3) == Verdict("ack", "approved", "every task of the phase was dropped")


def test_a_refused_launch_gives_its_attempt_back_and_waits_its_backoff():
    assert phase_pick.refusal("the launch was refused: usage stop", 1) == Verdict("retry", "", "the launch was refused: usage stop", 60, refund=True)
    assert phase_pick.refusal("x", 3).wait_s == 120


def test_a_phase_with_its_attempts_used_is_never_claimable_and_is_exhausted_once_nobody_holds_it():
    spent = _row("p", ready_at=NOW)
    spent["attempts"] = 3
    assert not phase_pick.claimable(spent, NOW)
    assert phase_pick.exhausted(spent, NOW)
    lapsed = {**_row("p", state="claimed", lease_until="2026-11-20T09:00:00Z"), "attempts": 3}
    assert not phase_pick.claimable(lapsed, NOW) and phase_pick.exhausted(lapsed, NOW)
    held = {**_row("p", state="claimed", lease_until="2026-11-20T10:00:30Z"), "attempts": 3}
    assert not phase_pick.exhausted(held, NOW)
    assert not phase_pick.exhausted({**_row("p"), "attempts": 2}, NOW)


def test_choices_keep_every_row_the_worker_meets_in_order_past_those_it_cannot():
    rows = [_row(f"mac{n}", seq=n, requires=["os:macos"]) for n in range(20)] + [_row("any", seq=99)]
    assert [r["phase"] for r in phase_pick.choices(rows, ["os:linux"], 1, NOW)] == ["any"]
    assert len(phase_pick.choices(rows, ["os:macos"], 1, NOW)) == 21
    assert phase_pick.choices(rows, ["os:macos"], 0, NOW) == []


def test_a_run_that_never_started_is_retried_even_when_its_tasks_look_done():
    found = phase_pick.verdict(["approved"], 1, launch_error="the launch exited 2: no initiative.md")
    assert found == Verdict("retry", "", "the launch exited 2: no initiative.md", 60)
    assert phase_pick.verdict([], 1).reason == "the phase has no tasks in the store"
