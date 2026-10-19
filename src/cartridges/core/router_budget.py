"""Per-node budget rule: a role's observed typical turn cost, with headroom."""

from __future__ import annotations

from statistics import median

DEFAULT_MARGIN = 1.5
DEFAULT_FLOOR_USD = 0.25


def budget_usd(
    observed_turn_costs: tuple[float, ...],
    margin: float = DEFAULT_MARGIN,
    floor_usd: float = DEFAULT_FLOOR_USD,
    *,
    price_ratio: float = 1.0,
) -> tuple[float, str]:
    """Per-call budget: median turn cost * margin * price_ratio, never below floor_usd. Callers clip to node and run caps."""
    if not observed_turn_costs:
        return floor_usd, f"no observed turn costs; using floor ${floor_usd:.2f}"
    typical = median(observed_turn_costs)
    scaled = typical * margin * price_ratio
    arithmetic = f"median ${typical:.2f} x margin {margin} x ratio {price_ratio} = ${scaled:.2f}"
    if scaled < floor_usd:
        return floor_usd, f"floor applied: {arithmetic} is below ${floor_usd:.2f}"
    return scaled, arithmetic
