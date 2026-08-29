"""Autonomy policy: decide whether a write kind may auto-apply, or must be gated.

Pure. No I/O, no clock, no environment reads. Takes a ledger and a question,
returns a decision. That purity is the point — the rule that governs whether an
agent may write to a production system should be testable in-process, with no
network and no fixtures beyond plain data.

CONTRACT (implement against this; see docs/CLEAN-ROOM.md — write it fresh)

    autonomy_policy(kind, risk, ledger_rows, policy_config) -> "auto" | "propose"

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

Unit tests for this file should need nothing but dicts and lists.
"""

from __future__ import annotations

__all__ = ["autonomy_policy"]


def autonomy_policy(*args, **kwargs):  # noqa: D401 - contract stub
    raise NotImplementedError(
        "Implement from the contract in this module's docstring. "
        "Do not port a prior employer's implementation — see docs/CLEAN-ROOM.md."
    )
