from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_tools.chair_exec import LAND_TIMEOUT_S, argv_for, local_timeout, perform
from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_report import lands_this_tick, would_items
from tests.test_chair_exec import _deps

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
QUEUE = {"batch_size": 3, "wait_s": 30.0}


def _task(task: str, phase: str) -> dict:
    return {"id": task, "initiative": "i", "repo": "r", "phase": phase, "phase_done": True, "needs": [], "run": "i-1", "needs_fetch": True}


def _facts(**extra) -> dict:
    approved = [_task("a", "p1"), _task("b", "p2"), _task("c", "p3")]
    return {"approved": approved, "initiatives": [{"id": "i", "started": True, "ready_tasks": [], "landed": set()}], **extra}


def _batch() -> dict:
    sources = [{"initiative": "i", "phase": p, "run": "i-1", "task_runs": {}} for p in ("p1", "p2", "p3")]
    return {"kind": "land_batch", "repo": "r", "sources": sources, "batch_size": 3, "wait_s": 30.0, "epoch": 1}


def test_three_ready_phases_plan_one_land_batch_with_three_sources():
    actions = plan_lands(_facts(merge_queue=QUEUE), T0)
    batches = [a for a in actions if a["kind"] == "land_batch"]
    assert [a["kind"] for a in actions if a["kind"] in ("land_batch", "land_phase")] == ["land_batch"]
    assert [s["phase"] for s in batches[0]["sources"]] == ["p1", "p2", "p3"]
    assert (batches[0]["repo"], batches[0]["batch_size"], batches[0]["wait_s"]) == ("r", 3, 30.0)


def test_the_queue_off_plans_the_three_land_phase_actions():
    assert [a["kind"] for a in plan_lands(_facts(), T0) if a["kind"] != "fetch"] == ["land_phase"] * 3


def test_a_phase_in_backoff_is_left_out_of_sources():
    refusal = {"initiative": "i", "phase": "p2", "run": "i-1", "attempts": 1, "last_refused_at": T0.isoformat()}
    actions = plan_lands(_facts(merge_queue=QUEUE, land_refusals=[refusal]), T0 + timedelta(minutes=5))
    batch = next(a for a in actions if a["kind"] == "land_batch")
    assert [s["phase"] for s in batch["sources"]] == ["p1", "p3"]


def test_argv_for_a_land_batch_holds_the_repo_the_sources_and_the_settings():
    assert argv_for(_batch(), label="chair") == [
        "cox", "runs", "land-batch", "--repo", "r", "--apply", "--no-claim", "--label", "chair",
        "--source", "i/p1/i-1", "--source", "i/p2/i-1", "--source", "i/p3/i-1", "--batch-size", "3", "--wait-s", "30.0",
    ]
    assert argv_for({**_batch(), "sources": []}) is None


def test_a_land_batch_may_run_for_the_land_bound_plus_its_wait():
    assert local_timeout(argv_for(_batch())) == LAND_TIMEOUT_S + 30.0


def test_two_landed_lines_and_one_returned_line_give_landed_and_one_needs_chair():
    output = "batch landed i/p1 pr=1\nbatch landed i/p2 pr=2\nbatch returned i/p3: failing checks: lint\nbatch summary: ci_runs=1 landed=2 returned=1 deferred=0\n"
    results = perform([_batch()], _deps([], output=output), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed", "escalated"]
    raised = results[1]["action"]
    assert (raised["kind"], raised["initiative"], raised["phase"], raised["cause"]) == ("needs_chair", "i", "p3", "checks")
    assert raised["reason"] == "failing checks: lint"
    assert lands_this_tick(results) == 1


def _phases(results: list) -> list[str]:
    return [r["action"].get("phase", "") for r in results if r["status"] == "escalated"]


def test_a_timed_out_land_batch_is_failed_and_escalates_every_source():
    results = perform([_batch()], _deps([], output="", code=124), lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "escalated", "escalated", "escalated"]
    assert _phases(results) == ["p1", "p2", "p3"]
    assert "timed out" in results[1]["action"]["reason"]


def test_a_crashed_land_batch_with_no_batch_lines_is_failed_and_escalates_every_source():
    results = perform([_batch()], _deps([], output="Traceback: boom\n", code=2), lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "escalated", "escalated", "escalated"]
    assert results[1]["action"]["reason"] == "Traceback: boom\n"


def test_an_exit_zero_with_no_batch_lines_is_not_a_success():
    results = perform([_batch()], _deps([], output="", code=0), lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "escalated", "escalated", "escalated"]


def test_a_land_batch_with_no_sources_is_refused_and_escalated_once():
    results = perform([{**_batch(), "sources": []}], _deps([]), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated"]
    assert results[1]["action"]["kind"] == "needs_chair"


def test_a_busy_land_batch_is_not_escalated():
    results = perform([_batch()], _deps([], output="land: refusing, r is landing in another run\n", code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["busy"]


class _Sink:
    """A land sink that runs each submitted land at once; it has no `start`, so every land goes to `submit`."""

    def full(self, repo):
        return False

    def pending(self, repo):
        return False

    def landing(self):
        return []

    def submit(self, action, work):
        return work()

    def collect(self):
        return []


def test_a_land_batch_handed_to_a_sink_runs_the_batch_command_never_the_phase_command():
    calls: list = []
    deps = replace(_deps(calls, output="batch landed i/p1 pr=1\n"), lands=_Sink())
    results = perform([_batch()], deps, lambda: 1, False)
    argvs = [c[1] for c in calls if c[0] == "run"]
    assert [a[:3] for a in argvs] == [["cox", "runs", "land-batch"]]
    assert "--phase" not in argvs[0]
    assert [r["status"] for r in results] == ["landed"]


def test_only_deferred_sources_record_not_landed_without_escalation():
    results = perform([_batch()], _deps([], output="batch deferred i/p1\nbatch summary: ci_runs=0 landed=0 returned=0 deferred=1\n"), lambda: 1, False)
    assert [r["status"] for r in results] == ["not_landed"]


def test_a_dry_run_names_the_land_batch_as_its_repo():
    assert would_items(perform([_batch()], _deps([]), lambda: 1, True)) == ["land_batch:r"]
