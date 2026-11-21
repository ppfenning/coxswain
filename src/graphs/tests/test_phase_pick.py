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


def test_a_stop_starts_early_enough_that_the_run_is_gone_by_45_seconds():
    grace = phase_pick.STOP_GRACE_S + phase_pick.KILL_WAIT_S + phase_pick.BEAT_LOOP_S
    assert phase_pick.stop_at(100.0) == 100.0 + 45 - grace == 137.0
    assert phase_pick.stop_at(100.0) + phase_pick.STOP_GRACE_S + phase_pick.KILL_WAIT_S + phase_pick.BEAT_LOOP_S <= 145.0
    assert phase_pick.next_step(100.0, 137.0, 999.0) == "stop"
    assert phase_pick.next_step(100.0, 136.9, 999.0) == "wait"


def test_a_heartbeat_is_sent_only_when_it_is_due_and_cannot_run_past_the_stop():
    assert phase_pick.next_step(100.0, 105.0, 110.0) == "wait"
    assert phase_pick.next_step(100.0, 110.0, 110.0) == "beat"
    assert phase_pick.next_step(100.0, 127.0, 120.0) == "beat"  # 127 + 10 = 137: it still ends in time
    assert phase_pick.next_step(100.0, 127.1, 120.0) == "stop"  # a 10 s call from here would end past 137


def test_a_lease_s_timings_scale_as_the_operator_s_sixty_seconds_do():
    assert phase_pick.Timings.of(60) == phase_pick.DEFAULT_TIMINGS == phase_pick.Timings(60, 10, 45)
    assert phase_pick.DEFAULT_TIMINGS.margin_s == phase_pick.LANE_MARGIN_S == 15
    short = phase_pick.Timings.of(48)
    assert (short.heartbeat_s, short.stop_after_s, short.margin_s) == (8, 36, 12)
    assert phase_pick.stop_at(100.0, short) == 100.0 + 36 - 8
    assert phase_pick.next_step(100.0, 108.0, 108.0, short) == "beat"  # 108 + 10 = 118: ends before its stop at 128
    assert phase_pick.next_step(100.0, 118.1, 108.0, short) == "stop"


def test_the_shortest_lease_leaves_its_lanes_a_check_a_push_and_slack_and_its_heartbeat_time_to_finish():
    from harness import lane_rollup

    assert phase_pick.PUSH_BOUND_S == lane_rollup.REMOTE_TIMEOUT_S
    # What can pass after a good check's store read: the rest of that check, one bounded push, and the slack.
    assert phase_pick.MARGIN_FLOOR_S == phase_pick.CHECK_CALL_S + phase_pick.PUSH_BOUND_S + phase_pick.SKEW_S == 12
    assert phase_pick.MIN_LEASE_S == 48 and phase_pick.MAX_LEASE_S == 600
    assert phase_pick.lease_problem(phase_pick.MIN_LEASE_S) == "" and phase_pick.lease_problem(60) == ""
    assert "a claim check (3s), one bounded push (8s) and 1s of slack" in phase_pick.lease_problem(phase_pick.MIN_LEASE_S - 1)
    assert phase_pick.lease_problem(32) and phase_pick.lease_problem(40)  # the old floor, and the reviewer's
    assert phase_pick.lease_problem(0) and phase_pick.lease_problem(-5)
    assert phase_pick.lease_problem(phase_pick.MAX_LEASE_S) == ""
    assert "longer than the longest a worker takes (600s)" in phase_pick.lease_problem(phase_pick.MAX_LEASE_S + 1)
    assert phase_pick.lease_problem(86_400)
    accepted = range(phase_pick.MIN_LEASE_S, phase_pick.MAX_LEASE_S + 1)
    assert all(phase_pick.lease_problem(n) == "" for n in accepted)
    for n in accepted:
        t = phase_pick.Timings.of(n)
        # A heartbeat sent when due ends, at its client-side bound, before the stop; the run is gone before the margin.
        assert phase_pick.next_step(0.0, t.heartbeat_s, t.heartbeat_s, t) == "beat", n
        assert t.heartbeat_s + phase_pick.HEARTBEAT_CALL_S <= phase_pick.stop_at(0.0, t), n
        assert t.stop_after_s + t.margin_s == n, n
        # A push that follows a good check lands before the lease lapses, the check's own worst case included.
        assert t.margin_s >= phase_pick.CHECK_CALL_S + phase_pick.PUSH_BOUND_S + phase_pick.SKEW_S, n
        # The lane's other statements, and a fence check that hangs, end inside the margin too.
        assert t.margin_s > phase_pick.LANE_STATEMENT_S and t.margin_s > phase_pick.CHECK_CALL_S, n


def test_a_lane_reads_its_margin_from_its_claim_s_own_lease():
    def row(heartbeat_at, lease_until):
        return {"heartbeat_at": heartbeat_at, "lease_until": lease_until}

    assert phase_pick.margin_of(row(NOW, phase_pick.lease(NOW, 60))) == 15
    assert phase_pick.margin_of(row(NOW, phase_pick.lease(NOW, 48))) == 12
    assert phase_pick.margin_of(row(NOW, phase_pick.lease(NOW, 120))) == 30
    # A row that says no lease a worker accepts: the default margin.
    assert phase_pick.margin_of(row(NOW, phase_pick.lease(NOW, 10))) == 15
    assert phase_pick.margin_of(row(NOW, phase_pick.lease(NOW, 32))) == 15  # under the floor now
    assert phase_pick.margin_of(row(NOW, phase_pick.lease(NOW, 3600))) == 15  # over the cap
    assert phase_pick.margin_of(row("", "")) == phase_pick.margin_of(None) == phase_pick.margin_of({}) == 15
    assert phase_pick.seconds(NOW, "2026-11-20T10:00:32Z") == 32 and phase_pick.seconds("2026-11-20T10:00:32Z", NOW) == -32


def test_a_lapsed_claim_s_lost_mark_names_its_holder_epoch_and_run_and_is_the_same_each_time():
    held = {**_row("p1", state="claimed", lease_until="2026-11-20T09:59:00Z"), "claimed_by": "w1", "claim_epoch": 3,
            "run_id": "i.p1-4"}
    assert phase_pick.lapsed(held, NOW) and not phase_pick.lapsed(held, "2026-11-20T09:58:00Z")
    assert not phase_pick.lapsed({**held, "state": "ready"}, NOW)
    assert phase_pick.lost_reason(held) == phase_pick.lost_reason(dict(held)) == (
        "run lost: w1's claim at epoch 3 lapsed at 2026-11-20T09:59:00Z, leaving run i.p1-4; claimable again")
    assert phase_pick.lost_reason({**held, "run_id": ""}).endswith("lapsed at 2026-11-20T09:59:00Z; claimable again")
    assert phase_pick.lost_reason(held).startswith(phase_pick.RUN_LOST)


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
