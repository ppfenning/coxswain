"""Pure three-way spend split for the five-hour and weekly windows."""

from __future__ import annotations

from collections.abc import Mapping

_WINDOWS = ("five_hour", "weekly")
_PARTS = ("chair_session", "chair_agents", "cox_seats")
_Cell = float | str | None
_Window = Mapping[str, _Cell]


def _usd(value: _Cell) -> float:
    return 0.0 if value is None else float(value)


def _share(usd: float, total: float) -> float:
    return round(usd / total, 4) if total else 0.0


def _split_window(window: _Window | None) -> dict[str, float | str]:
    raw = window or {}
    usd = {p: _usd(raw.get(f"{p}_usd")) for p in _PARTS}
    rounded = {p: round(v, 4) for p, v in usd.items()}
    total = round(sum(usd.values()), 4)
    meter = raw.get("meter_fraction")
    resets = raw.get("resets_at")
    return {
        **{f"{p}_usd": rounded[p] for p in _PARTS},
        "total_usd": total,
        **{f"{p}_share": _share(rounded[p], total) for p in _PARTS},
        "meter_fraction": 0.0 if meter is None else meter,
        "resets_at": "" if resets is None else resets,
    }


def spend_split(windows: Mapping[str, _Window]) -> dict[str, dict[str, float | str]]:
    """Split each window's spend three ways; a missing window is all zero."""
    return {name: _split_window(windows.get(name)) for name in _WINDOWS}
