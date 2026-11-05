from __future__ import annotations

import sys

import pytest

from graphs.delivery.initiative_decompose import LintRefusal, _carry, initiative_text

IDEA = {"id": "i1", "title": "t", "budget_usd": 3, "why": "w"}


def _no_dashes(value: str) -> str:
    if "-" in value:
        raise ValueError("no dashes")
    return value


def _no_dots(value: str) -> str:
    if ".." in value:
        raise ValueError("target_branch has dots")
    return value


VALIDATORS = {"epic": _no_dashes, "target_branch": _no_dots}


def _text(**kwargs: str) -> str:
    return initiative_text(IDEA, ["p1"], {"p1": "g"}, "r", **kwargs)


def test_epic_and_target_branch_are_written_into_the_frontmatter() -> None:
    frontmatter = _text(epic="e1", target_branch="release/x").split("---\n")[1]
    assert "epic: e1\n" in frontmatter
    assert "target_branch: release/x\n" in frontmatter


def test_neither_set_leaves_the_text_unchanged() -> None:
    assert _text() == (
        "---\nid: i1\ntitle: t\nrepo: r\nbudget_usd: 3\n---\n\nw\n\nPHASE GOALS, each judged against ITS OWN line:\n- p1: g\n"
    )


def test_a_bad_epic_id_is_refused_naming_the_key() -> None:
    with pytest.raises(LintRefusal, match="^epic: no dashes$"):
        _carry({"intake": {"frontmatter": {"epic": "a-b"}}}, {}, VALIDATORS)


def test_a_bad_branch_is_refused_naming_the_key() -> None:
    with pytest.raises(LintRefusal, match="^target_branch: target_branch has dots$"):
        _carry({}, {"target_branch": "a..b"}, VALIDATORS)


def test_an_intake_takes_the_place_of_the_idea_frontmatter() -> None:
    args = {"intake": {"frontmatter": {"epic": "e2"}}}
    assert _carry(args, {"epic": "e1", "target_branch": "main"}, VALIDATORS) == {"epic": "e2"}


def test_neither_key_set_needs_no_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "core.epics", None)
    monkeypatch.setitem(sys.modules, "core.landing", None)
    assert _carry({}, {"title": "t"}) == {}


def test_the_core_validators_refuse_a_bad_epic_and_branch() -> None:
    with pytest.raises(LintRefusal, match="^epic:"):
        _carry({}, {"epic": "Bad Epic"})
    with pytest.raises(LintRefusal, match="^target_branch:"):
        _carry({}, {"target_branch": "a..b"})
