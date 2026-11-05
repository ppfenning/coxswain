from dataclasses import fields

from runner.decision_log import CallDecision, from_row, to_row

REQUIRED = {
    "role": "plan",
    "requested_tier": "standard",
    "chosen_tier": "cheap",
    "model_id": "anthropic/claude-cheap",
    "reason": "node cap",
    "ticket_key": "T-1",
    "outcome_key": "o-1",
}

SHADOW_FIELDS = (
    "shadow_tier",
    "shadow_model_id",
    "shadow_verdict",
    "shadow_agreed",
    "shadow_costly",
    "shadow_cost_usd",
)

# The 23 fields CallDecision had before the shadow fields, as a literal.
PRE_CHANGE_FIELDS = [
    "role",
    "requested_tier",
    "chosen_tier",
    "model_id",
    "reason",
    "ticket_key",
    "outcome_key",
    "router_tier",
    "router_reason",
    "router_model",
    "router_effort",
    "router_budget_usd",
    "router_clipped_by",
    "claude_code_version",
    "effort",
    "budget_usd",
    "clipped_by",
    "system_one_backend",
    "system_one_mode",
    "system_one_answer",
    "system_one_confidence",
    "system_one_threshold",
    "system_one_agreed",
]

OLD_ROW = {
    **REQUIRED,
    "router_tier": "deep",
    "router_reason": "hard task",
    "claude_code_version": "2.1.0",
    "effort": "high",
    "budget_usd": 0.5,
    "clipped_by": "node_budget_cap",
}


def test_all_six_shadow_fields_round_trip_to_an_equal_record():
    record = CallDecision(
        **REQUIRED,
        shadow_tier="cheap",
        shadow_model_id="anthropic/claude-cheap",
        shadow_verdict=False,
        shadow_agreed=False,
        shadow_costly=True,
        shadow_cost_usd=0.0021,
    )
    row = to_row(record)
    assert all(name in row for name in SHADOW_FIELDS)
    assert from_row(row) == record


def test_an_old_shaped_row_loads_with_every_shadow_field_none():
    decision = from_row(OLD_ROW)
    assert [getattr(decision, name) for name in SHADOW_FIELDS] == [None] * 6


def test_the_fields_before_the_shadow_fields_are_the_pre_change_shape():
    existing = [f.name for f in fields(CallDecision) if not f.name.startswith("shadow_")]
    assert existing == PRE_CHANGE_FIELDS


def test_a_decision_without_shadow_fields_has_the_pre_change_values():
    row = to_row(CallDecision(**OLD_ROW))
    pre_change = {name: OLD_ROW.get(name) for name in PRE_CHANGE_FIELDS}
    assert {name: row[name] for name in PRE_CHANGE_FIELDS} == pre_change
