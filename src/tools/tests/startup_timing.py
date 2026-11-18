"""Shared startup timing: a cox command measured against a bare interpreter spawned the same way.

The two are timed in interleaved rounds, a baseline spawn then a cox spawn, and the verdict is the median of the
per-round ratios. Timing every baseline first and every cox run after let the host's load change between the two
blocks: a quiet baseline block and a busy cox block read as a slower cox (CI extras (tools), 2026-11-17: 9.01x against
the 8x multiple, a 0.017 s baseline against 0.157 s). Within one round both spawns see the same load.
"""

import statistics
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.startup_load_verdict import Round, ratios

# Documented absolute local targets, in seconds, for the median wall time of one process.
HELP_TARGET_S = 0.25
CHAIR_STATUS_TARGET_S = 0.30

# Measured cox/python -c pass ratio 4.6x to 5.8x (4 rounds of 21 runs, 2026-10-11, Linux, py3.14); 8.0x is 1.5x the clean ratio for noise and under 3x it (13.7x), so a 3x regression (~16x) fails.
# Python 3.15 (the prerelease CI job) starts cox relatively slower: a clean cox --help measured 8.6-8.8x the baseline
# under the load fixture on 2026-10-11 (tools main red from #1497 on), while the injected 3x regression lands near 26x
# there; on 3.14 that regression measures about 8.2x locally. So 3.14 keeps 8x and 3.15+ gets 12x.
STARTUP_MULTIPLE = 12.0 if sys.version_info >= (3, 15) else 8.0

BASELINE_ARGV = (sys.executable, "-c", "pass")


@dataclass(frozen=True)
class StartupResult:
    ok: bool
    message: str


def _once(argv: Sequence[str], env: Mapping[str, str] | None, cwd: Path | None) -> float:
    """Wall seconds of one spawn of `argv`; a non-zero exit raises."""
    start = time.perf_counter()
    subprocess.run(list(argv), capture_output=True, cwd=cwd, env=env, check=True)
    return time.perf_counter() - start


def time_rounds(
    argv: Sequence[str],
    env: Mapping[str, str] | None,
    runs: int,
    cwd: Path | None = None,
    *,
    baseline_env: Mapping[str, str] | None = None,
) -> list[Round]:
    """`runs` rounds of `(cox seconds, baseline seconds)`: in each, a `python -c pass` spawn and then an `argv` spawn,
    back to back, after one discarded warm-up of each. The baseline runs with `baseline_env`, by default `env`."""
    base_env = env if baseline_env is None else baseline_env
    _once(BASELINE_ARGV, base_env, cwd)
    _once(argv, env, cwd)
    rounds: list[Round] = []
    for _ in range(runs):
        baseline = _once(BASELINE_ARGV, base_env, cwd)
        rounds.append((_once(argv, env, cwd), baseline))
    return rounds


def check_rounds(rounds: Sequence[Round], multiple: float, absolute_target: float) -> StartupResult:
    """Pass when the median per-round cox/baseline ratio is at most `multiple`; the absolute target is reported only."""
    per_round, ratio = ratios(rounds)
    if ratio is None:
        return StartupResult(False, f"no round with a positive baseline among {len(rounds)}")
    if ratio <= multiple:
        return StartupResult(True, "")
    cox = statistics.median(c for c, _ in rounds)
    baseline = statistics.median(b for _, b in rounds)
    return StartupResult(
        False,
        f"cox took a median {cox:.3f}s against a {baseline:.3f}s interpreter baseline over {len(per_round)} "
        f"interleaved rounds: median ratio {ratio:.2f}x exceeds the {multiple:.2f}x multiple "
        f"(documented local target {absolute_target:.2f}s)",
    )
