"""The phase worker's pure rules: lanes, capabilities, repository tokens, the launch, and `cox worker status` lines.
The loop itself is glue, proven against a real store by smoke/test_worker_claims.py."""

import pytest

from agent_tools import worker, worker_cli, worker_ports
from harness import phase_pick

NOW = "2026-11-20T10:00:00Z"


def _lane_rows(ok: bool) -> list[dict]:
    from agent_tools import lane_prereqs

    checks = [lane_prereqs.DOCKER_CHECK, *lane_prereqs.CLIENT_TOOLS, *(lane_prereqs.image_check(i) for i in lane_prereqs.SMOKE_IMAGES)]
    return [{"check": c, "ok": ok if c == lane_prereqs.DOCKER_CHECK else True, "detail": ""} for c in checks]


def test_free_lanes_share_the_machine_with_old_model_runs():
    assert worker.free_lanes(3, 1, 1) == 1
    assert worker.free_lanes(1, 1, 0) == 0
    assert worker.free_lanes(1, 0, 4) == 0


def test_old_model_runs_are_the_live_runs_no_claim_started():
    assert worker.old_model_runs(["ship.p1-1", "old-3", "other-1"], {"ship.p1-1", ""}) == 2


def test_docker_is_advertised_only_when_the_lane_checks_pass_whatever_was_declared():
    assert worker.capabilities(["os:linux"], ["docker"], _lane_rows(True)) == ["docker", "os:linux"]
    assert worker.capabilities(["os:linux"], ["docker"], _lane_rows(False)) == ["os:linux"]
    assert worker.capabilities(["os:linux"], [], []) == ["os:linux"]


def test_a_declared_key_replaces_the_detected_one():
    assert worker.capabilities(["os:linux", "arch:amd64"], ["os:macos", "toolchain:go"], []) == [
        "arch:amd64", "os:macos", "toolchain:go",
    ]


def test_repo_tokens_name_only_the_queued_repos_checked_out_here():
    assert worker.repo_tokens(["~/src/app", "~/src/lib/", "", "~/src/gone"], {"~/src/app", "~/src/lib/"}) == ["repo:app", "repo:lib"]


def test_the_launch_is_the_phase_lane_of_the_claim_under_the_loop_s_label():
    assert worker.launch_argv("ship", "p1", "ship.p1-1", "chair-x", "w1", 4) == [
        "cox", "route", "launch", "epic", "--local", "--initiative", "work/ship", "--phase", "p1", "--run-id", "ship.p1-1",
        "--label", "chair-x", "--no-claim", "--lane-worker", "w1", "--claim-epoch", "4",
    ]
    assert worker.launch_label({"session": "chair-x"}, "worker:w1") == "chair-x"
    assert worker.launch_label(None, "worker:w1") == "worker:w1"
    assert worker.launch_label({"session": ""}, "worker:w1") == "worker:w1"


def test_status_says_who_holds_a_claim_and_when_one_has_lapsed():
    phases = [
        {"initiative": "ship", "phase": "p1", "state": "claimed", "claim_epoch": 2, "attempts": 2, "claimed_by": "w1",
         "lease_until": "2026-11-20T10:00:30Z", "requires_json": ["docker"], "run_id": "ship.p1-2"},
        {"initiative": "ship", "phase": "p2", "state": "claimed", "claim_epoch": 1, "attempts": 1, "claimed_by": "w2",
         "lease_until": "2026-11-20T09:59:00Z", "requires_json": [], "run_id": ""},
        {"initiative": "ship", "phase": "p3", "state": "ready", "claim_epoch": 0, "attempts": 1, "ready_at": "2026-11-20T10:01:00Z",
         "requires_json": ["os:macos"]},
    ]
    workers = [{"name": "w1", "state": "up", "lanes": 1, "capabilities_json": ["docker"], "beat_at": NOW}]
    assert worker_cli.status_lines(phases, workers, NOW) == [
        "ship/p1: claimed, epoch 2, 2 attempt(s), needs [docker]; held by w1 until 2026-11-20T10:00:30Z; run ship.p1-2",
        "ship/p2: claimed, epoch 1, 1 attempt(s), needs []; lapsed at 2026-11-20T09:59:00Z, 60s ago (was w2's, epoch 1), "
        "claimable",
        "ship/p3: ready, epoch 0, 1 attempt(s), needs [os:macos]; claimable from 2026-11-20T10:01:00Z",
        "",
        f"worker w1: up, 1 lane(s), [docker], beat {NOW}",
    ]
    assert worker_cli.status_lines([], [], NOW) == ["the phase queue is empty", "", "no worker has registered"]


