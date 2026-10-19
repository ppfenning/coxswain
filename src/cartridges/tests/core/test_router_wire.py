import json

from core.router import Decision
from core.router_wire import decision_from_wire, decision_to_wire

DECISION = Decision(
    model="m1", effort="high", budget_usd=0.5, reasons=("floor", "size"), clipped_by=("pacing",), chosen_class="judge"
)


def test_a_decision_round_trips_through_json():
    wire = decision_to_wire(DECISION)
    assert wire["schema"] == 1
    assert wire["reasons"] == ["floor", "size"]
    assert wire["clipped_by"] == ["pacing"]
    assert decision_from_wire(json.loads(json.dumps(wire))) == DECISION


def test_a_missing_key_is_an_error_value_naming_the_key():
    wire = {k: v for k, v in decision_to_wire(DECISION).items() if k != "model"}
    assert decision_from_wire(wire) == ["missing key: model"]


def test_an_unknown_schema_number_is_an_error_value():
    wire = {**decision_to_wire(DECISION), "schema": 2}
    assert decision_from_wire(wire) == ["unknown schema: 2"]


def test_a_wrong_typed_field_is_an_error_value():
    wire = {**decision_to_wire(DECISION), "reasons": "floor"}
    assert decision_from_wire(wire) == ["reasons must be a list of strings"]


def test_a_non_dict_is_an_error_value():
    assert decision_from_wire([1]) == ["wire form must be a dict, got list"]
