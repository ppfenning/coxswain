from collections.abc import Callable, Iterable

SECTION_ORDER: tuple[str, ...] = (
    "chair",
    "spend",
    "machines",
    "runs",
    "queue",
    "inbox",
    "history",
    "board",
    "epics",
    "live",
)


def _in_order(sections: frozenset[str]) -> list[str]:
    return [name for name in SECTION_ORDER if name in sections]


def cost_block(cpu_s: float, wall_s: float, recomputed: Iterable[str], reused: Iterable[str]) -> dict:
    """Feed cost block; the two section sets must be disjoint and cover SECTION_ORDER."""
    fresh, kept = frozenset(recomputed), frozenset(reused)
    both = fresh & kept
    if both:
        raise ValueError(f"sections both recomputed and reused: {_in_order(both)}")
    unknown = (fresh | kept) - frozenset(SECTION_ORDER)
    if unknown:
        raise ValueError(f"unknown sections: {sorted(unknown)}")
    missing = frozenset(SECTION_ORDER) - fresh - kept
    if missing:
        raise ValueError(f"sections neither recomputed nor reused: {_in_order(missing)}")
    return {
        "cpu_ms": round(cpu_s * 1000, 2),
        "wall_ms": round(wall_s * 1000, 2),
        "recomputed": _in_order(fresh),
        "reused": _in_order(kept),
    }


def measure(
    clock_cpu: Callable[[], float], clock_wall: Callable[[], float]
) -> tuple[tuple[float, float], Callable[[], tuple[float, float]]]:
    """Return a start token and a finish function giving (cpu_s, wall_s) elapsed since it."""
    start = (clock_cpu(), clock_wall())

    def finish() -> tuple[float, float]:
        return clock_cpu() - start[0], clock_wall() - start[1]

    return start, finish