def test_status_shows_a_lapsed_claim_s_lost_run_and_whether_the_chair_marked_it():
    row = {"initiative": "ship", "phase": "p1", "state": "claimed", "claim_epoch": 1, "attempts": 1, "claimed_by": "a",
           "lease_until": "2026-11-20T09:59:28Z", "heartbeat_at": "2026-11-20T09:58:56Z", "requires_json": [],
           "run_id": "ship.p1-1", "reason": ""}
    assert worker_cli.claim_of(row, NOW) == {"holder": "a", "epoch": 1, "lease_until": "2026-11-20T09:59:28Z",
                                             "run_id": "ship.p1-1", "lapsed": True, "lapsed_s": 32, "marked_lost": False}
    (line, *_) = worker_cli.status_lines([row], [], NOW)
    assert line == ("ship/p1: claimed, epoch 1, 1 attempt(s), needs []; lapsed at 2026-11-20T09:59:28Z, 32s ago "
                    "(was a's, epoch 1); run ship.p1-1 lost, claimable")
    marked = {**row, "reason": phase_pick.lost_reason(row)}
    assert worker_cli.claim_of(marked, NOW)["marked_lost"] is True
    assert worker_cli.status_lines([marked], [], NOW)[0].endswith("; run ship.p1-1 lost (the chair marked it), claimable")
    held = {**row, "lease_until": "2026-11-20T10:00:20Z"}
    assert worker_cli.claim_of(held, NOW) == {**worker_cli.claim_of(row, NOW), "lease_until": "2026-11-20T10:00:20Z",
                                              "lapsed": False, "lapsed_s": None}
    assert worker_cli.claim_of({**row, "state": "ready"}, NOW) is None


def test_a_worker_s_store_url_bounds_every_call_inside_the_heartbeat_window():
    bounded = worker.bounded_url("postgresql://u:p@db:5432/cox?options=-csearch_path%3Ds")
    assert "connect_timeout=5" in bounded
    assert "options=-csearch_path%3Ds+-c+statement_timeout%3D5000+-c+lock_timeout%3D3000" in bounded, bounded
    assert "connect_timeout=2" in worker.bounded_url("postgresql://db/cox?connect_timeout=2")
    assert worker.bounded_url("sqlite:///x.db") == "sqlite:///x.db"


def test_a_gate_s_refusal_is_told_apart_from_a_failed_launch():
    assert worker.refused(2, "routing: ship.p1-1 is already running (pid 9) on host-a\n")
    assert worker.refused(2, "refusing: the landing loop is held by chair-x (live); pass --force to override")
    assert worker.refused(2, "routing: usage stop: weekly ceiling")
    assert not worker.refused(2, "routing: ship has no phase p9; phases: p1")
    assert not worker.refused(1, "routing: launch failed: is already running")


# The heartbeat pass, driven by an injected clock: no sleep, no thread. A store that does not answer is a heartbeat
# that raises, as a bounded statement that timed out does.

def _claim(phase: str = "p1", epoch: int = 1):
    from harness.store_phases import Claim

    return Claim("ship", phase, epoch, "2026-11-20T10:01:00Z", {"attempts": 1})


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _pass(leases, clock, heartbeat):
    """One heartbeat pass; the runs it asked to stop. The request only records, as `stopper`'s returns at once."""
    stopped: list = []
    worker.beat(leases, heartbeat, lambda claim, run: stopped.append(run), clock, lambda line: None)
    return stopped


