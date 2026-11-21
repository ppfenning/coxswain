"""The phase smoke, pure: what a phase lane's `level` gate run means, and when an earlier run of it stands (plan L2).

Once every task of a phase has rolled up, the lane runs the level gate (`check_gate`'s `level`: the cartridge's
`level_checks` and `.agent-checks`' `# level: phase` section) once, in the phase worktree at the phase tip, and writes
one `smoke` step row (`phase/smoke/<round>`): the smoke before review round `round`. It runs again for each later
round, after that round's revision (L6). A phase whose level gate lists nothing still gets a row (`none`), so the
worker can tell a smoke that passed from a lane that ended before its smoke ran.

A passing row is evidence for the tip it ran on: a later lane of the phase at the same tip, with the same commands,
writes a `reused` row naming it instead of running the suite again. A failing row is never reused: the next claim
runs it again, which is what a flaky smoke needs.

How a failed smoke ends is the worker's (`phase_pick.lane_verdict`): no review, and the phase is not parked `built`;
it goes back to the queue with the failure as its reason, a counted attempt, and after `phase_pick.MAX_ATTEMPTS`
claims it finishes `failed` for the operator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from harness.checks import quarantine_reason

__all__ = ["Smoke", "outcome", "result_json", "reusable"]

Verdict = Literal["passed", "failed", "none", "reused"]


@dataclass(frozen=True)
class Smoke:
    """One smoke: whether it passed, its verdict, and why it failed ('' when it passed)."""

    ok: bool
    verdict: Verdict
    reason: str = ""


def outcome(results: Sequence[Mapping[str, object]]) -> Smoke:
    """The smoke `run_checks` gave as `results`: `none` with no checks, `passed` when every one passed, else `failed`
    with the first failing check as its reason (`checks.quarantine_reason`, which says when a check could not run)."""
    if not results:
        return Smoke(True, "none")
    reason = quarantine_reason(results)
    return Smoke(True, "passed") if reason is None else Smoke(False, "failed", f"the phase smoke failed: {reason}")


def result_json(tip: str, cmds: Sequence[str], smoke: Smoke, results: Sequence[Mapping[str, object]],
                reused_from: str = "") -> dict[str, object]:
    """The `smoke` row's result: the tip and commands it ran (what `reusable` matches on), the reason, and each check
    as `{name, passed, outcome, exit_code}`."""
    return {
        "tip": tip,
        "cmds": list(cmds),
        "reason": smoke.reason,
        "checks": [{"name": str(r.get("name") or ""), "passed": r.get("passed") is True,
                    "outcome": str(r.get("outcome") or ""), "exit_code": r.get("exit_code")} for r in results],
        **({"reused_from": reused_from} if reused_from else {}),
    }


def reusable(rows: Sequence[Mapping[str, object]], tip: str, cmds: Sequence[str]) -> Mapping[str, object] | None:
    """The first stored `smoke` row (as `{run_id, ok, subject_sha, result_json}`) that passed at `tip` with exactly
    `cmds`, or None. A row that listed no commands is reused only when there are still none."""
    for row in rows:
        result = row.get("result_json")
        listed = result.get("cmds") if isinstance(result, Mapping) else None
        if row.get("ok") in (1, True) and row.get("subject_sha") == tip and listed == list(cmds):
            return row
    return None
