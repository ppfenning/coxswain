from __future__ import annotations

import pytest

from core.landing import resolve_target_branch, valid_branch

CARTRIDGE = {"landing_areas": {"target_branch": "release"}}


@pytest.mark.parametrize("name", ["main", "feature/x-1", "release-2.0", "a.b", "v1@x"])
def test_valid_branch_accepts_bare_names(name: str) -> None:
    assert valid_branch(name, "initiative") == name


@pytest.mark.parametrize(
    "name",
    ["", "a b", "a\tb", "a\x7fb", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a..b", "a@{b",
     "-a", "/a", "a/", "a.", "a.lock", 3, None, ["main"]],
)  # fmt: skip
def test_valid_branch_refuses_broken_names(name: object) -> None:
    with pytest.raises(ValueError) as err:
        valid_branch(name, "epic")
    assert str(err.value) == f"epic target_branch must be a valid branch name, got {name!r}"


def test_initiative_wins_over_epic_and_cartridge() -> None:
    assert resolve_target_branch(CARTRIDGE, "epic-b", "init-b") == "init-b"


def test_epic_wins_over_cartridge() -> None:
    assert resolve_target_branch(CARTRIDGE, "epic-b", None) == "epic-b"


def test_cartridge_value_applies_alone() -> None:
    assert resolve_target_branch(CARTRIDGE, None, None) == "release"


@pytest.mark.parametrize("cartridge", [{}, {"landing_areas": {}}, {"landing_areas": None}])
def test_none_when_nothing_is_set(cartridge: dict) -> None:
    assert resolve_target_branch(cartridge, None, None) is None


def test_bad_initiative_value_is_refused() -> None:
    with pytest.raises(ValueError, match="initiative target_branch must be a valid branch name, got 'a..b'"):
        resolve_target_branch(CARTRIDGE, "epic-b", "a..b")


def test_bad_epic_value_is_refused_even_when_initiative_would_win() -> None:
    with pytest.raises(ValueError, match="epic target_branch must be a valid branch name, got '-x'"):
        resolve_target_branch(CARTRIDGE, "-x", "init-b")


def test_bad_cartridge_value_is_refused_even_when_initiative_would_win() -> None:
    with pytest.raises(ValueError, match="cartridge target_branch must be a valid branch name, got 'a b'"):
        resolve_target_branch({"landing_areas": {"target_branch": "a b"}}, "epic-b", "init-b")


def test_empty_string_is_refused_not_skipped() -> None:
    with pytest.raises(ValueError, match="epic target_branch"):
        resolve_target_branch(CARTRIDGE, "", None)
