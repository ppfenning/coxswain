from agent_tools.chair_facts import stale_draft_keys
from agent_tools.stale_draft import EXPORT_RESTORES, HELD_REFUSAL


def _row(initiative: str, stale_tasks: list[str], status: str, reason: str = "") -> dict:
    return {"action_json": {"kind": "stale_to_draft", "initiative": initiative, "stale_tasks": stale_tasks,
                            "status": status, "reason": reason}}


def test_a_done_stale_to_draft_gives_its_initiative_and_sorted_stale_tasks():
    rows = [
        _row("a", ["a-2", "a-1"], "done"),
        {"action_json": '{"kind": "stale_to_draft", "initiative": "b", "stale_tasks": ["b-1"], "status": "done"}'},
        {"action_json": {"kind": "needs_chair", "initiative": "c", "stale_tasks": ["c-1"], "status": "done"}},
        {"action_json": {"kind": "stale_to_draft", "stale_tasks": ["x"], "status": "done"}},
    ]
    assert stale_draft_keys(rows) == ["a|a-1,a-2", "b|b-1"]


def test_a_refusal_that_repeats_on_every_tick_counts_as_reported():
    rows = [
        _row("a", ["a-1"], "refused", f"{EXPORT_RESTORES} work/a/initiative.md"),
        _row("b", ["b-1"], "refused", HELD_REFUSAL),
    ]
    assert stale_draft_keys(rows) == ["a|a-1", "b|b-1"]


def test_a_transient_refusal_is_not_reported_so_the_next_tick_retries():
    rows = [
        _row("a", ["a-1"], "refused", "no initiative.md at /work/a/initiative.md"),
        _row("b", ["b-1"], "refused", "no ticket named b-1"),
        _row("c", ["c-1"], "failed", "boom"),
    ]
    assert stale_draft_keys(rows) == []
