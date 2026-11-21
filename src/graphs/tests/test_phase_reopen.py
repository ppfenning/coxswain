"""`phase_reopen`, pure: which queue rows `cox worker reopen` reopens, the change it writes, and what it says."""

import pytest

from harness import phase_reopen


def _row(state="finished", outcome="failed", **extra):
    return {"initiative": "ship", "phase": "p1", "state": state, "outcome": outcome, "claim_epoch": 4, "attempts": 3,
            "requires_json": ["docker", "repo:app"], "band": 1, "seq": 2000, "reason": "3 claims and none finished it",
            "claimed_by": "", **extra}


@pytest.mark.parametrize(("row", "verdict"), [
    (_row(), "reopen"),
    (_row(outcome="quarantined"), "quarantined"),  # triage owns it; the chair reopens it once its tasks are ready
    (_row(outcome="approved"), "approved"),
    (_row(outcome="revised"), "other"),  # an initiative review the chair re-reviews once its revision merged
    (_row(state="claimed", outcome=""), "claimed"),
    (_row(state="ready", outcome=""), "already"),
    (_row(state="waiting", outcome=""), "other"),
    (_row(state="cancelled", outcome=""), "other"),  # withdrawn: it comes back when its hold is released
    (_row(state="built", outcome=""), "other"),
    (None, "absent"),  # no queue row: not a phase on the new model
])
def test_only_a_finished_failed_row_is_reopened(row, verdict):
    assert phase_reopen.verdict(row) == verdict


def test_the_change_is_the_chairs_reopen_at_the_epoch_read_keeping_the_rows_requirements_and_place():
    assert phase_reopen.change(_row(), "pat", "the flaky\n check   is fixed") == {
        "op": "reopen", "initiative": "ship", "phase": "p1", "requires": ["docker", "repo:app"], "band": 1,
        "seq": 2000, "epoch": 4, "reason": "reopened by pat: the flaky check is fixed"}
    assert phase_reopen.change(_row(requires_json=None), "pat", "why")["requires"] == []


@pytest.mark.parametrize(("text", "key"), [
    ("ship/p1", ("ship", "p1")),
    ("work/ship/p1", ("work/ship", "p1")),  # split at the last slash
    (" ship/initiative ", ("ship", "initiative")),
    ("ship", None), ("ship/", None), ("/p1", None), ("", None),
])
def test_the_target_is_initiative_slash_phase(text, key):
    assert phase_reopen.target(text) == key


def test_the_note_says_what_changed_and_why_each_refusal_changed_nothing():
    after = _row(state="ready", outcome="", attempts=0, reason="reopened by pat: fixed")
    said = phase_reopen.note("ship", "p1", "reopen", _row(), after)
    assert said.startswith("worker: ship/p1 was finished failed (epoch 4, 3 attempt(s): 3 claims and none finished it)")
    assert 'it is ready now with 0 attempts, "reopened by pat: fixed", and a new review episode' in said
    assert phase_reopen.note("ship", "p1", "already", after, after) == (
        "worker: ship/p1 is ready already (0 attempt(s)); nothing changed")
    assert "not a phase on the new model; nothing changed" in phase_reopen.note("ship", "p1", "absent", None, None)
    claimed = _row(state="claimed", outcome="", claimed_by="w1")
    assert phase_reopen.note("ship", "p1", "claimed", claimed, claimed) == (
        "worker: ship/p1 is claimed by w1 (epoch 4); nothing changed")
    assert "finished approved; there is nothing to retry" in phase_reopen.note(
        "ship", "p1", "approved", _row(outcome="approved"), None)
    assert phase_reopen.note("ship", "p1", "quarantined", _row(outcome="quarantined"), None).startswith(
        "worker: ship/p1 finished quarantined: triage owns it")
    assert phase_reopen.note("ship", "p1", "other", _row(state="cancelled", outcome=""), None) == (
        "worker: ship/p1 is cancelled, not finished failed; the chair moves it; nothing changed")
    assert phase_reopen.note("ship", "p1", "other", _row(outcome="revised"), None).startswith(
        "worker: ship/p1 is finished revised, not")


def test_a_release_names_each_failed_phase_it_leaves_and_the_reopen_that_retries_it():
    assert phase_reopen.release_hints("ship", []) == []
    assert phase_reopen.release_hints("ship", ["p1", "p2"]) == [
        "release: phase ship/p1 is finished failed; reopen it with `cox worker reopen ship/p1 --reason ...`",
        "release: phase ship/p2 is finished failed; reopen it with `cox worker reopen ship/p2 --reason ...`"]