def _timed_out(claim):
    raise TimeoutError("statement timeout")


def test_a_claim_whose_heartbeats_time_out_is_stopped_early_enough_to_be_gone_by_45_seconds():
    clock, leases = _Clock(), worker.Leases()
    run = worker.Running("ship.p1-1", 4242)
    leases.add(_claim(), clock(), run)  # the clock starts when the worker set out to claim
    for t in (110.0, 120.0):  # each due heartbeat fits before the stop time (137), and times out
        clock.t = t
        assert _pass(leases, clock, _timed_out) == []
    clock.t = 130.0  # due again, but a 10 s call from here could end past 137: stop now instead
    assert _pass(leases, clock, _timed_out) == [run]
    (held,) = leases.snapshot()
    assert (held.state, held.stopped) == ("expired", False)  # stopped is set by the stop thread once the run is gone
    assert leases.take(_claim()) is None  # the main loop writes nothing for it


def test_a_heartbeat_that_runs_long_and_fails_is_stopped_on_its_return():
    clock, leases = _Clock(), worker.Leases()
    run = worker.Running("ship.p1-1", 4242)
    leases.add(_claim(), clock(), run)
    clock.t = 127.0  # due and fits: 127 + 10 = 137

    def slow(claim):
        clock.t = 137.5  # the call took its whole bound, and then some
        raise TimeoutError("statement timeout")

    assert _pass(leases, clock, slow) == [run]


def test_a_worker_on_a_short_lease_beats_and_stops_on_its_own_timings():
    short = phase_pick.Timings.of(48)  # a heartbeat every 8 s, a stop 36 s after the last good one
    clock, leases = _Clock(), worker.Leases(timings=short)
    run = worker.Running("ship.p1-1", 4242)
    leases.add(_claim(), clock(), run)
    assert leases.snapshot()[0].beat_due == 108.0
    beats: list[float] = []
    for t in (107.0, 108.0, 116.0):
        clock.t = t
        assert _pass(leases, clock, lambda claim: beats.append(clock.t) or True) == []
    assert beats == [108.0, 116.0] and leases.snapshot()[0].last_ok == 116.0
    for t in (124.0, 132.0):  # due, and t + 10 still ends before the stop at 116 + 28: sent, and it times out
        clock.t = t
        assert _pass(leases, clock, _timed_out) == []
    clock.t = 140.0
    assert _pass(leases, clock, _timed_out) == [run]  # 140 + 10 > 144: stop, 28 s after the last good heartbeat
    assert leases.snapshot()[0].state == "expired"


@pytest.mark.parametrize("lease_s", [0, 32, 40, 47, 601, 3600])
def test_a_lease_outside_the_floor_and_the_cap_is_refused_with_exit_2_before_the_store_is_opened(
        lease_s, monkeypatch, tmp_path, capsys):
    import argparse

    monkeypatch.setattr(worker_cli, "_workspace", lambda a: tmp_path)
    monkeypatch.setattr(worker_ports, "build", lambda *a, **k: pytest.fail("the store was opened"))
    a = argparse.Namespace(capability=None, lease_s=lease_s, name="w", lanes=None)
    assert worker_cli.cmd_run(a) == 2
    said = capsys.readouterr().out
    assert said.startswith(f"worker: --lease-s {lease_s}: ") and "a lease runs from 48s to 600s" in said, said


def test_the_lease_floor_and_cap_the_help_names_are_the_rules():
    from agent_tools import cli

    (arg,) = [a for a in cli._WORKER_ARGS if a.flags == ("--lease-s",)]
    assert f"from {phase_pick.MIN_LEASE_S} to {phase_pick.MAX_LEASE_S}" in arg.kwargs["help"]


def test_the_worker_bounds_each_statement_past_the_server_s_and_the_heartbeat_s_whole_call():
    assert worker.STATEMENT_S == worker.STATEMENT_TIMEOUT_MS / 1000 + 1 == 6
    assert worker.STATEMENT_S < phase_pick.HEARTBEAT_CALL_S


