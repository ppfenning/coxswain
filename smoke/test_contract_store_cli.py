"""Contract: `cox` <-> `python -m harness.store_cli set-state`, the store's single writer of work state.

`cox` (route edit, land, recover, the chair) moves a task by running this command under the harness python. Every
state a caller sends must be one the command's parser takes: one it refuses is exit 2 with "invalid choice", and the
callers' unit tests stub the command, so they never see it. That is how every triage request and hold failed on the
live store until #1174.
"""

import pytest

STATES_PROBE = "import json; from core.workstore import STATES; print(json.dumps(list(STATES)))"
EDIT_PROBE = "import json; from agent_tools.route_edit import SET_STATES; print(json.dumps(list(SET_STATES)))"
# Every target state a cox caller sends, with one caller each. `cox route edit` sends route_edit.SET_STATES.
TARGETS = {
    "ready": "cox route edit --state ready; the chair's triage request",
    "approved": "cox route edit --state approved; cox runs land",
    "done": "cox route edit --state done; cox runs recover; the chair's review apply",
    "dropped": "cox route edit --state dropped; the chair's drop remedy",
    "blocked": "the chair's hold for a person",
    "quarantined": "the chair's revert of a phase, cli._quarantine_phase_with",
}
# Drift this suite found and has not closed. Each entry is a strict xfail, so whoever closes it must delete the entry.
# `quarantined`, the chair's revert of a phase, was the first: set-state now takes every state in core.workstore.STATES.
KNOWN_DRIFT: dict[str, str] = {}


def _set_state(sandbox, state, *extra):
    argv = ("--store-url", sandbox.store_url, "set-state", "ship", "t1", state, "--by", "smoke", *extra)
    return sandbox.harness("harness.store_cli", *argv)


def _assert_taken(done, what):
    # exit 0 applied, exit 3 a refused precondition: either way the parser took the arguments. exit 2 is the drift.
    assert done.returncode in (0, 3), f"{what}: exit {done.returncode}: {done.stderr.strip().splitlines()[-1:]}"


def _seed(sandbox):
    seeded = _set_state(sandbox, "ready", "--phase", "p1")
    assert seeded.returncode == 0, seeded.stderr


def test_set_state_takes_every_work_state_as_its_expect(sandbox):
    _seed(sandbox)
    states = sandbox.python_json(STATES_PROBE)
    assert states, "core.workstore.STATES is empty"
    for state in states:
        _assert_taken(_set_state(sandbox, "ready", "--expect", state), f"--expect {state}")


def test_the_targets_listed_here_cover_what_route_edit_sends(sandbox):
    # A state added to route_edit.SET_STATES must be added to TARGETS, so the test below runs it through the command.
    assert set(sandbox.python_json(EDIT_PROBE)) <= TARGETS.keys()


TARGET_PARAMS = [
    pytest.param(state, id=state, marks=[pytest.mark.xfail(strict=True, reason=KNOWN_DRIFT[state])] if state in KNOWN_DRIFT else [])
    for state in TARGETS
]


@pytest.mark.parametrize("target", TARGET_PARAMS)
def test_set_state_takes_every_target_cox_sends(sandbox, target):
    _seed(sandbox)
    _assert_taken(_set_state(sandbox, target), f"set-state {target} ({TARGETS[target]})")
    assert sandbox.store_row("ship", "t1")["state"] == target
