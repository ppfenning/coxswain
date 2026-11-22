"""Pure pricing and budget-cap helpers for API runners.

Prices are plain dicts of ``{model: {"input": usd_per_million, "output": usd_per_million}}``, optionally with
``cache_write`` and ``cache_read``; without them a cache write is priced at 1.25x the input price and a cache read at
0.1x, the published prompt-caching multipliers the provider catalog uses too.
Neither function reads a client, a profile object, a store handle, or a clock.
"""

from __future__ import annotations

import math

__all__ = ["max_output_tokens_for_budget", "price_call"]

_PER_MILLION = 1_000_000
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


def price_call(
    prices: dict,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float | None:
    """Cost in USD of a call, or None when ``model`` has no entry in ``prices``.

    ``input_tokens`` is the uncached input only, as the Messages API reports it; the cache writes and reads beside it
    are priced at the model's ``cache_write`` and ``cache_read`` rates, or the input rate times the multipliers.
    """
    if model not in prices:
        return None
    rate = prices[model]
    cache_write = rate.get("cache_write", rate["input"] * CACHE_WRITE_MULTIPLIER)
    cache_read = rate.get("cache_read", rate["input"] * CACHE_READ_MULTIPLIER)
    return (
        input_tokens / _PER_MILLION * rate["input"]
        + output_tokens / _PER_MILLION * rate["output"]
        + cache_write_tokens / _PER_MILLION * cache_write
        + cache_read_tokens / _PER_MILLION * cache_read
    )


def max_output_tokens_for_budget(prices: dict, model: str, budget_left_usd: float) -> int | None:
    """Output tokens the budget affords; None when ``model`` is unpriced or its output price is 0, so no cap applies."""
    if model not in prices or prices[model]["output"] == 0:
        return None
    return math.floor(budget_left_usd / (prices[model]["output"] / _PER_MILLION))
