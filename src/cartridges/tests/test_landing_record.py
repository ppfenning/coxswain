"""The landing record's state tables: phase, wave and initiative states derived from the record and the task rows."""
from __future__ import annotations

import pytest

from core import landing_record as lr

NOW = "2026-11-12T10:00:00Z"
LATER = "2026-11-12T10:05:00Z"


def task(ident: str, phase: str, state: str = "ready", needs: tuple[str, ...] = ()) -> dict:
    return {"id": ident, "phase": phase, "state": state, "needs": list(needs)}


def record(**phases: dict) -> dict:
    base = lr.new_record("i", repo="~/repos/x", branch="init/i", parent="main", base_sha="b" * 40, now=NOW)
    return {**base, "phases": phases}


# Two waves: p1 and p2 have no parent; p3 needs both; p10 needs p3.
TWO_WAVES = [
    task("a", "p1"), task("b", "p2"), task("c", "p3", needs=("a", "b")), task("d", "p10", needs=("c",)),
]


def test_phase_parents_come_from_task_needs_across_phases() -> None:
    assert lr.phase_parents(TWO_WAVES) == {"p1": set(), "p2": set(), "p3": {"p1", "p2"}, "p10": {"p3"}}


def test_waves_are_depths_and_merge_order_is_parents_first_in_natural_order() -> None:
    parents = lr.phase_parents(TWO_WAVES)
    assert lr.waves(parents) == [["p1", "p2"], ["p3"], ["p10"]]
    assert lr.merge_order(parents) == ["p1", "p2", "p3", "p10"]
    assert lr.dependents(parents, "p1") == {"p3", "p10"}


def test_the_integrate_phase_needs_every_other_phase() -> None:
    tasks = [task("a", "p1"), task("b", "p2"), task("fix", lr.INTEGRATE_PHASE)]
    assert lr.phase_parents(tasks)[lr.INTEGRATE_PHASE] == {"p1", "p2"}


def test_a_new_initiative_has_wave_one_open_and_the_rest_waiting() -> None:
    assert lr.phase_states(record(), TWO_WAVES) == {"p1": "open", "p2": "open", "p3": "waiting", "p10": "waiting"}
    assert lr.placeable_phases(record(), TWO_WAVES) == ["p1", "p2"]
    assert lr.mergeable_phases(record(), TWO_WAVES) == []
    assert lr.initiative_state(record(), TWO_WAVES) == "building"


def test_a_phase_with_every_task_approved_is_built_and_only_a_merge_opens_its_children() -> None:
    tasks = [task("a", "p1", "approved"), task("b", "p2", "approved"), *TWO_WAVES[2:]]
    assert lr.phase_states(record(), tasks) == {"p1": "built", "p2": "built", "p3": "waiting", "p10": "waiting"}
    assert lr.mergeable_phases(record(), tasks) == ["p1", "p2"]
    one = record(p1={"state": "merged", "tip": "1" * 40, "merge_sha": "2" * 40})
    assert lr.phase_states(one, tasks)["p3"] == "waiting", "p2 is built but not merged"
    both = record(p1={"state": "merged"}, p2={"state": "merged"})
    assert lr.phase_states(both, tasks) == {"p1": "merged", "p2": "merged", "p3": "open", "p10": "waiting"}
    assert lr.placeable_phases(both, tasks) == ["p3"]


def test_a_queued_phase_reads_queued_until_it_merges() -> None:
    tasks = [task("a", "p1", "approved")]
    assert lr.phase_states(record(p1={"state": "queued"}), tasks) == {"p1": "queued"}
    assert lr.mergeable_phases(record(p1={"state": "queued"}), tasks) == ["p1"]


def test_a_phase_whose_tasks_are_all_done_reads_merged_whatever_the_record_says() -> None:
    tasks = [task("a", "p1", "done"), task("x", "p1", "dropped"), task("c", "p3", needs=("a",))]
    assert lr.phase_states(None, tasks) == {"p1": "merged", "p3": "open"}


def test_waves_go_green_when_every_phase_of_the_wave_is_merged() -> None:
    tasks = [task("a", "p1", "approved"), task("b", "p2", "approved"), *TWO_WAVES[2:]]
    rec = {**record(p1={"state": "merged"}, p2={"state": "merged"}), "waves": {"1": {"green_tip": "9" * 40}}}
    assert lr.wave_states(rec, tasks) == [
        {"wave": 1, "phases": ["p1", "p2"], "state": "green", "green_tip": "9" * 40},
        {"wave": 2, "phases": ["p3"], "state": "open", "green_tip": None},
        {"wave": 3, "phases": ["p10"], "state": "open", "green_tip": None},
    ]


