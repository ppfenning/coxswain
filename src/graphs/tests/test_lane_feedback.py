"""E1 finding 1: a task the phase lane builds again is told why its last attempt was refused. No mocks: the pure
feedback, the real step rows, a real store, and the lane's own build (`phase_lane._build`) running the real lifecycle
graph under the scripted runner, whose recorded prompts are what the builder was given."""

from __future__ import annotations

import pytest

from graphs._spec import GraphSpec
from graphs.delivery import lifecycle_propose
from harness import lane_feedback, phase_lane, phase_pick, review_loop, store_phases
from harness.epic import Ctx
from harness.phase_lane import Fence, Lane, LaneClaim
from harness.store_migrate import open_store
from harness.store_steps import put_round, put_step, round_row, steps_of_result
from runner import ScriptedRunner
from tests.conftest import CARTRIDGE

AT = "2026-11-21T00:00:00Z"
MISSING = "The test never asserts what happens to the trailing comment on the edited local_lanes line."
OTHER = "No test covers `--force` on a file that already exists."
PLAN = {"steps": ["keep the comments", "test it"], "files_expected": ["src/a.py"], "out_of_scope": []}
BUILD = {"patch": "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+new line\n", "summary": "kept the comments",
         "files_touched": ["src/a.py"], "commands_run": [{"command": "pytest -q tests/test_a.py", "output": "3 passed"}]}
REFUSED = {"complete": False, "blocking": False, "missing": [MISSING, OTHER], "brief": "evidence is missing"}
COMPLETE = {"complete": True, "blocking": False, "missing": [], "brief": "ready"}


def _row(run_id: str, kind: str, result: dict, at: str) -> dict:
    return {"run_id": run_id, "step": f"task/t1/{kind}", "kind": kind, "result_json": result, "finished_at": at}


def _checks(*checks: tuple[str, bool, int, str]) -> dict:
    return {"checks": [{"name": n, "passed": p, "exit_code": e, "outcome": o} for n, p, e, o in checks]}


# -- the pure feedback ------------------------------------------------------------------------------------------------

def test_a_refused_handoff_s_missing_items_reach_the_feedback_verbatim_in_the_fix_loop_s_words():
    text = lane_feedback.refusal_feedback([_row("r1", "handoff", {"complete": False, "missing": [MISSING]}, AT)])
    assert text.startswith(lane_feedback.HEADING) and "(run r1)" in text
    assert f"- finding (handoff evidence) in : {MISSING}" in text  # `_critique` of `_handoff_critique`, as in one run


def test_only_the_latest_refusing_run_counts_and_a_harness_fault_is_not_a_refusal():
    rows = [
        _row("r1", "handoff", {"complete": False, "missing": [OTHER]}, "2026-11-21T09:09:00Z"),
        _row("r2", "checks", _checks(("lint", False, 1, "failed"), ("unit", True, 0, "passed")), "2026-11-21T09:14:00Z"),
        _row("r3", "checks", _checks(("cd src/tools && pytest -q -n auto", False, None, "timed_out")), "2026-11-21T09:32:00Z"),
    ]
    text = lane_feedback.refusal_feedback(rows)
    assert "(run r2)" in text and "- check `lint` failed (exit 1)" in text
    assert OTHER not in text and "unit" not in text and "timed" not in text  # older, passing, and a fault: none of them
    # A timeout alone refused nothing the builder can answer, so the handoff before it is still the last word.
    assert "(run r1)" in lane_feedback.refusal_feedback([rows[0], rows[2]])


def test_nothing_refused_gives_no_feedback_and_the_body_stays_as_it_was():
    complete = _row("r1", "handoff", {"complete": True, "missing": []}, AT)
    passed = _row("r1", "checks", _checks(("lint", True, 0, "passed")), AT)
    assert lane_feedback.refusal_feedback([complete, passed]) == "" == lane_feedback.refusal_feedback([])
    assert lane_feedback.with_feedback("the body", "") == "the body"
    assert lane_feedback.with_feedback("the body", "why") == "the body\n\nwhy"


# -- the step row ------------------------------------------------------------------------------------------------------

def test_a_build_only_result_records_its_handoff_and_an_old_model_result_does_not():
    result = {"run_id": "r1:t1", "build_only": True, "plan": PLAN, "build": BUILD, "handoff": REFUSED,
              "build_stopped": "handoff incomplete"}
    rows = steps_of_result(result, run_id="r1", initiative="ship", phase="p1", task="t1", at=AT)
    handoff = next(r for r in rows if r["kind"] == "handoff")
    assert (handoff["step"], handoff["ok"], handoff["verdict"]) == ("task/t1/handoff", 0, "incomplete")
    assert handoff["result_json"] == {"complete": False, "blocking": False, "missing": [MISSING, OTHER]}
    old = steps_of_result({**result, "build_only": False}, run_id="r1", initiative="ship", phase="p1", task="t1", at=AT)
    assert "handoff" not in [r["kind"] for r in old]


