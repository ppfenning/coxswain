"""H10 items 6 and 7: the review, arbitration, phase-validation, decompose and plan skills carry the two questions.

A review verdict comes from the model, so no code path in the suites evaluates one. This test is the
fixture-driven half instead: it loads each role's body by the lookup the harness makes (the `local` cartridge
resolved against the in-repo plugin, each binding found in the index, as `harness.resolve.role_skill_bodies` does), then feeds two review fixtures (a patch adding a
helper nothing calls, and a unit test that monkeypatches its way to the logic) through the rules the body states,
and checks that a rule the body carries sends each one to revise. The unwired fixture's evidence row is produced by
the real check's pure core, so the reviewer and the check name the same thing.
"""

from __future__ import annotations

import functools
import importlib.util
import re
from pathlib import Path

import pytest

from core.cartridge import load
from core.skills import index_from_roots

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO.parents[1]
PLUGIN_ROOT = REPO / "skills-plugins"

_SPEC = importlib.util.spec_from_file_location("check_unwired", ROOT / ".github" / "check_unwired.py")
check_unwired = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_unwired)

REVIEW_ROLES = ("review_charter", "review_adversary", "arbitrate", "validate_phase")
CORE = "src/pkg/core.py"

UNWIRED_PATCH = {
    "old": {CORE: "def parse(text):\n    return text.split()\n"},
    "new": {
        CORE: "def parse(text):\n    return text.split()\n\n\ndef count(text):\n    return len(parse(text))\n",
        "src/pkg/cli.py": "from pkg.core import parse\n\n\ndef main(argv):\n    return parse(argv[0])\n",
        "src/pkg/tests/test_core.py": "from pkg.core import count\n\n\ndef test_count():\n"
                                      "    assert count('a b') == 2\n",
    },
}
MOCKED_TEST = (
    "def test_sync_reports(monkeypatch):\n"
    "    monkeypatch.setattr(sync, 'run_git', lambda *a: (0, 'ok'))\n"
    "    assert sync.report() == 'ok'\n"
)


@functools.cache
def bodies() -> dict[str, str]:
    """role -> body text, resolved as the harness resolves it: the cartridge's binding looked up in the index."""
    index = index_from_roots([PLUGIN_ROOT])
    resolved = load("local", REPO / "cartridges", skill_index=index)
    paths = {role: Path(str(index[name][0])) for role, name in resolved["skills"].items() if len(index.get(name, ())) == 1}
    return {role: path.read_text(encoding="utf-8") for role, path in paths.items()}


def flat(text: str) -> str:
    return " ".join(text.split())


def revise_rules(body: str) -> list[str]:
    """The numbered rules of the body's two-questions section that end in revise, each flattened to one line."""
    section = body[body.index("## Two questions every review answers"):]
    section = section[: section.index("\n## ", 1)]
    rules = re.split(r"\n(?=\d\. )", section)
    return [flat(rule) for rule in rules if re.match(r"\d\. ", rule) and "**revise**" in rule]


def verdict(body: str, finding: str) -> str:
    """What the body's own rules say for a review whose evidence holds `finding`: revise when a revise rule names it."""
    return "revise" if any(finding in rule for rule in revise_rules(body)) else "approve"


def unwired_evidence() -> tuple[str, ...]:
    changes = (check_unwired.Change("M", CORE, CORE),)
    added = check_unwired.added_symbols(changes, UNWIRED_PATCH["old"], UNWIRED_PATCH["new"])
    found = check_unwired.unwired(added, UNWIRED_PATCH["new"], frozenset())
    return check_unwired.report(added, found)


def test_the_unwired_fixture_is_what_the_check_reports():
    assert unwired_evidence() == (
        f"{CORE}:5: unwired: function count has no reference outside tests; call it from the code that should run it,"
        " or allowlist an entry point with its reason",
    )


@pytest.mark.parametrize("role", REVIEW_ROLES)
def test_a_review_of_an_unwired_helper_returns_revise(role):
    body = bodies()[role]
    assert any("unwired" in line for line in unwired_evidence())
    assert verdict(body, "no caller outside tests") == "revise"
    assert verdict(body, "`check_unwired` row fails") == "revise"


@pytest.mark.parametrize("role", REVIEW_ROLES)
def test_a_review_of_a_mocked_unit_test_returns_revise(role):
    body = bodies()[role]
    assert "monkeypatch" in MOCKED_TEST
    assert verdict(body, "`monkeypatch`") == "revise"


@pytest.mark.parametrize("role", REVIEW_ROLES)
def test_a_review_names_the_smoke_test_that_reaches_the_change(role):
    rules = revise_rules(bodies()[role])
    assert any("Which smoke or end-to-end test runs the entry point that reaches this change?" in r for r in rules)
    assert any("Is the logic pure, and tested without mocks?" in r for r in rules)


def test_a_clean_change_finds_no_rule_to_revise_on():
    assert verdict(bodies()["review_charter"], "a typed signature") == "approve"


def test_the_charter_lists_the_two_questions_among_what_may_carry_a_revise():
    text = flat(bodies()["review_charter"])
    assert "Four things and only these may carry a revise" in text
    assert "one of the two questions below has the wrong answer" in text


def test_arbitration_may_not_price_an_unwired_or_mocked_change_into_an_approve():
    text = flat(bodies()["arbitrate"])
    assert "These are not risks you may price into an approve" in text


def test_phase_validation_reports_the_missing_wiring_as_the_gap():
    text = flat(bodies()["validate_phase"])
    assert "`goal_met` is false and `missing` names the helper and the wiring it lacks" in text


def test_decompose_keeps_a_helper_and_its_wiring_together():
    text = flat(bodies()["decompose"])
    assert "A helper and its wiring are one task, or one phase." in text
    assert "never into a library task and a later wiring task" in text
    assert "no phase can complete with library code alone" in text
    assert "A done-when names a runtime behaviour at an entry point." in text


def test_plan_wires_what_it_adds():
    text = flat(bodies()["plan"])
    assert "A step that adds a function also wires it." in text
    assert "no mock, `monkeypatch`, stub or fake" in text