def test_a_good_heartbeat_moves_the_clock_and_a_refused_one_stops_the_run_at_once():
    clock, leases = _Clock(), worker.Leases()
    run = worker.Running("ship.p1-1", 4242)
    leases.add(_claim(), clock(), run)
    for t in (110.0, 120.0, 130.0, 140.0, 150.0, 160.0):
        clock.t = t
        assert _pass(leases, clock, lambda claim: True) == []
    assert leases.snapshot()[0].last_ok == 160.0
    clock.t = 170.0
    assert _pass(leases, clock, lambda claim: False) == [run]
    assert leases.snapshot()[0].state == "lost"


def test_a_claim_not_yet_due_is_left_alone_and_one_the_main_loop_settled_is_not_stopped():
    clock, leases = _Clock(), worker.Leases()
    leases.add(_claim(), clock())
    calls: list = []
    clock.t = 105.0
    assert _pass(leases, clock, lambda claim: calls.append(claim) or True) == []
    assert calls == []
    assert leases.take(_claim()) is not None  # settled by the main loop while it was still held
    clock.t = 200.0
    assert _pass(leases, clock, _timed_out) == []


def test_a_slow_stop_of_one_lane_never_holds_up_the_other_lane_s_heartbeat():
    """Two lanes: p1's heartbeat is refused and its stop blocks until released; p2 renews on the same pass and on
    the next, while p1's stop is still running on its own thread."""
    import threading

    clock, leases = _Clock(), worker.Leases()
    slow, fast = worker.Running("ship.p1-1", 4241), worker.Running("ship.p2-1", 4242)
    leases.add(_claim("p1"), clock(), slow)
    leases.add(_claim("p2"), clock(), fast)
    release, entered = threading.Event(), threading.Event()

    def stop(run):
        entered.set()
        return release.wait(30)

    request = worker.stopper(stop, leases, lambda line: None)
    renewed: list[str] = []

    def heartbeat(claim):
        renewed.append(claim.phase)
        return claim.phase == "p2"

    clock.t = 110.0
    worker.beat(leases, heartbeat, request, clock, lambda line: None)
    assert entered.wait(5)  # p1's stop is running, and blocked
    clock.t = 120.0
    worker.beat(leases, heartbeat, request, clock, lambda line: None)
    assert renewed == ["p1", "p2", "p2"]
    states = {h.claim.phase: (h.state, h.stopped, h.last_ok) for h in leases.snapshot()}
    assert states == {"p1": ("lost", False, 100.0), "p2": ("held", False, 120.0)}
    release.set()
    for _ in range(100):
        if {h.claim.phase: h.stopped for h in leases.snapshot()}["p1"]:
            break
        threading.Event().wait(0.05)
    assert {h.claim.phase: h.stopped for h in leases.snapshot()} == {"p1": True, "p2": False}


def test_a_lanes_facts_are_the_phases_open_tasks_its_roll_ups_in_any_run_and_its_last_round():
    rows = [{"kind": "task", "task_id": "t2", "phase": "p1", "state": "ready"},
            {"kind": "task", "task_id": "t1", "phase": "p1", "state": "approved"},
            {"kind": "task", "task_id": "t0", "phase": "p1", "state": "dropped"},
            {"kind": "task", "task_id": "t9", "phase": "p2", "state": "ready"},
            {"kind": "initiative", "task_id": "ship", "phase": "", "state": "ready"}]
    rollups = [{"phase": "p1", "task_id": "t1"}, {"phase": "p1", "task_id": "t1"}, {"phase": "p2", "task_id": "t9"}]
    facts = worker.lane_facts(rows, rollups, "p1", (1, "revise"), [], "ship.p1-1")
    assert facts == worker.LaneFacts(("t1", "t2"), ("t1",), (1, "revise"), None)
    quarantined = worker.lane_facts(rows, rollups, "p1", (2, "quarantine"), [], "ship.p1-1", "goal missed")
    assert (quarantined.last_round, quarantined.reason) == ((2, "quarantine"), "goal missed")


