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
