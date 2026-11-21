"""E1 finding 1: a task the phase lane builds again is told why its last attempt was refused. No mocks: the pure
feedback, the real step rows, a real store, and the lane's own build (`phase_lane._build`) running the real lifecycle
graph under the scripted runner, whose recorded prompts are what the builder was given."""

from __future__ import annotations

import subprocess

import pytest

from graphs._spec import GraphSpec
from graphs.delivery import lifecycle_propose
from harness import lane_feedback, phase_lane, phase_pick, review_loop, store_phases
from harness.epic import Ctx
from harness.phase_lane import Fence, Lane, LaneClaim
from harness.store_migrate import open_store
from harness.store_steps import put_round, put_step, round_row, step_row, steps_of_result, with_checks
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


def test_a_refusal_a_later_complete_handoff_answered_is_not_replayed_after_a_timeout():
    rows = [
        _row("r1", "handoff", {"complete": False, "missing": [MISSING]}, "2026-11-21T09:09:00Z"),
        _row("r2", "handoff", {"complete": True, "missing": []}, "2026-11-21T09:20:00Z"),
        _row("r2", "checks", _checks(("cd src/tools && pytest -q -n auto", False, None, "timed_out")), "2026-11-21T09:20:00Z"),
    ]
    assert lane_feedback.refusal_feedback(rows) == ""
    # In one store second an attempt's checks still follow its handoff: a failure there is the latest verdict.
    failed = _row("r2", "checks", _checks(("lint", False, 1, "failed")), "2026-11-21T09:20:00Z")
    assert "- check `lint` failed" in lane_feedback.refusal_feedback([*rows[:2], failed])
    assert MISSING not in lane_feedback.refusal_feedback([*rows[:2], failed])


def test_nothing_refused_gives_no_feedback_and_the_body_stays_as_it_was():
    complete = _row("r1", "handoff", {"complete": True, "missing": []}, AT)
    passed = _row("r1", "checks", _checks(("lint", True, 0, "passed")), AT)
    assert lane_feedback.refusal_feedback([complete, passed]) == "" == lane_feedback.refusal_feedback([])
    assert lane_feedback.with_feedback("the body", "") == "the body"
    assert lane_feedback.with_feedback("the body", "why") == "the body\n\nwhy"


# -- a failed check's output -------------------------------------------------------------------------------------------

TYPES = "python .github/check_types.py"
PYRIGHT = ("src/graphs/harness/phase_lane.py:812:9 - error: Argument of type \"str | None\" cannot be assigned to "
           "parameter \"base\" of type \"str\"", "src/graphs/harness/lane_feedback.py:40:5 - error: \"_mapping\" is "
           "not defined", "2 errors, 0 warnings, 0 informations")


def _types_check(noise: int = 200) -> dict:
    """A failed `check_types` result as `checks.run_checks` keeps it: a long run of progress lines, then pyright's
    errors, in both its output tail and its excerpt (the last lines, for output that is not pytest's)."""
    output = "\n".join([*(f"checking module {n} of {noise}" for n in range(noise)), *PYRIGHT])
    return {"name": TYPES, "cmd": TYPES, "passed": False, "exit_code": 1, "outcome": "failed",
            "output_tail": output[-2000:], "excerpt": "\n".join(output.splitlines()[-40:]), "log": "checks/p1/t1/1.log"}


def _checks_row(checks: list[dict], run_id: str = "r1") -> dict:
    result = with_checks({"run_id": f"{run_id}:t1"}, checks, run_id)
    return steps_of_result(result, run_id=run_id, initiative="ship", phase="p1", task="t1", at=AT)[0]


def test_a_failed_check_s_errors_reach_the_feedback_from_its_row_capped():
    row = _checks_row([_types_check()])
    kept = row["result_json"]["checks"][0]["output_tail"]
    assert kept.splitlines()[-3:] == list(PYRIGHT) and len(kept) <= 2000 and len(kept.splitlines()) <= 40
    text = lane_feedback.refusal_feedback([row])
    assert f"- check `{TYPES}` failed (exit 1)" in text and all(line in text for line in PYRIGHT)
    assert "checking module 0 of" not in text  # the end of the output, never the whole log


def test_only_three_failed_checks_carry_their_output_and_a_passed_or_timed_out_one_none():
    many = [{**_types_check(5), "name": f"types-{n}", "excerpt": f"error in check {n}"} for n in range(4)]
    timed_out = {"name": "suite", "passed": False, "exit_code": None, "outcome": "timed_out", "excerpt": "[timed out]"}
    passed = {"name": "lint", "passed": True, "exit_code": 0, "outcome": "passed", "excerpt": "All checks passed!"}
    row = _checks_row([passed, timed_out, *many])
    assert ["output_tail" in c for c in row["result_json"]["checks"]] == [False, False, True, True, True, False]
    text = lane_feedback.refusal_feedback([row])
    assert all(f"error in check {n}" in text for n in range(3)) and "error in check 3" not in text
    assert "- check `types-3` failed (exit 1)" in text  # still named, without its output


