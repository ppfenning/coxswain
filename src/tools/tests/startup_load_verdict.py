"""Pure verdict core for the interleaved startup check: timing data in, verdict out, no I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

Round = tuple[float, float]


@dataclass(frozen=True)
class CleanVerdict:
    ok: bool
    median_ratio: float | None
    ceiling: float
    rounds: int

    def __str__(self) -> str:
        return f"clean median ratio {self.median_ratio} vs ceiling {self.ceiling} over {self.rounds} rounds"


def ratios(rounds: Sequence[Round]) -> tuple[tuple[float, ...], float | None]:
    """Per-round cox/reference ratios and their median; rounds with a non-positive reference are skipped."""
    per_round = tuple(cox / ref for cox, ref in rounds if ref > 0)
    return per_round, (median(per_round) if per_round else None)


def check_clean(rounds: Sequence[Round], ceiling: float) -> CleanVerdict:
    per_round, mid = ratios(rounds)
    return CleanVerdict(mid is not None and mid <= ceiling, mid, ceiling, len(per_round))

