import json
from pathlib import Path

import pytest

from agent_tools.dash_feed_cost import SECTION_ORDER, cost_block, measure

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_cost_v1.json").read_text())["cost"]
_ALL = list(SECTION_ORDER)


def test_section_order_is_canonical():
    assert _ALL == ["chair", "spend", "machines", "runs", "queue", "inbox", "history", "board", "epics", "live"]


def test_cost_block_equals_fixture_exactly():
    got = cost_block(
        _FIXTURE["cpu_ms"] / 1000,
        _FIXTURE["wall_ms"] / 1000,
        reversed(_FIXTURE["recomputed"]),
        reversed(_FIXTURE["reused"]),
    )
    assert got == _FIXTURE


def test_cost_block_rounds_to_two_decimals():
    got = cost_block(0.0065049, 0.0120051, ["live"], _ALL[:-1])
    assert (got["cpu_ms"], got["wall_ms"]) == (6.5, 12.01)


def test_overlapping_sections_raise():
    with pytest.raises(ValueError, match="both"):
        cost_block(0.0, 0.0, ["live", "chair"], _ALL[:-1])


def test_missing_section_raises():
    with pytest.raises(ValueError, match="neither"):
        cost_block(0.0, 0.0, ["live"], _ALL[:-2])


def test_unknown_section_raises():
    with pytest.raises(ValueError, match="unknown"):
        cost_block(0.0, 0.0, ["live", "bogus"], _ALL[:-1])


def test_measure_returns_start_token_and_elapsed():
    cpu, wall = iter([1.0, 1.5]), iter([10.0, 10.25])
    start, finish = measure(lambda: next(cpu), lambda: next(wall))
    assert start == (1.0, 10.0)
    assert finish() == (0.5, 0.25)
