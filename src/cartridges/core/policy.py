"""Autonomy policy: decide whether a write kind may auto-apply, or must be gated.

Pure. No I/O, no clock, no environment reads. Takes a ledger and a question,
returns a decision. That purity is the point — the rule that governs whether an
agent may write to a production system should be testable in-process, with no
network and no fixtures beyond plain data.

CONTRACT (implement against this; see docs/CLEAN-ROOM.md — write it fresh)

    autonomy_policy(kind, risk, ledger_rows, policy_config) -> "auto" | "propose"

    plan_tier(cartridge, *, surfaces, patterns) -> 0 | 1 | 2

Rules the implementation must honour:

1.  ramp: never    -> always "propose". No streak can graduate it.
    ramp: gated    -> always "propose".
    ramp: deferred -> "propose" until the eligible kinds have graduated.
    ramp: eligible -> may graduate.

2.  A kind graduates after `graduation_n` consecutive CLEAN outcomes for that
    (kind, risk) pair. Clean means: proposed, approved unedited, and applied.

3.  A single reversal — human edited it, refused it, or a post-hoc detector
    fired — resets the streak to zero and multiplies the bar for that kind by
    `regraduation_multiplier`.

4.  Streaks are scoped to a configuration. Rows recorded under a different
    cartridge hash, provider profile, or per-node model binding DO NOT COUNT.
    A track record earned under different rules is not a track record. The
    caller filters; this module must not silently accept unfiltered rows.

5.  `caps` bound how many of a kind may auto-apply in a single run, even once
    graduated. Exceeding the cap does not reset the streak; the overflow simply
    gets proposed.

6.  The principal in a ledger row is the GRAPH, never a person. This module
    measures whether a write kind is trustworthy, not whether someone is.

7.  A row MAY carry a `subject` — the finer-grained principal inside a kind,
    such as the runbook entry a `runbook_execute` row was following. Trust is
    earned per subject where the caller names one: a runbook entry that has
    been right forty times says nothing about the entry written yesterday.

8.  A row MAY carry `attempts` — how many build attempts a fix loop took before
    this outcome. A clean that took three tries proves the loop converged, not
    that the kind is trustworthy first-try, so it neither builds nor breaks a
    streak. The fix loop must never launder struggle into trust.

9.  Before a build there are no `change_facts`, only a task's `surfaces` and
    `patterns`. `plan_tier` reads `cartridge["policy"]["review_tier"]`: 2 if
    any surface is in `tier2_surfaces`, else 0 if any pattern is in
    `tier0_patterns`, else 1. A missing list reads as empty.

Unit tests for this file should need nothing but dicts and lists.

IMPLEMENTATION NOTES (decisions the contract left open)

Rule 4 says the caller filters and this module "must not silently accept
unfiltered rows". Silence is the part that matters: handed rows spanning more
than one `cartridge_sha` or `provider_profile`, this module RAISES rather than
averaging a streak across configurations that were never comparable. A policy
that quietly does the wrong thing with bad input is how an agent earns autonomy
it did not deserve.

`policy_config` is the resolved cartridge's `policy` block plus the things the
decision cannot be made without: `write_kinds` (to read the kind's ramp),
`applied_this_run` (to enforce caps), and — where the caller has one — the
current proposal's `subject` and `subject_new`. The documented four-argument
signature is preserved rather than growing keyword arguments for them.

Subject is a GRAIN, not a scope. `SCOPE_KEYS` and `_require_single_scope` are
untouched by rule 7: rows about two runbook entries under one cartridge sha are
perfectly comparable, they are just answers to different questions. So:

-   `subject_new` truthy -> PROPOSE, unconditionally, even for a kind that has
    graduated. A brand-new entry has no track record by definition, and entry
    creation is the moment where a wrong one is cheapest to catch.
-   `subject` present -> the streak and the bar for this (kind, risk) are
    counted over rows carrying THAT subject and no others. An entry with no
    history has streak 0, and proposes.
-   `subject` absent -> kind-level fallback, exactly as before: every row of
    that (kind, risk) counts, whatever subject it happens to carry. That means
    one bad entry's reversals weigh on a subject-less caller's bar. It is the
    correct direction of error — the fallback should be the strict reading.

The `deferred` ramp check stays kind-level regardless. It asks whether the
basics are trusted at all, which is a question about the taxonomy, not about
whichever entry is in front of us.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["AUTO", "PROPOSE", "PolicyError", "autonomy_policy", "plan_tier"]

AUTO = "auto"
PROPOSE = "propose"

CLEAN = "clean"
REVERSAL = "reversal"
FAILURE = "failure"

# A reversal is anything that says the human did not accept what was proposed.
# `skipped` is neither — approved but never executed proves nothing either way,
# so it breaks no streak and builds none.
STREAK_BREAKING = frozenset({REVERSAL, FAILURE})

# Rows must be comparable on these before any streak may be counted across them.
SCOPE_KEYS = ("cartridge_sha", "provider_profile")


class PolicyError(Exception):
    """The policy was asked a question it must refuse rather than guess at."""


def _require_single_scope(rows: Sequence[Mapping[str, Any]]) -> None:
    """Refuse rows spanning more than one configuration. See rule 4."""
    for key in SCOPE_KEYS:
        values = {row.get(key) for row in rows}
        if len(values) > 1:
            found = ", ".join(sorted(repr(v) for v in values))
            raise PolicyError(
                f"ledger rows span {len(values)} values of '{key}' ({found}); "
                "a streak earned under different rules is not a streak. Filter before asking."
            )


def _first_try(row: Mapping[str, Any]) -> bool:
    """Did this outcome arrive on the first build attempt? Absent means yes.

    See rule 8. Only cleans consult this: a reversal is a reversal however many
    tries preceded it, and reading `attempts` there would let a fix loop buy
    its way out of the ratchet.
    """
    return int(row.get("attempts", 1) or 1) <= 1


def _streak_and_bar(
    rows: Sequence[Mapping[str, Any]],
    kind: str,
    risk: str,
    graduation_n: int,
    multiplier: int,
    subject: Any = None,
) -> tuple[int, int]:
    """Consecutive clean outcomes for (kind, risk), and the bar they must clear.

    Walks oldest-first so the bar reflects every reversal in this kind's
    history, not just the ones after the most recent clean run.

    `subject` narrows the history to one entry's own track record; None means
    the kind-level reading, which counts every row whatever subject it carries.
    """
    streak = 0
    bar = graduation_n
    for row in rows:
        if row.get("kind") != kind or row.get("risk") != risk:
            continue
        if subject is not None and row.get("subject") != subject:
            continue
        outcome = row.get("outcome")
        if outcome in STREAK_BREAKING:
            streak = 0
            bar *= multiplier
        elif outcome == CLEAN and _first_try(row):
            streak += 1
        # A clean that took several attempts falls through, transparent in
        # exactly the way `skipped` is: it neither proves the kind trustworthy
        # nor proves it wrong, so it must not move the streak either way.
    return streak, bar


def _has_graduated(
    rows: Sequence[Mapping[str, Any]],
    kind: str,
    risk: str,
    graduation_n: int,
    multiplier: int,
    subject: Any = None,
) -> bool:
    streak, bar = _streak_and_bar(rows, kind, risk, graduation_n, multiplier, subject)
    return streak >= bar


def autonomy_policy(
    kind: str,
    risk: str,
    ledger_rows: Sequence[Mapping[str, Any]],
    policy_config: Mapping[str, Any],
) -> str:
    """Return AUTO if this kind has earned the right to write, else PROPOSE."""
    rows = list(ledger_rows)
    _require_single_scope(rows)

    write_kinds = policy_config.get("write_kinds") or {}
    spec = write_kinds.get(kind)
    if not isinstance(spec, Mapping):
        raise PolicyError(f"unknown write kind '{kind}'; it is not in the cartridge's taxonomy")

    ramp = spec.get("ramp")
    if ramp in (None, "never", "gated"):
        return PROPOSE

    if policy_config.get("subject_new"):
        # Creating the entry is the one act no history can vouch for. Rule 7.
        return PROPOSE

    subject = policy_config.get("subject")
    graduation_n = int(policy_config.get("graduation_n", 5))
    multiplier = int(policy_config.get("regraduation_multiplier", 2))

    if ramp == "deferred":
        # Deferred kinds wait for the basics. Until every eligible kind has
        # earned its autonomy, nothing downstream of them gets to.
        eligible = [
            (name, s) for name, s in write_kinds.items() if isinstance(s, Mapping) and s.get("ramp") == "eligible"
        ]
        if not eligible:
            return PROPOSE
        for name, s in eligible:
            # Kind-level on purpose: "are the basics trusted yet" is a question
            # about the taxonomy, not about whichever subject is in front of us.
            if not _has_graduated(rows, name, s.get("risk", risk), graduation_n, multiplier):
                return PROPOSE
    elif ramp != "eligible":
        raise PolicyError(f"write kind '{kind}' has unknown ramp '{ramp}'")

    if not _has_graduated(rows, kind, risk, graduation_n, multiplier, subject):
        return PROPOSE

    # Graduated. Caps still bound how much it may do in one run; the overflow is
    # proposed rather than dropped, and does not touch the streak.
    caps = policy_config.get("caps") or {}
    cap = caps.get(kind)
    if cap is not None and int(policy_config.get("applied_this_run", 0)) >= int(cap):
        return PROPOSE

    return AUTO


def plan_tier(cartridge: Mapping[str, Any], *, surfaces: Sequence[str], patterns: Sequence[str]) -> int:
    """The pre-build tier, from a task's `surfaces` and `patterns` alone (rule 9).

    Before a build there are no change facts, so this reads the same
    `review_tier` config block the post-build tier does and answers from what
    the work item declares: a dangerous surface is tier 2, a tier-0 pattern is
    tier 0, anything else is tier 1.
    """
    review_tier = (cartridge.get("policy") or {}).get("review_tier") or {}
    tier2_surfaces = set(review_tier.get("tier2_surfaces") or [])
    tier0_patterns = set(review_tier.get("tier0_patterns") or [])

    if tier2_surfaces & set(surfaces):
        return 2
    if tier0_patterns & set(patterns):
        return 0
    return 1
