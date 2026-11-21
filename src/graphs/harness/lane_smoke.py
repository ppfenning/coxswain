"""The phase smoke, pure: what a phase lane's `level` gate run means, and when an earlier run of it stands (plan L2).

Once every task of a phase has rolled up, the lane runs the level gate (`check_gate`'s `level`: the cartridge's
`level_checks` and `.agent-checks`' `# level: phase` section) once, in the phase worktree at the phase tip, and writes
one `smoke` step row (`phase/smoke/<round>`): the smoke before review round `round`. It runs again for each later
round, after that round's revision (L6). A phase whose level gate lists nothing still gets a row (`none`), so the
worker can tell a smoke that passed from a lane that ended before its smoke ran.

A pass is evidence for the tip it ran on only when something was checked: a row records `reusable` when it passed
and no check of it ran nothing but skips (a smoke suite on a machine without docker skips every test and exits 0).
A later lane of the phase at the same tip with the same commands then writes a `reused` row naming that run instead
of running the suite again. A failure is never reused.

How a failed smoke ends is the worker's (`phase_pick.lane_verdict`): no review, and the phase is not parked `built`.
A real failure finishes the phase `failed` at once, since a retry would run the same commands on the same tip. A
harness fault (a check that could not run, `checks.is_harness_fault`) goes back to the queue once; a second one at
the same tip finishes it `failed`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from harness.checks import is_harness_fault, quarantine_reason

__all__ = ["Smoke", "outcome", "result_json", "reusable", "skips_only"]

Verdict = Literal["passed", "failed", "none", "reused"]
_PREFIX = "the phase smoke failed: "


@dataclass(frozen=True)
class Smoke:
    """One smoke: whether it passed, its verdict, why it failed ('' when it passed), whether that failure is a harness
    fault rather than the phase's, and whether a pass is evidence a later lane at the same tip may reuse."""

    ok: bool
    verdict: Verdict
    reason: str = ""
    harness_fault: bool = False
    reusable: bool = False


def skips_only(result: Mapping[str, object]) -> bool:
    """Whether a check result checked nothing: pytest's "no tests ran", or a run whose counts hold skips and no pass."""
    counts = result.get("counts")
    counted = counts if isinstance(counts, Mapping) else {}
    return result.get("outcome") == "skipped" or (bool(counted.get("skipped")) and not counted.get("passed"))


def outcome(results: Sequence[Mapping[str, object]]) -> Smoke:
    """The smoke `run_checks` gave as `results`: `none` with no checks, `passed` when every one passed (reusable
    unless one of them only skipped), else `failed` with the first failing check as its reason
    (`checks.quarantine_reason`), marked a harness fault when a check could not run."""
    if not results:
        return Smoke(True, "none")
    reason = quarantine_reason(results)
    if reason is None:
        return Smoke(True, "passed", reusable=not any(skips_only(r) for r in results))
    return Smoke(False, "failed", _PREFIX + reason, harness_fault=is_harness_fault(reason))


def result_json(tip: str, cmds: Sequence[str], smoke: Smoke, results: Sequence[Mapping[str, object]],
                reused_from: str = "") -> dict[str, object]:
    """The `smoke` row's result: the tip and commands it ran (what `reusable` matches on), the reason, whether it is
    a harness fault and whether it may be reused, and each check as `{name, passed, outcome, exit_code}`."""
    return {
        "tip": tip,
        "cmds": list(cmds),
        "reason": smoke.reason,
        "harness_fault": smoke.harness_fault,
        "reusable": smoke.reusable,
        "checks": [{"name": str(r.get("name") or ""), "passed": r.get("passed") is True,
                    "outcome": str(r.get("outcome") or ""), "exit_code": r.get("exit_code")} for r in results],
        **({"reused_from": reused_from} if reused_from else {}),
    }


def reusable(rows: Sequence[Mapping[str, object]], tip: str, cmds: Sequence[str]) -> Mapping[str, object] | None:
    """The first stored `smoke` row (as `{run_id, ok, subject_sha, result_json}`) that passed at `tip` with exactly
    `cmds` and recorded itself `reusable`, or None. A `reused` row is never reusable itself: the run it names is."""
    for row in rows:
        result = row.get("result_json")
        if not isinstance(result, Mapping) or result.get("reusable") is not True:
            continue
        if row.get("ok") in (1, True) and row.get("subject_sha") == tip and result.get("cmds") == list(cmds):
            return row
    return None
