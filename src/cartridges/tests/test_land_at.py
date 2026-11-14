from __future__ import annotations

import pytest

from core.cartridge import apply_overlay, overlay_errors
from core.landing import (
    canonical_land_at,
    land_at_warnings,
    land_gate_level,
    lands_at_initiative,
    phase_counts_complete,
    phase_land_deferred,
    resolve_land_at,
)

REFUSAL = "must be one of 'initiative', 'task', 'ticket', 'level'"
EVERY_SPELLING = ("initiative", "ticket", "level", "task")


def test_default_is_initiative() -> None:
    assert resolve_land_at({}, None) == "initiative"
    assert resolve_land_at({"landing_areas": {"checks": []}}, None) == "initiative"


def test_ticket_is_a_permanent_alias_of_initiative() -> None:
    assert resolve_land_at({"landing_areas": {"land_at": "ticket"}}, None) == "initiative"
    assert resolve_land_at({}, "ticket") == "initiative"
    assert canonical_land_at("ticket") == "initiative"
    assert land_at_warnings({"landing_areas": {"land_at": "ticket"}}, "ticket") == []


def test_retired_level_reads_as_initiative_with_a_warning() -> None:
    assert resolve_land_at({"landing_areas": {"land_at": "level"}}, None) == "initiative"
    warnings = land_at_warnings({"landing_areas": {"land_at": "level"}}, "level")
    assert len(warnings) == 2
    assert "cartridge land_at 'level' is retired" in warnings[0]
    assert "initiative land_at 'level' is retired" in warnings[1]


def test_lands_at_initiative_reads_every_spelling_and_the_default() -> None:
    assert [lands_at_initiative(v) for v in (None, "initiative", "ticket", "level", "task", "epic")] == [
        True, True, True, True, False, False,
    ]


def test_overlay_value_merges_and_keeps_siblings() -> None:
    base = {"landing_areas": {"land_checks": "local", "land_at": "ticket"}}
    merged = apply_overlay(base, {"landing_areas": {"land_at": "task"}})
    assert merged["landing_areas"] == {"land_checks": "local", "land_at": "task"}
    assert resolve_land_at(merged, None) == "task"


def test_initiative_beats_cartridge_and_overlay() -> None:
    merged = apply_overlay({"landing_areas": {"land_at": "initiative"}}, {"landing_areas": {"land_at": "task"}})
    assert resolve_land_at(merged, "ticket") == "initiative"
    assert resolve_land_at({"landing_areas": {"land_at": "initiative"}}, "task") == "task"


def test_overlay_accepts_each_spelling() -> None:
    for value in EVERY_SPELLING:
        assert overlay_errors({"landing_areas": {"land_at": value}}) == []


@pytest.mark.parametrize("bad", ["epic", "", 3])
def test_invalid_overlay_value_is_refused(bad: object) -> None:
    errors = overlay_errors({"landing_areas": {"land_at": bad}})
    assert len(errors) == 1
    assert "landing_areas.land_at" in errors[0] and REFUSAL in errors[0]


def test_invalid_cartridge_value_is_refused() -> None:
    with pytest.raises(ValueError, match="cartridge land_at must be one of 'initiative', 'task'"):
        resolve_land_at({"landing_areas": {"land_at": "epic"}}, None)


def test_invalid_cartridge_value_is_refused_even_when_initiative_overrides() -> None:
    with pytest.raises(ValueError, match="cartridge land_at"):
        resolve_land_at({"landing_areas": {"land_at": "epic"}}, "task")


def test_invalid_initiative_value_is_refused() -> None:
    with pytest.raises(ValueError, match="initiative land_at must be one of 'initiative', 'task'"):
        resolve_land_at({}, "epic")


def test_land_gate_level_is_the_canonical_value() -> None:
    assert [land_gate_level(v) for v in EVERY_SPELLING] == ["initiative", "initiative", "initiative", "task"]
    with pytest.raises(ValueError, match="gate land_at"):
        land_gate_level("epic")


def test_last_phase_is_never_deferred() -> None:
    assert [phase_land_deferred(v, "p2", ["p1", "p2"]) for v in EVERY_SPELLING] == [False] * 4


def test_non_last_phase_is_deferred_at_initiative_only() -> None:
    assert [phase_land_deferred(v, "p1", ["p1", "p2"]) for v in EVERY_SPELLING] == [True, True, True, False]


def test_approved_merged_counts_at_initiative_not_task() -> None:
    assert [phase_counts_complete(v, [("approved", True)]) for v in EVERY_SPELLING] == [True, True, True, False]


def test_approved_unmerged_never_counts() -> None:
    assert [phase_counts_complete(v, [("approved", False)]) for v in EVERY_SPELLING] == [False] * 4


def test_done_dropped_and_empty_count_everywhere() -> None:
    for v in EVERY_SPELLING:
        assert phase_counts_complete(v, [("done", False), ("dropped", False)])
        assert phase_counts_complete(v, [])


def test_unknown_land_at_raises_in_both_phase_rules() -> None:
    with pytest.raises(ValueError, match="land_at"):
        phase_land_deferred("epic", "p1", ["p1", "p2"])
    with pytest.raises(ValueError, match="land_at"):
        phase_counts_complete("epic", [])
