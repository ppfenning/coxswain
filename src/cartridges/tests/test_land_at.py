from __future__ import annotations

import pytest

from core.cartridge import apply_overlay, overlay_errors
from core.landing import land_gate_level, phase_counts_complete, phase_land_deferred, resolve_land_at

REFUSAL = "must be 'ticket', 'level' or 'task'"


def test_default_is_ticket() -> None:
    assert resolve_land_at({}, None) == "ticket"
    assert resolve_land_at({"landing_areas": {"checks": []}}, None) == "ticket"


def test_cartridge_value() -> None:
    assert resolve_land_at({"landing_areas": {"land_at": "level"}}, None) == "level"


def test_overlay_value_merges_and_keeps_siblings() -> None:
    base = {"landing_areas": {"land_checks": "local", "land_at": "ticket"}}
    merged = apply_overlay(base, {"landing_areas": {"land_at": "task"}})
    assert merged["landing_areas"] == {"land_checks": "local", "land_at": "task"}
    assert resolve_land_at(merged, None) == "task"


def test_initiative_beats_cartridge_and_overlay() -> None:
    merged = apply_overlay({"landing_areas": {"land_at": "level"}}, {"landing_areas": {"land_at": "task"}})
    assert resolve_land_at(merged, "ticket") == "ticket"
    assert resolve_land_at({"landing_areas": {"land_at": "level"}}, "task") == "task"


def test_overlay_accepts_each_allowed_value() -> None:
    for value in ("ticket", "level", "task"):
        assert overlay_errors({"landing_areas": {"land_at": value}}) == []


@pytest.mark.parametrize("bad", ["epic", "", 3])
def test_invalid_overlay_value_is_refused(bad: object) -> None:
    errors = overlay_errors({"landing_areas": {"land_at": bad}})
    assert len(errors) == 1
    assert "landing_areas.land_at" in errors[0] and REFUSAL in errors[0]


def test_invalid_cartridge_value_is_refused() -> None:
    with pytest.raises(ValueError, match="'ticket', 'level', 'task'"):
        resolve_land_at({"landing_areas": {"land_at": "epic"}}, None)


def test_invalid_cartridge_value_is_refused_even_when_initiative_overrides() -> None:
    with pytest.raises(ValueError, match="cartridge land_at"):
        resolve_land_at({"landing_areas": {"land_at": "epic"}}, "task")


def test_invalid_initiative_value_is_refused() -> None:
    with pytest.raises(ValueError, match="'ticket', 'level', 'task'"):
        resolve_land_at({}, "epic")


def test_land_gate_level_is_the_value_itself() -> None:
    assert [land_gate_level(v) for v in ("ticket", "level", "task")] == ["ticket", "level", "task"]
    with pytest.raises(ValueError, match="'ticket', 'level', 'task'"):
        land_gate_level("epic")


def test_last_phase_is_never_deferred() -> None:
    assert [phase_land_deferred(v, "p2", ["p1", "p2"]) for v in ("ticket", "level", "task")] == [False] * 3


def test_non_last_phase_is_deferred_at_ticket_only() -> None:
    assert [phase_land_deferred(v, "p1", ["p1", "p2"]) for v in ("ticket", "level", "task")] == [True, False, False]


def test_approved_merged_counts_at_ticket_and_level_not_task() -> None:
    assert [phase_counts_complete(v, [("approved", True)]) for v in ("ticket", "level", "task")] == [True, True, False]


def test_approved_unmerged_never_counts() -> None:
    assert [phase_counts_complete(v, [("approved", False)]) for v in ("ticket", "level", "task")] == [False] * 3


def test_done_dropped_and_empty_count_everywhere() -> None:
    for v in ("ticket", "level", "task"):
        assert phase_counts_complete(v, [("done", False), ("dropped", False)])
        assert phase_counts_complete(v, [])


def test_unknown_land_at_raises_in_both_phase_rules() -> None:
    with pytest.raises(ValueError, match="land_at"):
        phase_land_deferred("epic", "p1", ["p1", "p2"])
    with pytest.raises(ValueError, match="land_at"):
        phase_counts_complete("epic", [])
