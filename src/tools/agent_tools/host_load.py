"""Host load rules shared by the beat and by placement: the record a beat stores, and which hosts are over a ceiling."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping

LOAD_KEY = "load"

__all__ = ["LOAD_KEY", "load_report", "overloaded", "resolve_load_ceiling"]


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 2) if denominator else 0.0


def load_report(loadavg_1m: float, cores: int, mem_total_kb: int, mem_available_kb: int) -> dict[str, float]:
    """`cpu` is the 1-minute load average per core; `mem` is the fraction of memory in use. A zero total gives 0.0."""
    return {"cpu": _ratio(loadavg_1m, cores), "mem": _ratio(mem_total_kb - mem_available_kb, mem_total_kb)}


def resolve_load_ceiling(value: object) -> float | None:
    """A ceiling above zero as a float; anything else means no ceiling."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        ceiling = float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return ceiling if math.isfinite(ceiling) and ceiling > 0 else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _load_of(raw: object) -> dict[str, float] | None:
    """The `load` record of a `versions_json` (JSON string or mapping) when both fields are numbers, else None."""
    try:
        versions = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return None
    record = versions.get(LOAD_KEY) if isinstance(versions, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    cpu, mem = _number(record.get("cpu")), _number(record.get("mem"))
    return None if cpu is None or mem is None else {"cpu": cpu, "mem": mem}


def _reason(load: dict[str, float], ceiling: float) -> str | None:
    over = [field for field in ("cpu", "mem") if load[field] > ceiling]
    return f"load {over[0]} {load[over[0]]:.2f} over {ceiling:.2f}" if over else None


def overloaded(rows: Iterable[Mapping[str, object]], ceiling: float | None) -> dict[str, str]:
    """Host name to reason for each host whose cpu or mem is above `ceiling`. Cpu is named when both are over."""
    if ceiling is None:
        return {}
    pairs = ((str(r["name"]), _load_of(r.get("versions_json"))) for r in rows)
    reasons = ((name, _reason(load, ceiling)) for name, load in pairs if load is not None)
    return {name: reason for name, reason in reasons if reason is not None}