TOKEN = "s3cr3t-forgejo-token-value"
STORE_URL = "postgresql://cox:hunter22pw@localhost:5432/cox"
SCRUBBED_URL = "postgresql://***@localhost:5432/cox"


def _secret_check() -> dict:
    """A failed check whose output printed a token from the lane's environment and a store URL with its password."""
    return {**_types_check(5), "excerpt": f"auth failed with {TOKEN}\nconnecting to {STORE_URL}\n{PYRIGHT[0]}"}


def test_with_no_artifacts_root_a_check_s_tail_is_scrubbed_before_the_store_and_the_prompt(
        tmp_path, lane_store, monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", TOKEN)  # the lane's real environment, which `tail_secrets` reads
    conn = open_store(lane_store, AT)
    try:
        claim, log = _claim(conn), []
        first = _lane(tmp_path, lane_store, conn, claim, "ship-p1-1",
                      ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": COMPLETE}), log)
        assert first.ctx.artifacts is None  # the default: nothing is uploaded, so nothing is marked
        result, _, _, _ = phase_lane._build(first, "t1", _base(tmp_path))
        assert result is not None
        rows = phase_lane._rows_of(first, with_checks(result, [_secret_check()], "ship-p1-1"), "t1", "")
        assert TOKEN in next(r for r in rows if r["kind"] == "checks")["result_json"]["checks"][0]["output_tail"]
        assert first.put(rows)
        (stored,) = [r for r in store_phases.task_attempt_rows(conn, "ship", "p1", "t1") if r["kind"] == "checks"]
        tail = stored["result_json"]["checks"][0]["output_tail"]
        assert "auth failed with ***" in tail and SCRUBBED_URL in tail and PYRIGHT[0] in tail
        assert TOKEN not in tail and "hunter22pw" not in tail

        again = ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": COMPLETE})
        phase_lane._build(_lane(tmp_path, lane_store, conn, claim, "ship-p1-2", again, log), "t1", _base(tmp_path))
        prompt = next(c["prompt"] for c in again.calls if c["role"] == "build")
        assert PYRIGHT[0] in prompt and SCRUBBED_URL in prompt
        assert TOKEN not in prompt and "hunter22pw" not in prompt
    finally:
        conn.close()


def test_with_a_root_the_uploaded_log_and_the_row_s_tail_are_scrubbed_alike(tmp_path, lane_store):
    pytest.importorskip("pyarrow")
    from harness.artifacts import artifacts_root, step_artifacts

    check = _secret_check()
    log = tmp_path / "runs" / "r1" / "checks" / "p1" / "t1" / "1.log"
    log.parent.mkdir(parents=True)
    log.write_text(check["excerpt"] + "\n", encoding="utf-8")
    root = artifacts_root({"artifacts_url": f"file://{tmp_path / 'artifacts'}"}, {"FORGEJO_TOKEN": TOKEN},
                          tmp_path / "runs")
    result = with_checks({"run_id": "r1:t1"}, [check], "r1")
    rows = steps_of_result(result, run_id="r1", initiative="ship", phase="p1", task="t1", at=AT)
    conn = open_store(lane_store, AT)
    try:
        (stored,) = step_artifacts(root, conn, rows, result, runs_dir=tmp_path / "runs", run_id="r1", phase="p1",
                                   task="t1")
    finally:
        conn.close()
    assert stored["artifacts_json"][0]["scrubbed"] is True
    text = lane_feedback.refusal_feedback([stored])
    assert PYRIGHT[0] in text and "auth failed with ***" in text and SCRUBBED_URL in text
    assert TOKEN not in text and "hunter22pw" not in text


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


def _base(tmp_path) -> str:
    """The commit a task builds on: `tmp_path/app`'s one commit, made the first time it is asked for. A build reads a
    worktree at its base (`phase_lane.builder_runner`), so the checkout is a real repository."""
    repo = tmp_path / "app"
    if not repo.exists():
        repo.mkdir()
        (repo / "README.md").write_text("app\n", encoding="utf-8")
        for args in (["init", "-q", "-b", "main"], ["add", "README.md"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _lane(tmp_path, url: str, conn, claim: LaneClaim, run_id: str, runner: ScriptedRunner, log: list[str]) -> Lane:
    cartridge = {**CARTRIDGE, "skills": {**CARTRIDGE["skills"], "handoff": "acme-skills:handoff"}}
    _base(tmp_path)
    ctx = Ctx(repo=tmp_path / "app", cartridge=cartridge, runner=runner,
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
        result, why, _, _ = phase_lane._build(_lane(tmp_path, lane_store, conn, claim, "ship-p1-1", first, log), "t1", _base(tmp_path))
        assert result is None and why.startswith("handoff incomplete")
        assert lane_feedback.HEADING not in next(c["prompt"] for c in first.calls if c["role"] == "build")

        again = ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": COMPLETE})
        second = _lane(tmp_path, lane_store, conn, claim, "ship-p1-2", again, log)
        result, why, _, _ = phase_lane._build(second, "t1", _base(tmp_path))
        assert result is not None and why == ""
        for role in ("plan", "build"):  # the ticket body both read carries the refusal, each missing item verbatim
            prompt = next(c["prompt"] for c in again.calls if c["role"] == role)
            assert lane_feedback.HEADING in prompt and "(run ship-p1-1)" in prompt
            assert MISSING in prompt and OTHER in prompt
        assert "lane: t1 is built again with why its last attempt was refused" in log

        # The second attempt's handoff passed, then its suite timed out (a harness fault, as in E1's p1-3): the third
        # build is not told the first attempt's complaint again, which the second already answered.
        timed_out = [{"name": "cd src/tools && pytest -q -n auto", "passed": False, "exit_code": None,
                      "outcome": "timed_out"}]
        assert second.put(phase_lane._rows_of(second, with_checks(result, timed_out, "ship-p1-2"), "t1", ""))
        third = ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": COMPLETE})
        phase_lane._build(_lane(tmp_path, lane_store, conn, claim, "ship-p1-3", third, log), "t1", _base(tmp_path))
        assert all(lane_feedback.HEADING not in c["prompt"] and MISSING not in c["prompt"] for c in third.calls)
    finally:
        conn.close()


def test_a_task_its_checks_refused_is_built_again_with_the_check_s_errors(tmp_path, lane_store):
    conn = open_store(lane_store, AT)
    try:
        claim, log = _claim(conn), []
        first = _lane(tmp_path, lane_store, conn, claim, "ship-p1-1",
                      ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": COMPLETE}), log)
        result, why, _, _ = phase_lane._build(first, "t1", _base(tmp_path))
        assert result is not None and why == ""
        # The handoff passed and `check_types` refused the change, as in E1: the lane writes the checks row.
        assert first.put(phase_lane._rows_of(first, with_checks(result, [_types_check()], "ship-p1-1"), "t1", ""))

        again = ScriptedRunner({"plan": PLAN, "build": BUILD, "handoff": COMPLETE})
        phase_lane._build(_lane(tmp_path, lane_store, conn, claim, "ship-p1-2", again, log), "t1", _base(tmp_path))
        prompt = next(c["prompt"] for c in again.calls if c["role"] == "build")
        assert lane_feedback.HEADING in prompt and f"- check `{TYPES}` failed (exit 1)" in prompt
        assert all(line in prompt for line in PYRIGHT) and "checking module 0 of" not in prompt
    finally:
        conn.close()


def test_a_refusal_from_before_the_phase_was_reopened_is_not_this_episode_s(tmp_path, lane_store):
    conn = open_store(lane_store, AT)
    try:
        result = {"run_id": "old:t1", "build_only": True, "plan": PLAN, "build": BUILD, "handoff": REFUSED}
        for row in steps_of_result(result, run_id="old", initiative="ship", phase="p1", task="t1",
                                   at="2026-11-20T00:00:00Z"):
            put_step(conn, row)
        assert [r["kind"] for r in store_phases.task_attempt_rows(conn, "ship", "p1", "t1")] == ["handoff"]
        assert store_phases.task_attempt_rows(conn, "ship", "p1", "t2") == []
        put_round(conn, round_row(initiative="ship", phase="p1", round_=1, verdict=review_loop.REOPENED,
                                  reason="reopened", worker="chair", decided_at="2026-11-20T12:00:00Z"))
        assert store_phases.task_attempt_rows(conn, "ship", "p1", "t1") == []
    finally:
        conn.close()


def test_a_revision_s_rows_of_one_run_say_why_it_did_not_roll_up(tmp_path, lane_store):
    conn = open_store(lane_store, AT)
    try:
        why = "node 'build' patch_outside_scratch: the reported patch is not in the scratch tree and does not apply to it"
        for run, error in (("ship.p1-8", why), ("ship.p1-9", "another run's")):
            put_step(conn, {**steps_of_result({"run_id": f"{run}:p1-rev-1", "build_only": True, "plan": PLAN},
                                              run_id=run, initiative="ship", phase="p1", task="p1-rev-1", at=AT)[0]})
            put_step(conn, step_row(
                run_id=run, step="task/p1-rev-1/build/0", initiative="ship", phase="p1", task_id="p1-rev-1",
                kind="build", round_=0, ok=False, verdict="failed", result={"error": error}, finished_at=AT))
        rows = store_phases.task_run_rows(conn, "ship", "p1", "p1-rev-1", "ship.p1-8")
        assert sorted(r["kind"] for r in rows) == ["build", "plan"]
        assert phase_pick.revision_failure(rows) == f"its build failed: {why}"
        assert store_phases.task_run_rows(conn, "ship", "p1", "p1-rev-1", "ship.p1-7") == []
    finally:
        conn.close()