# -- the store and the lane --------------------------------------------------------------------------------------------

@pytest.fixture
def lane_store(store_url, tmp_path):
    return f"sqlite:///{tmp_path / 'cox.db'}" if store_url.startswith("sqlite") else store_url


def _claim(conn) -> LaneClaim:
    store_phases.apply_changes(conn, [{"op": "insert", "initiative": "ship", "phase": "p1", "requires": []}], "chair")
    claim = store_phases.claim_next(conn, "w1", [], 1, lease_s=phase_pick.LEASE_S)
    assert claim is not None
    return LaneClaim("ship", "p1", "w1", claim.epoch)


def _lane(tmp_path, url: str, conn, claim: LaneClaim, run_id: str, runner: ScriptedRunner, log: list[str]) -> Lane:
    cartridge = {**CARTRIDGE, "skills": {**CARTRIDGE["skills"], "handoff": "acme-skills:handoff"}}
    ctx = Ctx(repo=tmp_path, cartridge=cartridge, runner=runner,
              specs={"lifecycle": GraphSpec(name="lifecycle", graph_name="lifecycle-propose", run=lifecycle_propose.run)},
              run_id=run_id, date="2026-11-21", max_parallel=1, ledger_path=tmp_path / "ledger", provider_profile="p",
              runs_dir=tmp_path / "runs", worktree_root=tmp_path / "wt", assume=None, fix_attempts=None,
              initiative_id="ship", default_ref="main", check_levels=True)
    item = {"id": "t1", "title": "Keep the file's comments", "body": "Rewrite the file and keep its comments.",
            "phase": "p1", "budget_usd": 1.0}
    return Lane(ctx=ctx, claim=claim, conn=conn, fence=Fence(lambda: open_store(url, AT), claim, log.append, lambda: None),
                phase_worktree=tmp_path / "phase", branch="epic/ship/p1", base_sha="", items={"t1": item},
                log=log.append, open_conn=lambda: open_store(url, AT))


def test_a_task_whose_last_build_s_handoff_was_refused_is_built_again_with_the_reasons(tmp_path, lane_store):
    conn = open_store(lane_store, AT)
    try:
        claim, log = _claim(conn), []
        first = ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": REFUSED})
        result, why, _, _ = phase_lane._build(_lane(tmp_path, lane_store, conn, claim, "ship-p1-1", first, log), "t1", "")
        assert result is None and why.startswith("handoff incomplete")
        assert lane_feedback.HEADING not in next(c["prompt"] for c in first.calls if c["role"] == "build")

        again = ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": COMPLETE})
        result, why, _, _ = phase_lane._build(_lane(tmp_path, lane_store, conn, claim, "ship-p1-2", again, log), "t1", "")
        assert result is not None and why == ""
        for role in ("plan", "build"):  # the ticket body both read carries the refusal, each missing item verbatim
            prompt = next(c["prompt"] for c in again.calls if c["role"] == role)
            assert lane_feedback.HEADING in prompt and "(run ship-p1-1)" in prompt
            assert MISSING in prompt and OTHER in prompt
        assert "lane: t1 is built again with why its last attempt was refused" in log
    finally:
        conn.close()


def test_a_refusal_from_before_the_phase_was_reopened_is_not_this_episode_s(tmp_path, lane_store):
    conn = open_store(lane_store, AT)
    try:
        result = {"run_id": "old:t1", "build_only": True, "plan": PLAN, "build": BUILD, "handoff": REFUSED}
        for row in steps_of_result(result, run_id="old", initiative="ship", phase="p1", task="t1",
                                   at="2026-11-20T00:00:00Z"):
            put_step(conn, row)
        assert [r["kind"] for r in store_phases.task_refusals(conn, "ship", "p1", "t1")] == ["handoff"]
        assert store_phases.task_refusals(conn, "ship", "p1", "t2") == []
        put_round(conn, round_row(initiative="ship", phase="p1", round_=1, verdict=review_loop.REOPENED,
                                  reason="reopened", worker="chair", decided_at="2026-11-20T12:00:00Z"))
        assert store_phases.task_refusals(conn, "ship", "p1", "t1") == []
    finally:
        conn.close()