def _smoke(run: str, ok: int, sha: str = "a1", reason: str = "", fault: bool = False) -> dict[str, object]:
    return {"run_id": run, "ok": ok, "verdict": "passed" if ok else "failed", "subject_sha": sha,
            "result_json": {"tip": sha, "reason": reason, "harness_fault": fault}}


def test_a_lanes_smoke_is_the_last_smoke_row_its_own_run_wrote_with_its_reason():
    failed, passed = _smoke("r1", 0, reason="the phase smoke failed: wiring"), _smoke("r1", 1)
    assert worker.lane_facts([], [], "p1", None, [passed, failed], "r1").smoke == phase_pick.SmokeSeen(
        False, "the phase smoke failed: wiring", 0)
    assert worker.lane_facts([], [], "p1", None, [failed, passed], "r1").smoke == phase_pick.SmokeSeen(
        True, "the phase smoke was passed", 0)
    assert worker.lane_facts([], [], "p1", None, [failed, passed], "r2").smoke is None  # another run's are not its own
    assert worker.lane_facts([], [], "p1", None, [{"run_id": "r1", "ok": 0, "result_json": None}], "r1").smoke == (
        phase_pick.SmokeSeen(False, "the phase smoke was not recorded", 0))


def test_a_harness_fault_counts_every_harness_fault_of_the_phase_at_that_tip():
    why = "the phase smoke failed: harness fault: check 'smoke' could not run"
    rows = [_smoke("r1", 0, "a1", why, fault=True), _smoke("r1", 0, "a0", why, fault=True),
            _smoke("r1", 0, "a1", "the phase smoke failed: wiring"), _smoke("r2", 0, "a1", why, fault=True)]
    assert worker.lane_facts([], [], "p1", None, rows, "r2").smoke == phase_pick.SmokeSeen(False, why, 2)
    assert worker.lane_facts([], [], "p1", None, rows[:1], "r1").smoke == phase_pick.SmokeSeen(False, why, 1)


def test_every_other_live_lane_of_the_phase_is_an_older_claims_and_is_stopped_before_the_new_one_starts():
    live = [{"run": "ship.p1-1", "pid": 11, "state": "running"}, {"run": "ship.p1-2", "pid": 12, "state": "fenced"},
            {"run": "ship.p1-3", "pid": 13, "state": "running"}]
    assert worker.stale_lanes(live, "ship.p1-3") == [worker.Running("ship.p1-1", 11), worker.Running("ship.p1-2", 12)]
    assert worker.stale_lanes([], "ship.p1-3") == []


def test_an_ended_initiative_review_is_judged_by_its_round_and_suite_and_a_phase_by_its_tasks():
    """D2: the claim's row kind picks the verdict; an initiative review has no tasks of its own to count."""
    from harness import initiative_review
    from harness.store_phases import Claim

    review = Claim("ship", initiative_review.QUEUE_PHASE, 2, "2026-11-20T10:01:00Z",
                   {"attempts": 1, "kind": initiative_review.KIND})
    passed = initiative_review.SuiteSeen(True, "a" * 40)
    approved = worker.lane_verdict(review, worker.LaneFacts((), (), (2, "approve"), None, "", passed))
    assert (approved.kind, approved.outcome) == ("ack", "approved")
    revised = worker.lane_verdict(review, worker.LaneFacts((), (), (1, "revise"), None, "dedupe"))
    assert (revised.kind, revised.outcome, revised.reason) == (
        "ack", "revised", "the initiative review's round 1 asked for revise-1: dedupe")
    # A build row is `phase_pick.lane_verdict`'s, as before: its approve round acks it; with no round and its task not
    # rolled up it is retried.
    build = worker.lane_verdict(_claim(), worker.LaneFacts(("t1",), (), (2, "approve"), None, "", passed))
    assert (build.kind, build.outcome) == ("ack", "approved")
    assert worker.lane_verdict(_claim(), worker.LaneFacts(("t1",), (), None, None)).kind == "retry"