def test_blocked_stands_while_a_task_is_quarantined_and_conflicted_while_a_rectify_task_is_open() -> None:
    quarantined = [task("a", "p1", "quarantined")]
    assert lr.phase_states(record(p1={"state": "blocked"}), quarantined) == {"p1": "blocked"}
    assert lr.initiative_state(record(p1={"state": "blocked"}), quarantined) == "blocked"
    released = [task("a", "p1", "ready")]
    assert lr.phase_states(record(p1={"state": "blocked"}), released) == {"p1": "open"}
    rectifying = [task("a", "p1", "approved"), task("a-rectify", "p1", "ready")]
    assert lr.phase_states(record(p1={"state": "conflicted"}), rectifying) == {"p1": "conflicted"}
    assert lr.placeable_phases(record(p1={"state": "conflicted"}), rectifying) == ["p1"], "the rectify task builds there"
    assert lr.initiative_state(record(p1={"state": "conflicted"}), rectifying) == "conflicted"
    rectified = [task("a", "p1", "approved"), task("a-rectify", "p1", "approved")]
    assert lr.phase_states(record(p1={"state": "conflicted"}), rectified) == {"p1": "built"}


def test_the_initiative_is_assembled_when_every_phase_is_merged_then_pr_open_then_landed() -> None:
    tasks = [task("a", "p1", "approved"), task("b", "p2", "approved")]
    merged = record(p1={"state": "merged"}, p2={"state": "merged"})
    assert lr.initiative_state(merged, tasks) == "assembled"
    assert lr.initiative_state({**merged, "state": "pr_open"}, tasks) == "pr_open"
    assert lr.initiative_state({**merged, "state": "landed"}, tasks) == "landed"
    assert lr.initiative_state(None, [task("a", "p1", "done"), task("b", "p2", "dropped")]) == "landed"
    assert lr.initiative_state(None, []) == "draft"


def test_merged_tasks_are_the_approved_tasks_of_merged_phases_only() -> None:
    tasks = [task("a", "p1", "approved"), task("b", "p2", "approved"), task("c", "p3", "ready", needs=("a",))]
    assert lr.merged_tasks(record(p1={"state": "merged"}), tasks) == ["a"]
    assert lr.merged_tasks(record(), tasks) == []


def test_phase_moves_follow_the_table_and_merged_is_final() -> None:
    rec = lr.with_phase(record(), "p1", NOW, state="queued")
    rec = lr.with_phase(rec, "p1", LATER, state="merged", tip="1" * 40, merge_sha="2" * 40)
    assert rec["phases"]["p1"] == {"state": "merged", "tip": "1" * 40, "merge_sha": "2" * 40, "updated_at": LATER}
    assert rec["updated_at"] == LATER
    with pytest.raises(lr.RecordError, match="p1 cannot move from merged to open"):
        lr.with_phase(rec, "p1", LATER, state="open")
    assert lr.with_phase(rec, "p1", LATER, note="kept")["phases"]["p1"]["state"] == "merged"


def test_initiative_moves_follow_the_table() -> None:
    rec = lr.with_state(record(), "assembled", LATER)
    rec = lr.with_state(rec, "pr_open", LATER, pr={"number": 7, "url": "https://forge.example/pull/7"})
    assert (rec["state"], rec["pr"]["number"]) == ("pr_open", 7)
    rec = lr.with_state(rec, "landed", LATER)
    with pytest.raises(lr.RecordError, match="cannot move from landed to building"):
        lr.with_state(rec, "building", LATER)
    with pytest.raises(lr.RecordError, match="cannot move from building to pr_open"):
        lr.with_state(record(), "pr_open", LATER)


def test_recorded_shas_are_the_base_and_every_merged_phase_tip_and_merge() -> None:
    rec = record(
        p1={"state": "merged", "tip": "1" * 40, "merge_sha": "2" * 40},
        p2={"state": "queued", "tip": "3" * 40},
        p10={"state": "merged", "tip": "4" * 40, "merge_sha": "5" * 40},
    )
    assert lr.recorded_shas(rec) == ["b" * 40, "1" * 40, "2" * 40, "4" * 40, "5" * 40]


def test_split_moves_the_blocked_phase_and_its_dependents_and_refuses_when_a_merged_phase_depends_on_it() -> None:
    parents = lr.phase_parents(TWO_WAVES)
    states = {"p1": "merged", "p2": "merged", "p3": "blocked", "p10": "waiting"}
    assert lr.blocked_split(parents, states, "p3") == (["p3", "p10"], None)
    assert lr.blocked_split(parents, {**states, "p10": "merged"}, "p3") == ([], "merged phase(s) p10 depend on blocked phase p3")


def test_follow_up_ids_take_the_first_free_number() -> None:
    assert lr.follow_up_id("aim-slack", {"aim-slack"}) == "aim-slack-2"
    assert lr.follow_up_id("aim-slack", {"aim-slack", "aim-slack-2"}) == "aim-slack-3"
    assert lr.follow_up_id("aim-slack-2", {"aim-slack", "aim-slack-2"}) == "aim-slack-3"
