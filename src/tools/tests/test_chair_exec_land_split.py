from dataclasses import replace

from agent_tools import chair_exec
from agent_tools.chair_exec import Blocker, Deps, land_command, land_outcome, runs_as_process


def _deps(**over) -> Deps:
    base = Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "run-1",
        repo_for=lambda action: action.get("repo", "r"),
        rev_parse=lambda repo, ref: None,
    )
    return replace(base, **over)


LAND = {"kind": "land", "task_id": "t1", "repo": "r", "run": "run-1", "epoch": 1}
PHASE = {"kind": "land_phase", "initiative": "alpha", "phase": "p1", "repo": "r", "run": "run-1", "epoch": 1}


def test_outcome_timed_out_is_a_failure_naming_the_command_and_the_bound() -> None:
    result = land_outcome(LAND, _deps(), chair_exec.TIMED_OUT, "")
    assert result["status"] == "failed"
    assert result["reason"].endswith("timed out after 45 min")
    assert result["reason"].startswith("cox runs land run-1 --task t1")


def test_outcome_repo_busy_is_busy_even_on_a_non_zero_exit() -> None:
    output = "land: refusing, run-2 is landing in r\n"
    assert land_outcome(LAND, _deps(), 1, output)["status"] == "busy"


def test_outcome_refusal_carries_needs_chair() -> None:
    result = land_outcome(LAND, _deps(), 1, "boom")
    assert result["status"] == "refused"
    assert result["reason"] == "boom"
    assert result["needs_chair"]["kind"] == "needs_chair"


def test_outcome_landed_carries_the_commit(monkeypatch) -> None:
    monkeypatch.setattr(chair_exec, "land_commit", lambda repo: "abc123")
    result = land_outcome(LAND, _deps(), 0, "merge: ok\nmark_done: ok\n")
    assert (result["status"], result["commit"]) == ("landed", "abc123")


def test_outcome_land_phase_with_every_task_done_and_the_branch_gone_is_already_landed() -> None:
    output = "land: stopped at squash_phase: fatal: invalid reference: epic/alpha/p1\n"
    deps = _deps(phase_states=lambda initiative, phase: ["done", "done"])
    result = land_outcome(PHASE, deps, 1, output)
    assert (result["status"], result["already_landed"]) == ("recorded", True)
    assert result["reason"] == "land phase p1 already landed: fatal: invalid reference: epic/alpha/p1"


def test_command_with_no_argv_is_refused_with_the_kind_s_reason() -> None:
    assert land_command({**LAND, "task_id": ""}, _deps(), {})["reason"] == "land needs a run id, a task_id and a repo"
    assert land_command({**PHASE, "phase": ""}, _deps(), {})["reason"] == "land_phase needs a run id, a phase and a repo"


def test_command_for_a_blocked_repo_is_skipped() -> None:
    blocked = {"r": Blocker(label="land t0")}
    result = land_command(LAND, _deps(), blocked)
    assert result["status"] == "skipped"
    assert result["reason"] == "an earlier land in r (land t0) was not counted"


def test_command_for_a_land_returns_the_action_unchanged_with_its_argv() -> None:
    action, argv = land_command(LAND, _deps(label="chair-1"), {})
    assert action == LAND
    assert argv[:6] == ["cox", "runs", "land", "run-1", "--task", "t1"]
    assert argv[-2:] == ["--label", "chair-1"]


def test_command_for_a_land_phase_applies_the_head() -> None:
    deps = _deps(rev_parse=lambda repo, ref: "f" * 40 if ref == "epic/alpha/p1" else None)
    action, argv = land_command(PHASE, deps, {})
    assert action == {**PHASE, "head": "f" * 40}
    assert "--phase" in argv


def test_runs_as_process_for_land_and_a_land_phase_without_task_runs() -> None:
    assert runs_as_process(LAND)
    assert runs_as_process(PHASE)


def test_runs_as_process_is_false_for_partial_land_and_a_land_phase_with_task_runs() -> None:
    assert not runs_as_process({**LAND, "kind": "partial_land", "task_ids": ["t1"]})
    assert not runs_as_process({**PHASE, "task_runs": {"t1": "run-2"}})
