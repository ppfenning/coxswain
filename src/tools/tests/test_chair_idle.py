from agent_tools.chair_decompose_streak import DecomposeStreak
from agent_tools.chair_idle import Idle, is_idle, next_wait
from agent_tools.chair_wake import new_wake_events, wait_for_events

KINDS = ("run_exited", "intake", "chat_directive")
INTAKE_ENTRY = {"id": "a", "ack": False, "ref": "coxswain://intake/x"}
DIRECTIVE_ENTRY = {"id": "b", "ack": False, "event": "chat_directive", "ref": "coxswain://run/r1"}


def _facts(**over):
    return {"initiatives": [], "approved": [], "intake": [], **over}


def _task(**over):
    row = {"id": "t", "initiative": "i", "repo": "/r", "phase": "p", "phase_done": True, "needs": [], "run": "r1", "needs_fetch": False, "merged": False}
    return {**row, **over}


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _wake_once(entry):
    clock, bus = Clock(), [entry]
    wait = wait_for_events(lambda: bus, clock.sleep, clock.now, 5, KINDS)
    return wait(60), wait(60)


def test_all_empty_facts_is_idle():
    assert is_idle(_facts()) == Idle(True, "")


def test_one_ready_task_is_not_idle():
    result = is_idle(_facts(initiatives=[{"id": "i", "started": False, "ready_tasks": [{"id": "t"}], "landed": set()}]))
    assert result == Idle(False, "ready task in i")


def test_one_landable_approval_is_not_idle():
    assert is_idle(_facts(approved=[_task()])) == Idle(False, "land planned: land_phase")


def test_a_manual_land_approval_is_idle_because_plan_lands_skips_it():
    assert is_idle(_facts(approved=[_task(land_manual=True)])).idle


def test_an_approval_awaiting_its_review_pr_is_idle():
    review = [{"initiative": "i", "phase": "p", "task_id": "t", "state": "open"}]
    assert is_idle(_facts(approved=[_task()], review_prs=review)).idle


def test_a_frozen_land_is_idle():
    assert is_idle(_facts(approved=[_task()], land_freeze={"until": "2026-11-11T00:00:00+00:00"})).idle


def test_one_ready_intake_is_not_idle():
    assert is_idle(_facts(intake=["intake/a.md"])) == Idle(False, "1 queued intake")


def test_a_stalled_intake_alone_is_idle():
    stalled = {"intake/a.md": DecomposeStreak(3, ("i-1", "i-2", "i-3"), None)}
    assert is_idle(_facts(intake=["intake/a.md"], decompose_streaks=stalled)).idle


def test_a_ready_intake_beside_a_stalled_one_is_not_idle():
    stalled = {"intake/a.md": DecomposeStreak(3, ("i-1", "i-2", "i-3"), None)}
    assert not is_idle(_facts(intake=["intake/a.md", "intake/b.md"], decompose_streaks=stalled)).idle


def test_one_pending_directive_is_not_idle():
    assert is_idle(_facts(), 1) == Idle(False, "1 pending directive(s)")


def test_idle_ceiling_is_the_fallback_and_busy_is_the_interval():
    assert next_wait(True, 30, 600) == (30, 600)
    assert next_wait(False, 30, 600) == (30, 30)


def test_an_intake_entry_wakes_once():
    assert _wake_once(INTAKE_ENTRY) == (True, False)


def test_a_directive_entry_wakes_once():
    assert _wake_once(DIRECTIVE_ENTRY) == (True, False)


def test_kinds_filter_excludes_unlisted_kinds():
    assert new_wake_events([INTAKE_ENTRY, DIRECTIVE_ENTRY], set(), ("chat_directive",)) == [DIRECTIVE_ENTRY]


def test_an_event_on_an_intake_ref_is_not_new_intake():
    answer = {**INTAKE_ENTRY, "event": "chat_answer"}
    assert new_wake_events([answer], set(), KINDS) == []


def test_an_acked_entry_does_not_wake():
    assert new_wake_events([{**INTAKE_ENTRY, "ack": True}], set(), KINDS) == []
