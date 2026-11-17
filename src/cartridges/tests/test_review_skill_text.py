"""H10 items 6 and 7, as text: the skill bodies the harness loads carry the two questions and the wiring rule.

This is a text-presence check, not a review. It resolves each role's body by the lookup the harness makes (the
`local` cartridge against the in-repo plugin, each binding found in the index, as `harness.resolve.role_skill_bodies`
does) and asserts the rule text is there. It does not show that a review of an unwired helper or a mocked unit test
returns revise: the verdict comes from the model, and no path in the suites runs one. That done-when line stays open.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest

from core.cartridge import load
from core.skills import index_from_roots

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO / "skills-plugins"
REVIEW_ROLES = ("review_charter", "review_adversary", "arbitrate", "validate_phase")


@functools.cache
def bodies() -> dict[str, str]:
    """role -> body text, flattened to single spaces: the cartridge's binding looked up in the index."""
    index = index_from_roots([PLUGIN_ROOT])
    resolved = load("local", REPO / "cartridges", skill_index=index)
    paths = {role: Path(str(index[name][0])) for role, name in resolved["skills"].items() if len(index.get(name, ())) == 1}
    return {role: " ".join(path.read_text(encoding="utf-8").split()) for role, path in paths.items()}


def section(text: str) -> str:
    start = text.index("## Two questions every review answers")
    return text[start: text.index(" ## ", start + 1)]


@pytest.mark.parametrize("role", REVIEW_ROLES)
def test_each_review_body_asks_both_questions(role):
    rules = section(bodies()[role])
    assert "Is the logic pure, and tested without mocks?" in rules
    assert "Which smoke or end-to-end test runs the entry point that reaches this change?" in rules


@pytest.mark.parametrize("role", REVIEW_ROLES)
def test_each_review_body_names_a_mock_the_patch_adds_and_an_unwired_symbol(role):
    rules = section(bodies()[role])
    assert "`monkeypatch`" in rules
    assert re.search(r"adds? to a unit test", rules)
    assert "new or changed unit test" not in rules
    assert "no caller outside tests" in rules
    assert "`check_unwired` row fails" in rules
    assert "smoke suite (docs/smoke.md) grows" in rules


@pytest.mark.parametrize("role", ("review_charter", "review_adversary", "arbitrate"))
def test_task_review_and_arbitration_say_revise(role):
    assert section(bodies()[role]).count("**revise**") >= 2


def test_the_charter_lists_the_two_questions_among_what_may_carry_a_revise():
    text = bodies()["review_charter"]
    assert "Four things and only these may carry a revise" in text
    assert "one of the two questions below has the wrong answer" in text


def test_arbitration_may_not_price_an_unwired_or_mocked_change_into_an_approve():
    assert "These are not risks you may price into an approve" in bodies()["arbitrate"]


def test_phase_validation_answers_in_its_own_schema():
    rules = section(bodies()["validate_phase"])
    assert "`goal_met: false`, with the helper and the wiring it lacks named in `missing`" in rules
    assert "`partial: true`" in rules
    assert "**revise**" not in rules


def test_decompose_keeps_a_helper_and_its_wiring_in_one_task():
    text = bodies()["decompose"]
    assert "A helper and its wiring are one task." in text
    assert "never into a library task and a later wiring task" in text
    assert "no phase can complete with library code alone" in text
    assert "A done-when names a runtime behaviour at an entry point." in text


def test_plan_wires_what_it_adds():
    text = bodies()["plan"]
    assert "A step that adds a function also wires it." in text
    assert "no mock, `monkeypatch`, stub or fake" in text
