from typing import Any

from graphs._contract import proposal

CARTRIDGE = {"write_kinds": {"item_create": {"risk": "low", "ramp": "deferred"}}}


def _build(**extra: Any) -> dict[str, Any]:
    return proposal(
        CARTRIDGE,
        kind="item_create",
        target="work/x/p1/t.md",
        evidence=[{"check": "ls", "output": "t.md"}],
        rationale="the task is missing",
        suggested_action="create it",
        **extra,
    )


def test_apply_is_carried_as_a_copy_of_the_mapping_given() -> None:
    given = {"path": "work/x/p1/t.md", "item": {"title": "T"}}
    out = _build(apply=given)
    assert out["apply"] == given
    assert out["apply"] is not given


def test_no_apply_means_no_apply_key() -> None:
    assert "apply" not in _build()
    assert "apply" not in _build(apply=None)


def test_malformed_apply_without_path_is_carried_unchanged() -> None:
    out = _build(apply={"item": {"title": "T"}})
    assert out["apply"] == {"item": {"title": "T"}}
