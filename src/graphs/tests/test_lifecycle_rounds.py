"""lifecycle-propose records one summary per handoff or review round, in order."""

from __future__ import annotations

from graphs.delivery import lifecycle_propose
from graphs.delivery.lifecycle_propose import round_summary
from runner import ScriptedRunner

REVISE = {
    "verdict": "revise",
    "findings": [{"charter_principle": "A3", "detail": "mutates its argument", "file": "src/a.py"}],
    "rationale": "the argument is mutated",
}
APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}


def test_a_handoff_critique_summarises_as_a_handoff_round_with_no_objections() -> None:
    review, adversary, arbitration, verdict, _, _ = lifecycle_propose._handoff_critique(
        {"complete": False, "missing": ["a pytest run"], "brief": "supply it"}
    )
    assert round_summary(1, "handoff", review, adversary, arbitration, verdict) == {
        "attempt": 1,
        "source": "handoff",
        "verdict": "revise",
        "findings": ["handoff evidence"],
        "objections": 0,
        "arbitration": None,
    }


def test_a_review_round_with_two_objections_and_an_arbitration_summarises_to_the_literal_dict() -> None:
    adversary = {"verdict": "revise", "objections": [{"claim": "one"}, {"claim": "two"}], "strongest_objection": "one"}
    arbitration = {"verdict": "revise", "sided_with": "adversary", "reasoning": "the objection holds"}
    assert round_summary(2, "review", REVISE, adversary, arbitration, "revise") == {
        "attempt": 2,
        "source": "review",
        "verdict": "revise",
        "findings": ["A3"],
        "objections": 2,
        "arbitration": "revise",
    }


def test_a_run_revised_once_then_approved_records_two_rounds_in_order(
    cartridge, plan_response, build_response
) -> None:
    second = {**build_response, "patch": "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+a different fix\n+with its own test\n"}
    scripted = ScriptedRunner(
        {"plan": plan_response, "build": [build_response, second], "review_charter": [REVISE, APPROVE]}
    )
    result = lifecycle_propose.run(
        {"run_id": "run-1", "date": "2026-09-26", "ticket": "TICKET-1", "cartridge": cartridge}, scripted
    )
    assert [(r["attempt"], r["source"], r["verdict"]) for r in result["fix_loop"]["rounds"]] == [
        (1, "review", "revise"),
        (2, "review", "approve"),
    ]


# Assembled at runtime so this file passes the clean check it exercises.
HOME_LITERAL = "/ho" + "me/" + "someone"
HIT_LINE = f"tests/test_a.py:1: home-path: '{HOME_LITERAL}'"


def _test_patch(line: str) -> str:
    return f"--- a/tests/test_a.py\n+++ b/tests/test_a.py\n-old line\n+{line}\n+assert True\n"


def _stub_gate(monkeypatch, outcomes=None):
    """A gate edge that flags a patch carrying the literal and records every patch it sees."""
    seen: list[str] = []

    def gate(patch, checks, repo_dir):
        seen.append(patch)
        if outcomes is not None:
            return outcomes
        if HOME_LITERAL in patch:
            return f"The clean check found a personal reference in the patch:\n  {HIT_LINE}", "ran"
        return None, "ran"

    monkeypatch.setattr(lifecycle_propose, "clean_gate_outcome", gate)
    return seen


def _build(build_response: dict, line: str) -> dict:
    return {**build_response, "patch": _test_patch(line), "files_touched": ["tests/test_a.py"]}


def _run(cartridge, scripted):
    return lifecycle_propose.run(
        {"run_id": "run-1", "date": "2026-11-11", "ticket": "TICKET-1", "cartridge": cartridge}, scripted
    )


def test_a_clean_critique_is_a_revise_with_one_clean_check_finding() -> None:
    assert lifecycle_propose._clean_critique(HIT_LINE) == {
        "verdict": "revise",
        "findings": [{"charter_principle": "clean check", "detail": HIT_LINE, "file": ""}],
        "rationale": "clean_check",
    }


def test_a_home_literal_is_flagged_before_review_and_fixed_on_the_retry(
    monkeypatch, cartridge, plan_response, build_response
) -> None:
    seen = _stub_gate(monkeypatch)
    first = _build(build_response, f'path = "{HOME_LITERAL}"')
    second = _build(build_response, "path = tmp_path")
    scripted = ScriptedRunner({"plan": plan_response, "build": [first, second], "review_charter": [APPROVE]})
    result = _run(cartridge, scripted)

    assert [(r["attempt"], r["source"], r["verdict"]) for r in result["fix_loop"]["rounds"]] == [
        (1, "clean check", "revise"),
        (2, "review", "approve"),
    ]
    roles = [c["role"] for c in scripted.calls if c["role"] in ("build", "review_charter")]
    assert roles == ["build", "build", "review_charter"]
    retry_prompt = next(c["prompt"] for c in scripted.calls if c["role"] == "build" and "Standing critique" in c["prompt"])
    assert "(clean check)" in retry_prompt
    assert HIT_LINE in retry_prompt
    assert seen == [first["patch"], second["patch"]]
    evidence = {e["check"]: e["output"] for e in result["proposals"][0]["evidence"]}
    assert evidence["fix loop"] == "approved on attempt 2 of 3"
    assert evidence["clean gate"] == "ran on attempt 2: no hits"


def test_a_retry_that_still_carries_the_literal_is_flagged_again(
    monkeypatch, cartridge, plan_response, build_response
) -> None:
    _stub_gate(monkeypatch)
    builds = [
        _build(build_response, f'path = "{HOME_LITERAL}"'),
        # Moves enough text that the no-progress check sees a real second attempt.
        _build(build_response, f'def home_dir():\n+    return "{HOME_LITERAL}"\n+\n+path = home_dir()'),
        _build(build_response, "path = tmp_path"),
    ]
    scripted = ScriptedRunner({"plan": plan_response, "build": builds, "review_charter": [APPROVE]})
    result = _run(cartridge, scripted)

    assert [(r["attempt"], r["source"]) for r in result["fix_loop"]["rounds"]] == [
        (1, "clean check"),
        (2, "clean check"),
        (3, "review"),
    ]
    assert len([c for c in scripted.calls if c["role"] == "review_charter"]) == 1


def test_a_gate_that_exited_two_is_a_skipped_row_and_never_blocks(
    monkeypatch, cartridge, plan_response, build_response
) -> None:
    _stub_gate(monkeypatch, outcomes=(None, "skipped"))
    scripted = ScriptedRunner({"plan": plan_response, "build": build_response, "review_charter": [APPROVE]})
    result = _run(cartridge, scripted)

    assert [(r["attempt"], r["source"], r["verdict"]) for r in result["fix_loop"]["rounds"]] == [(1, "review", "approve")]
    evidence = {e["check"]: e["output"] for e in result["proposals"][0]["evidence"]}
    assert evidence["clean gate"] == "skipped on attempt 1: names pattern not exported (exit 2)"
