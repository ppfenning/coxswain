from itertools import product

import pytest

from harness.carry import (
    REASON_DOES_NOT_APPLY,
    REASON_NOT_FOUND,
    CarriedFrom,
    Carry,
    Facts,
    Fetched,
    MarkDone,
    NotFetched,
    NotOnBase,
    OnBase,
    Rebuild,
    Skip,
    carried_from_record,
    decide,
    next_step,
)


def _facts(**kw) -> Facts:
    return Facts(task_id="t1", merged_commit="abc123", **kw)


def _off_branch(**kw) -> Facts:
    return _facts(on_phase_branch=False, claimed_by_active_land=False, **kw)


def test_on_the_phase_branch_is_skipped():
    assert decide(_facts(on_phase_branch=True)) == Skip()


def test_claimed_by_an_active_land_is_skipped():
    assert decide(_facts(on_phase_branch=False, claimed_by_active_land=True)) == Skip()


def test_a_claim_beats_a_landed_change():
    facts = _facts(on_phase_branch=False, claimed_by_active_land=True, base=OnBase(7))
    assert decide(facts) == Skip()


def test_landed_on_base_is_marked_done_with_the_pull_request():
    assert decide(_off_branch(base=OnBase(7))) == MarkDone(7)


def test_fetched_and_applied_cleanly_is_carried():
    facts = _off_branch(base=NotOnBase(), fetch=Fetched("origin"), applies_cleanly=True)
    assert decide(facts) == Carry(CarriedFrom("abc123", "origin"))


def test_not_fetchable_is_rebuilt_as_not_found():
    assert decide(_off_branch(base=NotOnBase(), fetch=NotFetched())) == Rebuild("commit not found")
    assert REASON_NOT_FOUND == "commit not found"


def test_not_applying_cleanly_is_rebuilt():
    facts = _off_branch(base=NotOnBase(), fetch=Fetched("origin"), applies_cleanly=False)
    assert decide(facts) == Rebuild("does not apply cleanly to the current base")
    assert REASON_DOES_NOT_APPLY == "does not apply cleanly to the current base"


def test_a_fetch_without_a_source_is_rejected():
    with pytest.raises(ValueError):
        Fetched("")


def test_probes_are_asked_in_precedence_order():
    assert next_step(_facts()) == "on_phase_branch"
    assert next_step(_facts(on_phase_branch=False)) == "claimed"
    assert next_step(_off_branch()) == "landed_on_base"
    assert next_step(_off_branch(base=NotOnBase())) == "fetch"
    assert next_step(_off_branch(base=NotOnBase(), fetch=Fetched("origin"))) == "apply"


def test_decide_answers_only_when_no_probe_is_left():
    states = (
        (None, True, False),
        (None, True, False),
        (None, OnBase(7), NotOnBase()),
        (None, Fetched("origin"), NotFetched()),
        (None, True, False),
    )
    for on_branch, claimed, base, fetch, applies in product(*states):
        facts = _facts(
            on_phase_branch=on_branch,
            claimed_by_active_land=claimed,
            base=base,
            fetch=fetch,
            applies_cleanly=applies,
        )
        assert (decide(facts) is None) == (next_step(facts) is not None), facts


def test_carried_from_record_is_the_stored_dict():
    carry = Carry(CarriedFrom("abc123", "origin"))
    assert carried_from_record(carry) == {"commit": "abc123", "fetched_from": "origin"}
