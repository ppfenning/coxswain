"""The phase worker's pure rules: lanes, capabilities, repository tokens, the launch, and `cox worker status` lines.
The loop itself is glue, proven against a real store by smoke/test_worker_claims.py."""

from agent_tools import worker, worker_cli

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


def test_the_launch_is_today_s_per_phase_launch_under_the_loop_s_label():
    assert worker.launch_argv("ship", "p1", "ship.p1-1", "chair-x") == [
        "cox", "route", "launch", "epic", "--local", "--initiative", "work/ship", "--phase", "p1", "--run-id", "ship.p1-1",
        "--label", "chair-x", "--no-claim",
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
        "ship/p2: claimed, epoch 1, 1 attempt(s), needs []; lapsed (was w2), claimable",
        "ship/p3: ready, epoch 0, 1 attempt(s), needs [os:macos]; claimable from 2026-11-20T10:01:00Z",
        "",
        f"worker w1: up, 1 lane(s), [docker], beat {NOW}",
    ]
    assert worker_cli.status_lines([], [], NOW) == ["the phase queue is empty", "", "no worker has registered"]


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
    stopped: list = []
    worker.beat(leases, heartbeat, lambda run: stopped.append(run) or True, clock, lambda line: None)
    return stopped


def _timed_out(claim):
    raise TimeoutError("statement timeout")


def test_a_claim_with_no_good_heartbeat_for_45_seconds_stops_its_run_before_its_lease_can_lapse():
    clock, leases = _Clock(), worker.Leases()
    run = worker.Running("ship.p1-1", 4242)
    leases.add(_claim(), clock())  # the clock starts when the worker set out to claim
    leases.set_run(_claim(), run)
    clock.t = 110.0
    assert _pass(leases, clock, _timed_out) == []
    clock.t = 144.9
    assert _pass(leases, clock, _timed_out) == []
    clock.t = 145.0  # 45 s after the claim began, 15 s before the 60 s lease the store gave it
    assert _pass(leases, clock, _timed_out) == [run]
    (held,) = leases.snapshot()
    assert (held.state, held.stopped) == ("expired", True)
    assert leases.take(_claim()) is None  # the main loop writes nothing for it


def test_a_good_heartbeat_moves_the_clock_and_a_refused_one_stops_the_run_at_once():
    clock, leases = _Clock(), worker.Leases()
    run = worker.Running("ship.p1-1", 4242)
    leases.add(_claim(), clock())
    leases.set_run(_claim(), run)
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
