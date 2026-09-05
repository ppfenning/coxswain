"""Nothing but dicts and lists, as the contract requires.

These tests are the argument that autonomy is earned rather than assumed, so
they check the ways a kind must FAIL to graduate at least as hard as the one
way it succeeds.

One exception: `test_loading_local_resolves_base_cartridges_plan_competition_min_tier`
loads the real `local` cartridge off disk, because the fact under test is
what the shipped cartridge chain resolves to, not a function's behaviour on a
literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.policy import AUTO, PROPOSE, PolicyError, autonomy_policy, plan_tier
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


def clean(kind: str, risk: str, n: int, **row_kwargs):
    return rows(*[(kind, risk, "clean")] * n, **row_kwargs)


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


# ── Rule 7: trust is earned per subject, where the caller names one ────────


def test_a_subject_graduates_on_its_own_record_and_its_sibling_does_not() -> None:
    """rb-04 has been right three times. rb-09 has never been run at all."""
    ledger = clean("draft_pr_create", "low", 3, subject="rb-04")
    assert autonomy_policy("draft_pr_create", "low", ledger, config(subject="rb-04")) == AUTO
    assert autonomy_policy("draft_pr_create", "low", ledger, config(subject="rb-09")) == PROPOSE


def test_a_reversal_resets_only_the_subject_it_was_against() -> None:
    ledger = (
        clean("draft_pr_create", "low", 3, subject="rb-04")
        + clean("draft_pr_create", "low", 3, subject="rb-09")
        + rows(("draft_pr_create", "low", "reversal"), subject="rb-04")
    )
    assert autonomy_policy("draft_pr_create", "low", ledger, config(subject="rb-04")) == PROPOSE
    assert autonomy_policy("draft_pr_create", "low", ledger, config(subject="rb-09")) == AUTO


def test_a_subject_with_no_history_proposes_even_where_the_kind_is_trusted() -> None:
    ledger = clean("draft_pr_create", "low", 10, subject="rb-04")
    assert autonomy_policy("draft_pr_create", "low", ledger, config(subject="rb-77")) == PROPOSE


def test_a_subjectless_caller_falls_back_to_the_kind_and_counts_every_row() -> None:
    """No subject in the config means the old reading: all rows of the kind."""
    ledger = clean("draft_pr_create", "low", 2, subject="rb-04") + clean("draft_pr_create", "low", 1, subject="rb-09")
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == AUTO


def test_the_kind_level_fallback_carries_another_subjects_reversals() -> None:
    """The strict direction of error: a bad entry weighs on the whole kind."""
    ledger = rows(("draft_pr_create", "low", "reversal"), subject="rb-04") + clean(
        "draft_pr_create", "low", 5, subject="rb-09"
    )
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == PROPOSE, "bar doubled to six"
    assert autonomy_policy("draft_pr_create", "low", ledger, config(subject="rb-09")) == AUTO


def test_deferred_still_waits_on_the_kind_level_record_not_the_subjects() -> None:
    """`are the basics trusted` is a question about the taxonomy, not an entry."""
    ledger = (
        clean("draft_pr_create", "low", 3, subject="rb-04")
        + clean("retry_idempotent", "medium", 3, subject="rb-04")
        + clean("ticket_create", "low", 3, subject="rb-04")
    )
    assert autonomy_policy("ticket_create", "low", ledger, config(subject="rb-04")) == AUTO


def test_a_new_subject_always_proposes_even_on_a_graduated_kind() -> None:
    """Creating the entry is the one act no track record can vouch for."""
    ledger = clean("draft_pr_create", "low", 10)
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == AUTO
    assert autonomy_policy("draft_pr_create", "low", ledger, config(subject_new=True)) == PROPOSE


# ── Rule 8: a pass that took three tries is not a first-try pass ────────────


def test_a_clean_that_took_several_attempts_neither_builds_nor_breaks() -> None:
    """Transparent, exactly as `skipped` is. The first-try cleans still count."""
    ledger = (
        clean("draft_pr_create", "low", 1)
        + clean("draft_pr_create", "low", 1, attempts=3)
        + clean("draft_pr_create", "low", 2)
    )
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == AUTO


def test_attempts_cleans_alone_never_graduate_a_kind() -> None:
    """A fix loop that always converges on the third try has proved nothing."""
    ledger = clean("draft_pr_create", "low", 10, attempts=3)
    assert autonomy_policy("draft_pr_create", "low", ledger, config()) == PROPOSE


def test_attempts_one_is_a_first_try_clean() -> None:
    assert autonomy_policy("draft_pr_create", "low", clean("draft_pr_create", "low", 3, attempts=1), config()) == AUTO


def test_a_reversal_resets_and_doubles_however_many_attempts_it_took() -> None:
    """Reading attempts on a reversal would let the fix loop buy out the ratchet."""
    after = rows(("draft_pr_create", "low", "reversal"), attempts=4) + clean("draft_pr_create", "low", 5)
    assert autonomy_policy("draft_pr_create", "low", after, config()) == PROPOSE
    six = rows(("draft_pr_create", "low", "reversal"), attempts=4) + clean("draft_pr_create", "low", 6)
    assert autonomy_policy("draft_pr_create", "low", six, config()) == AUTO


# ── refusing to guess ──────────────────────────────────────────────────────


def test_unknown_kind_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(PolicyError, match="unknown write kind 'invented_by_a_node'"):
        autonomy_policy("invented_by_a_node", "low", [], config())


# ── plan_tier: the pre-build gate, from surfaces and patterns alone ─────────

REVIEW_TIER_CARTRIDGE = {
    "policy": {
        "review_tier": {
            "tier0_patterns": ["docs_only", "rename_only"],
            "tier2_surfaces": ["schema", "auth"],
        }
    }
}


def test_a_dangerous_surface_gives_tier_two_regardless_of_patterns() -> None:
    assert plan_tier(REVIEW_TIER_CARTRIDGE, surfaces=["auth"], patterns=["docs_only"]) == 2


def test_a_tier_zero_pattern_with_no_dangerous_surface_gives_tier_zero() -> None:
    assert plan_tier(REVIEW_TIER_CARTRIDGE, surfaces=["ui"], patterns=["docs_only"]) == 0


def test_neither_a_dangerous_surface_nor_a_tier_zero_pattern_gives_tier_one() -> None:
    assert plan_tier(REVIEW_TIER_CARTRIDGE, surfaces=["ui"], patterns=["feature_add"]) == 1


def test_empty_review_tier_lists_give_tier_one_for_anything() -> None:
    empty = {"policy": {"review_tier": {}}}
    assert plan_tier(empty, surfaces=["auth"], patterns=["docs_only"]) == 1
