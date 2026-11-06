"""Pure builders for the feed's machines[].lanes and machines[].load keys; no I/O, no clock."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any


def _field(lane: Any, name: str, default: Any) -> Any:
    value = lane.get(name) if isinstance(lane, Mapping) else getattr(lane, name, None)
    return default if value is None else value


def _elapsed_s(started: datetime | None, now: datetime) -> int:
    """Whole seconds from `started` to `now`; 0 when `started` is missing or later than `now`."""
    return 0 if started is None or started > now else int((now - started).total_seconds())


def lane_entries(
    lanes: Sequence[Any],
    host_of: Callable[[str], str | None],
    initiative_of: Mapping[str, str],
    building_tasks: Mapping[tuple[str, str], Sequence[str]],
    started_at: Mapping[str, datetime | None],
    now: datetime,
    *,
    local_host: str,
) -> dict[str, list[dict]]:
    """Group lanes by host. A lane with no host goes to `host_of(run)`, then to `local_host`."""
    entries = []
    for lane in lanes:
        run = str(_field(lane, "run", ""))
        phase = str(_field(lane, "phase", ""))
        initiative = initiative_of.get(run) or ""
        host = _field(lane, "host", "") or host_of(run) or local_host
        entries.append(
            (
                host,
                {
                    "run": run,
                    "initiative": initiative,
                    "phase": phase,
                    "tasks": list(building_tasks.get((initiative, phase), ())),
                    "elapsed_s": _elapsed_s(started_at.get(run), now),
                    "cost_usd": float(_field(lane, "cost_usd", 0)),
                },
            )
        )
    hosts = sorted({host for host, _ in entries})
    return {
        host: sorted((entry for h, entry in entries if h == host), key=lambda entry: entry["run"]) for host in hosts
    }


def _number(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def host_load(versions: Mapping[str, Any]) -> dict[str, float]:
    """Load figures from a host beat's `host_facts` and `host_load`; every figure rounded to 2 places."""
    facts = versions.get("host_facts") or {}
    load = versions.get("host_load") or {}
    cpu_count = _number(facts.get("cpu_count"))
    load_1m = _number(load.get("load_avg_1m"))
    return {
        "cpu_pct": round(_number(load.get("cpu_pct")), 2),
        "load_avg_per_core": round(load_1m / cpu_count, 2) if cpu_count > 0 else 0.0,
        "mem_used_gb": round(_number(load.get("mem_used_gb")), 2),
        "mem_total_gb": round(_number(facts.get("mem_total_gb")), 2),
    }
