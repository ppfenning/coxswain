"""A retry's build prompt carries the previous run's last objection for the same task."""

from __future__ import annotations

from test_epic_driver import Runner, cart, drive, initiative, new_file_patch, repo  # noqa: F401 -- fixtures

from harness.epic import PREVIOUS_OBJECTION_HEADING, previous_objection_block
from harness.resume import save_result

PRIOR = {
    "review": {"findings": [{"detail": "the probe skips the header row", "file": "probe.py"}]},
    "adversary": {"objections": [{"claim": "first claim", "why_wrong": "first reason"}]},
}


def test_the_last_objection_wins_over_a_finding() -> None:
    result = {**PRIOR, "adversary": {"objections": [
        {"claim": "first claim", "why_wrong": "first reason"},
        {"claim": "the join drops nulls", "why_wrong": "the ticket asks for them kept"},
    ]}}
    assert previous_objection_block(result) == (
        f"{PREVIOUS_OBJECTION_HEADING}\nthe join drops nulls the ticket asks for them kept"
    )


def test_the_last_finding_is_used_when_there_is_no_objection() -> None:
    result = {"review": {"findings": [
        {"detail": "older", "file": "a.py"},
        {"detail": "the probe skips the header row", "file": "probe.py"},
    ]}}
    assert previous_objection_block(result) == (
        f"{PREVIOUS_OBJECTION_HEADING}\nthe probe skips the header row (probe.py)"
    )


def test_the_block_is_empty_when_there_is_neither() -> None:
    assert previous_objection_block({"review": {"findings": []}, "adversary": {"objections": []}}) == ""
    assert previous_objection_block({}) == ""
    assert previous_objection_block(None) == ""


def test_the_cap_bounds_a_long_objection_but_not_its_heading() -> None:
    block = previous_objection_block({"adversary": {"objections": [{"claim": "x" * 5000, "why_wrong": "y"}]}}, 100)
    assert block.startswith(PREVIOUS_OBJECTION_HEADING)
    assert block.endswith("x" * 100 + "... truncated")
    assert len(block) == len(PREVIOUS_OBJECTION_HEADING) + 1 + 100 + len("... truncated")


def _runner() -> Runner:
    return Runner({"t1-probe": new_file_patch("t1-probe.txt")})


def _build_prompt(runner: Runner) -> str:
    return next(c["prompt"] for c in runner.calls if c["role"] == "build" and "t1-probe" in c["prompt"])


def test_a_retry_prompt_contains_the_previous_objection_claim(repo, cart, tmp_path) -> None:  # noqa: F811
    prior = {**PRIOR, "adversary": {"objections": [{"claim": "the join drops nulls", "why_wrong": "kept in ticket"}]}}
    save_result(prior, runs_dir=tmp_path / "runs", run_id="epic-before", phase="p1-foundations", task="t1-probe")
    runner = _runner()
    drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), run_id="epic-retry",
          resume_from="epic-before")
    prompt = _build_prompt(runner)
    assert PREVIOUS_OBJECTION_HEADING in prompt and "the join drops nulls" in prompt


def test_a_first_build_prompt_has_no_objection_heading(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = _runner()
    drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), run_id="epic-first")
    assert PREVIOUS_OBJECTION_HEADING not in _build_prompt(runner)
