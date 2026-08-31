"""Nothing but dicts and lists, as the contract requires.

These tests are the argument that autonomy is earned rather than assumed, so
they check the ways a kind must FAIL to graduate at least as hard as the one
way it succeeds.
"""

from __future__ import annotations

import pytest

from core.policy import AUTO, PROPOSE, PolicyError, autonomy_policy
from tests.conftest import rows

WRITE_KINDS = {
    "draft_pr_create": {"risk": "low", "ramp": "eligible"},
    "retry_idempotent": {"risk": "medium", "ramp": "eligible"},
    "ticket_create": {"risk": "low", "ramp": "deferred"},
    "comment_add": {"risk": "low", "ramp": "gated"},
    "merge": {"risk": "high", "ramp": "never"},
}


def config(**overrides):
    base = {
        "graduation_n": 3,
        "regraduation_multiplier": 2,
        "caps": {},
        "write_kinds": WRITE_KINDS,
        "applied_this_run": 0,
    }
    return {**base, **overrides}


def clean(kind: str, risk: str, n: int):
    return rows(*[(kind, risk, "clean")] * n)


# ── Rule 1: ramp gates everything ───────────────────────────────────────────


@pytest.mark.parametrize("kind", ["merge", "comment_add"])
def test_never_and_gated_never_auto_apply_no_matter_the_streak(kind: str) -> None:
    risk = WRITE_KINDS[kind]["risk"]
    assert autonomy_policy(kind, risk, clean(kind, risk, 50), config()) == PROPOSE


def test_eligible_kind_proposes_until_the_bar_is_met() -> None:
    assert autonomy_policy("draft_pr_create", "low", clean("draft_pr_create", "low", 2), config()) == PROPOSE


def test_eligible_kind_graduates_at_the_bar() -> None:
    assert autonomy_policy("draft_pr_create", "low", clean("draft_pr_create", "low", 3), config()) == AUTO


def test_nothing_auto_applies_on_day_one() -> None:
    assert autonomy_policy("draft_pr_create", "low", [], config()) == PROPOSE


# ── Rule 2: the streak is per (kind, risk) and must be CONSECUTIVE ──────────


def test_streak_does_not_borrow_from_another_kind() -> None:
    assert autonomy_policy("draft_pr_create", "low", clean("retry_idempotent", "medium", 5), config()) == PROPOSE


def test_streak_does_not_borrow_across_risk() -> None:
    ledger = clean("draft_pr_create", "high", 5)
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == PROPOSE


def test_skipped_neither_builds_nor_breaks_a_streak() -> None:
    ledger = rows(
        ("draft_pr_create", "low", "clean"),
        ("draft_pr_create", "low", "skipped"),
        ("draft_pr_create", "low", "clean"),
        ("draft_pr_create", "low", "clean"),
    )
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == AUTO


# ── Rule 3: one reversal resets the streak AND doubles the bar ──────────────


def test_single_reversal_resets_the_streak() -> None:
    ledger = clean("draft_pr_create", "low", 2) + rows(("draft_pr_create", "low", "reversal"))
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == PROPOSE


def test_after_a_reversal_the_bar_doubles() -> None:
    """Three cleans used to be enough. After one reversal it takes six."""
    after = rows(("draft_pr_create", "low", "reversal")) + clean("draft_pr_create", "low", 5)
    assert autonomy_policy("draft_pr_create", "low", after, config()) == PROPOSE
    six = rows(("draft_pr_create", "low", "reversal")) + clean("draft_pr_create", "low", 6)
    assert autonomy_policy("draft_pr_create", "low", six, config()) == AUTO


def test_a_post_hoc_failure_counts_as_a_reversal() -> None:
    ledger = clean("draft_pr_create", "low", 3) + rows(("draft_pr_create", "low", "failure"))
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == PROPOSE


# ── Rule 4: a track record earned under different rules is not a track record ─


def test_rows_spanning_two_cartridge_shas_are_refused_not_averaged() -> None:
    ledger = clean("draft_pr_create", "low", 3) + rows(("draft_pr_create", "low", "clean"), sha="sha-2")
    with pytest.raises(PolicyError, match="span 2 values of 'cartridge_sha'"):
        autonomy_policy("draft_pr_create", "low", ledger, config())


def test_rows_spanning_two_provider_profiles_are_refused() -> None:
    ledger = clean("draft_pr_create", "low", 3) + rows(("draft_pr_create", "low", "clean"), profile="other")
    with pytest.raises(PolicyError, match="span 2 values of 'provider_profile'"):
        autonomy_policy("draft_pr_create", "low", ledger, config())


# ── Rule 5: caps bound a graduated kind, and overflow does not punish it ────


def test_cap_forces_propose_once_the_run_ceiling_is_hit() -> None:
    ledger = clean("draft_pr_create", "low", 3)
    assert autonomy_policy("draft_pr_create", "low", ledger, config(caps={"draft_pr_create": 2})) == AUTO
    capped = config(caps={"draft_pr_create": 2}, applied_this_run=2)
    assert autonomy_policy("draft_pr_create", "low", ledger, capped) == PROPOSE


def test_overflow_does_not_reset_the_streak() -> None:
    """Hitting a cap is the policy working, not the kind misbehaving."""
    ledger = clean("draft_pr_create", "low", 3)
    capped = config(caps={"draft_pr_create": 1}, applied_this_run=1)
    assert autonomy_policy("draft_pr_create", "low", ledger, capped) == PROPOSE
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == AUTO


# ── deferred waits for the eligible kinds ──────────────────────────────────


def test_deferred_kind_waits_even_with_its_own_clean_streak() -> None:
    ledger = clean("ticket_create", "low", 10)
    assert autonomy_policy("ticket_create", "low", ledger, config()) == PROPOSE


def test_deferred_kind_graduates_once_every_eligible_kind_has() -> None:
    ledger = (
        clean("draft_pr_create", "low", 3) + clean("retry_idempotent", "medium", 3) + clean("ticket_create", "low", 3)
    )
    assert autonomy_policy("ticket_create", "low", ledger, config()) == AUTO


def test_deferred_kind_still_waits_if_one_eligible_kind_lags() -> None:
    ledger = clean("draft_pr_create", "low", 3) + clean("ticket_create", "low", 5)
    assert autonomy_policy("ticket_create", "low", ledger, config()) == PROPOSE


# ── refusing to guess ──────────────────────────────────────────────────────


def test_unknown_kind_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(PolicyError, match="unknown write kind 'invented_by_a_node'"):
        autonomy_policy("invented_by_a_node", "low", [], config())
