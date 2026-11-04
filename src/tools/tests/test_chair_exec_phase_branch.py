from agent_tools import chair_exec
from agent_tools.chair_exec import LAND_CONFLICT_CAUSE, Deps, perform

LAND = {"kind": "land_phase", "phase": "p1", "repo": "r", "run": "run-1", "initiative": "A", "epoch": 1}
NOT_FETCHED = "phase branch not fetched from host-a"


def _deps(output: str) -> Deps:
    return Deps(
        run=lambda argv: (1, output),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "run-1",
        repo_for=lambda action: action.get("repo", "r"),
        rev_parse=lambda repo, ref: None,
    )


def _stop(step: str, detail: str) -> str:
    return f"merge: ok\nland: stopped at {step}: {detail}\n"


def _escalated(output: str) -> dict:
    """The escalated result for a phase land whose command exits 1 with `output`, as the loop's `_escalate` makes it."""
    deps = _deps(output)
    return chair_exec._escalate(LAND, chair_exec._land_phase(LAND, deps, {}))


def test_a_fetch_phase_branch_stop_escalates_with_the_named_reason() -> None:
    escalated = _escalated(_stop("fetch_phase_branch", NOT_FETCHED))
    assert escalated["status"] == "escalated"
    assert NOT_FETCHED in escalated["reason"]
    assert escalated["action"]["kind"] == "needs_chair"


def test_a_fetch_phase_branch_stop_is_not_a_land_conflict() -> None:
    escalated = _escalated(_stop("fetch_phase_branch", NOT_FETCHED))
    assert escalated["action"]["cause"] != LAND_CONFLICT_CAUSE
    assert chair_exec._LAND_CONFLICT_ROUTE not in escalated["reason"]


def test_a_squash_phase_stop_is_still_a_land_conflict() -> None:
    escalated = _escalated(_stop("squash_phase", "conflict in a.py"))
    assert escalated["status"] == "escalated"
    assert escalated["action"]["cause"] == LAND_CONFLICT_CAUSE


def test_perform_ends_a_fetch_phase_branch_stop_with_the_named_escalation() -> None:
    results = perform([LAND], _deps(_stop("fetch_phase_branch", NOT_FETCHED)), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated"]
    assert NOT_FETCHED in results[-1]["reason"]
    assert results[-1]["action"]["cause"] != LAND_CONFLICT_CAUSE
