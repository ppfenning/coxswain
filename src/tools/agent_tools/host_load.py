"""Host load rules shared by the beat and by placement: the record a beat stores, and which hosts are over a ceiling."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping

LOAD_KEY = "load"

__all__ = ["LOAD_KEY", "load_report", "overload_warnings", "overloaded", "resolve_load_ceiling"]


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 2) if denominator else 0.0


def load_report(loadavg_1m: float, cores: int, mem_total_kb: int, mem_available_kb: int) -> dict[str, float]:
    """`cpu` is the 1-minute load average per core; `mem` is the fraction of memory in use. A zero total gives 0.0.

    `load_1m` is the raw 1-minute load and `cores` the core count, kept so a warning can name them.
    """
    return {
        "cpu": _ratio(loadavg_1m, cores),
        "mem": _ratio(mem_total_kb - mem_available_kb, mem_total_kb),
        "load_1m": round(loadavg_1m, 2),
        "cores": int(cores),
    }


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


def _record_of(raw: object) -> dict[str, float] | None:
    """The `load` record of a `versions_json` (JSON string or mapping) when `cpu` and `mem` are numbers, else None.

    `load_1m` and `cores` are carried only when the record has them as numbers; older records lack both.
    """
    try:
        versions = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return None
    record = versions.get(LOAD_KEY) if isinstance(versions, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    cpu, mem = _number(record.get("cpu")), _number(record.get("mem"))
    if cpu is None or mem is None:
        return None
    extras = {key: value for key in ("load_1m", "cores") if (value := _number(record.get(key))) is not None}
    return {"cpu": cpu, "mem": mem, **extras}


def _load_of(raw: object) -> dict[str, float] | None:
    """The `cpu` and `mem` of the `load` record, or None when either is not a number."""
    record = _record_of(raw)
    return None if record is None else {"cpu": record["cpu"], "mem": record["mem"]}


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


def _warning(record: dict[str, float], factor: float) -> str:
    """Names the raw load and core count when the record has both; otherwise the per-core figure alone."""
    cpu, load_1m, cores = record["cpu"], record.get("load_1m"), record.get("cores")
    if load_1m is not None and cores is not None and cores >= 1:
        return f"load {load_1m} on {int(cores)} cores is {cpu:.2f} per core, over {factor:.2f}x"
    return f"load {cpu:.2f} per core, over {factor:.2f}x"


def overload_warnings(rows: Iterable[Mapping[str, object]], factor: float = 2.0) -> dict[str, str]:
    """Host name to a one-sentence warning for each host whose cpu is strictly above `factor`."""
    pairs = ((str(r["name"]), _record_of(r.get("versions_json"))) for r in rows)
    return {name: _warning(record, factor) for name, record in pairs if record is not None and record["cpu"] > factor}
