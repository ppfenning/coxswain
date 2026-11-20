"""The planner skips an intake whose idea file was missing, for a doubling backoff, then raises one needs_chair."""

from datetime import UTC, datetime, timedelta

from test_chair_decompose_gate import _facts

from agent_tools.chair_decompose_miss import DecomposeMiss
from agent_tools.chair_plan import plan_tick

PATH = "intake/2026-11-11-some-idea.md"
NEXT = "intake/2026-11-12-next-idea.md"
AT = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def _miss(count: int, fingerprint: str = "fp-1") -> DecomposeMiss:
    return DecomposeMiss("2026-11-11-some-idea", PATH, count, AT.isoformat(), fingerprint)


def _plan(miss: DecomposeMiss | None, minutes: float, *, current: str = "fp-1", reported: tuple[str, ...] = ()) -> list:
    facts = _facts(
        intake=[PATH, NEXT],
        dispatch={"max_in_flight": 1, "live_runs": 0, "hosts": []},
        limits={"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False},
        decompose_misses={PATH: miss} if miss else {},
        decompose_miss_fingerprints={PATH: current} if miss else {},
        decompose_misses_reported=list(reported),
    )
    return plan_tick(facts, AT + timedelta(minutes=minutes))


def _launched(actions: list) -> list[str]:
    return [path for a in actions if a["kind"] == "launch_decompose" for path in a["intake_ids"]]


def _needs_chair(actions: list) -> list:
    return [a for a in actions if a["kind"] == "needs_chair"]


def test_with_no_miss_the_first_intake_takes_the_one_slot() -> None:
    assert _launched(_plan(None, 1)) == [PATH]


def test_a_first_miss_inside_its_window_hands_the_slot_to_the_next_intake() -> None:
    actions = _plan(_miss(1), 3)
    assert _launched(actions) == [NEXT]
    assert _needs_chair(actions) == []


def test_a_first_miss_launches_again_once_its_four_tick_window_has_passed() -> None:
    assert _launched(_plan(_miss(1), 4)) == [PATH]


def test_a_second_miss_waits_eight_ticks() -> None:
    assert _launched(_plan(_miss(2), 7)) == [NEXT]
    assert _launched(_plan(_miss(2), 8)) == [PATH]


def test_a_third_miss_raises_one_needs_chair_naming_the_intake_and_the_path() -> None:
    actions = _plan(_miss(3), 1)
    [raised] = _needs_chair(actions)
    assert raised["cause"] == "decompose_missing_idea"
    assert (raised["intake"], raised["fingerprint"], raised["initiative"]) == (PATH, "fp-1", "2026-11-11-some-idea")
    assert PATH in raised["reason"] and "3 times" in raised["reason"]
    assert _launched(actions) == [NEXT]


def test_a_reported_third_miss_raises_nothing_and_stays_skipped_past_the_cap() -> None:
    actions = _plan(_miss(3), 60 * 24 * 30, reported=(f"{PATH}|fp-1",))
    assert _needs_chair(actions) == []
    assert _launched(actions) == [NEXT]


def test_a_report_for_an_older_fingerprint_does_not_silence_a_new_escalation() -> None:
    actions = _plan(_miss(3, "fp-2"), 1, current="fp-2", reported=(f"{PATH}|fp-1",))
    assert len(_needs_chair(actions)) == 1


def test_a_changed_intake_is_not_skipped_and_raises_nothing() -> None:
    for count in (1, 3):
        actions = _plan(_miss(count), 1, current="fp-2")
        assert _launched(actions) == [PATH]
        assert _needs_chair(actions) == []


def test_a_miss_with_no_current_fingerprint_is_not_skipped() -> None:
    facts = _facts(intake=[PATH], decompose_misses={PATH: _miss(3)}, decompose_miss_fingerprints={})
    assert _launched(plan_tick(facts, AT)) == [PATH]


def test_with_no_clock_a_miss_on_an_unchanged_intake_is_skipped() -> None:
    facts = _facts(intake=[PATH], decompose_misses={PATH: _miss(1)}, decompose_miss_fingerprints={PATH: "fp-1"})
    assert _launched(plan_tick(facts)) == []


def test_a_skipped_intake_plans_no_pull() -> None:
    facts = _facts(
        intake=[PATH], work_store_ready=False, sources_configured=True,
        decompose_misses={PATH: _miss(1)}, decompose_miss_fingerprints={PATH: "fp-1"},
    )
    assert [a for a in plan_tick(facts, AT) if a["kind"] == "pull"] == []
