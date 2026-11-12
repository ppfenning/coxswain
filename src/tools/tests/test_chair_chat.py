from copy import deepcopy

import pytest

from agent_tools.chair_chat import (
    DIRECTIVE_KINDS,
    answer,
    apply_directives,
    build_message,
    empty_state,
    parse_message,
    state_summary,
    validate_directive,
)
from agent_tools.chair_steward import decide
from agent_tools.courier import Reference

REF = Reference("initiative", "e1")

VALID = {
    "priority": {"kind": "priority", "target_kind": "task", "target": "t1", "rank": "1"},
    "hold": {"kind": "hold", "target_kind": "initiative", "target": "e1"},
    "release": {"kind": "release", "target_kind": "initiative", "target": "e1"},
    "scope": {"kind": "scope", "initiatives": "e1, e2"},
    "feature": {"kind": "feature", "name": "auto-merge", "state": "on"},
    "cut_release": {"kind": "cut_release", "version": "2026.11.7"},
}

REFUSED = {
    "priority": {"kind": "priority", "target_kind": "task", "target": "t1", "rank": "0"},
    "hold": {"kind": "hold", "target_kind": "branch", "target": "e1"},
    "release": {"kind": "release", "target_kind": "task", "target": " "},
    "scope": {"kind": "scope", "initiatives": "all,e1"},
    "feature": {"kind": "feature", "name": "auto-merge", "state": "maybe"},
    "cut_release": {"kind": "cut_release", "version": "next"},
}


def directive(message_id, payload, sender="operator"):
    return build_message(sender, "directive", payload, message_id, REF)


def question(payload):
    return build_message("operator", "question", payload, "q1", REF)


def facts(*tasks, epic="e1", exited=True):
    return {
        "epic_id": epic,
        "branch_head": "h1",
        "levels": [{"tasks": [{"id": i, "state": s} for i, s in tasks], "merged": False, "phase": "p0"}],
        "exited": exited,
        "signature": "s1",
        "reported": frozenset(),
        "land_at": "ticket",
    }


def test_build_message_is_a_courier_entry_on_the_chair_label():
    entry = directive("m1", VALID["hold"])
    assert entry["topic"] == "chair_chat" and entry["event"] == "chat_directive"
    assert entry["to"] == "chair" and entry["from"] == "operator" and entry["fields"] == VALID["hold"]


def test_parse_message_round_trips_a_built_entry():
    assert parse_message(question({"kind": "what_failed"})) == {
        "id": "q1", "sender": "operator", "body_kind": "question", "payload": {"kind": "what_failed"},
    }


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "text",
        {},
        {**directive("m1", VALID["hold"]), "topic": "other"},
        {**directive("m1", VALID["hold"]), "event": "stuck"},
        {**directive("m1", VALID["hold"]), "event": ["chat_directive"]},
        {**directive("m1", VALID["hold"]), "from": ""},
        {**directive("m1", VALID["hold"]), "id": 7},
        {**directive("m1", VALID["hold"]), "fields": {"rank": 1}},
        {**directive("m1", VALID["hold"]), "fields": "hold"},
    ],
)
def test_a_malformed_entry_parses_to_none(entry):
    assert parse_message(entry) is None


@pytest.mark.parametrize("kind", DIRECTIVE_KINDS)
def test_each_directive_kind_validates(kind):
    assert validate_directive(kind, VALID[kind]) is None


@pytest.mark.parametrize("kind", DIRECTIVE_KINDS)
def test_each_directive_kind_refuses_a_bad_payload_with_a_reason(kind):
    assert isinstance(validate_directive(kind, REFUSED[kind]), str)


def test_validate_directive_never_raises_on_odd_input():
    assert validate_directive("nope", {}) == "unknown directive kind 'nope'"
    assert validate_directive(["hold"], {}) == "unknown directive kind ['hold']"
    assert validate_directive("hold", None) == "payload must be a mapping"
    assert validate_directive("priority", {"target_kind": "task", "target": "t", "rank": 1}) is not None


def test_a_priority_directive_applies_once():
    state, events = apply_directives(empty_state(), [directive("m1", VALID["priority"])], "t0")
    assert state["priorities"] == {"task:t1": "1"} and state["applied"] == ["m1"]
    assert [e["event"] for e in events] == ["directive_applied"]
    assert events[0]["fields"] == {**VALID["priority"], "id": "m1", "kind": "priority", "sender": "operator", "at": "t0"}
    assert events[0]["key"].startswith("directive_applied:")


def test_hold_then_release_clears_the_hold():
    held, _ = apply_directives(empty_state(), [directive("m1", VALID["hold"])], "t0")
    assert held["holds"] == ["initiative:e1"]
    freed, _ = apply_directives(held, [directive("m2", VALID["release"])], "t1")
    assert freed["holds"] == []


def test_a_scope_directive_replaces_all_with_the_listed_initiatives():
    state, _ = apply_directives(empty_state(), [directive("m1", VALID["scope"])], "t0")
    assert state["scope"] == ["e1", "e2"]
    back, _ = apply_directives(state, [directive("m2", {"kind": "scope", "initiatives": "all"})], "t1")
    assert back["scope"] == ["all"]


def test_a_feature_directive_records_on_or_off():
    state, _ = apply_directives(empty_state(), [directive("m1", VALID["feature"])], "t0")
    assert state["features"] == {"auto-merge": "on"}


def test_a_cut_release_directive_records_the_version():
    state, events = apply_directives(empty_state(), [directive("m1", VALID["cut_release"])], "t0")
    assert state["cut_release"] == "2026.11.7" and events[0]["event"] == "directive_applied"


def test_a_repeated_directive_id_applies_no_second_time():
    once, _ = apply_directives(empty_state(), [directive("m1", VALID["hold"])], "t0")
    twice, events = apply_directives(once, [directive("m1", VALID["hold"])], "t1")
    assert twice == once and events == []
    both, in_batch = apply_directives(empty_state(), [directive("m1", VALID["hold"]), directive("m1", VALID["hold"])], "t0")
    assert both["applied"] == ["m1"] and len(in_batch) == 1


def test_the_input_state_compares_equal_before_and_after():
    state, _ = apply_directives(empty_state(), [directive("m0", VALID["feature"])], "t0")
    before = deepcopy(state)
    apply_directives(state, [directive(f"m{n}", VALID[k]) for n, k in enumerate(DIRECTIVE_KINDS, 1)], "t1")
    assert state == before


def test_a_refused_directive_emits_a_reason_and_is_not_recorded():
    state, events = apply_directives(empty_state(), [directive("m1", REFUSED["cut_release"])], "t0")
    assert state == empty_state()
    assert events[0]["event"] == "directive_refused" and events[0]["fields"]["reason"] == "version must look like 1.2.3"


def test_questions_and_malformed_entries_apply_nothing():
    state, events = apply_directives(empty_state(), [question({"kind": "what_failed"}), {"id": "x"}, None], "t0")
    assert state == empty_state() and events == []


def test_left_on_release_lists_open_tasks_and_what_is_planned():
    f = [facts(("t1", "done"), ("t2", "ready"), ("t3", "quarantined"), exited=False)]
    text = answer({"kind": "left_on_release", "subject": "e1"}, f, decide(f))
    assert text == "e1: 2 left: t2 (ready), t3 (quarantined). Planned: publish(task=t3)."


def test_left_on_release_says_when_nothing_is_left():
    f = [facts(("t1", "done"), ("t2", "dropped"))]
    assert answer({"kind": "left_on_release", "subject": "e1"}, f, []) == (
        "e1: nothing is left; every task is approved, done or dropped. Nothing is planned."
    )


def test_why_not_launched_names_the_blocking_fact_for_a_task():
    f = [facts(("t1", "ready"), exited=False)]
    assert answer({"kind": "why_not_launched", "subject": "t1"}, f, []) == (
        "t1 is ready but not launched: the run for e1 has not exited."
    )
    planned = decide([facts(("t1", "ready"))])
    assert answer({"kind": "why_not_launched", "subject": "t1"}, [facts(("t1", "ready"))], planned) == (
        "t1 is ready and a relaunch is planned for e1."
    )


def test_why_not_launched_for_an_initiative_counts_states():
    f = [facts(("t1", "ready"), ("t2", "done"), exited=False)]
    assert answer({"kind": "why_not_launched", "subject": "e1"}, f, []) == (
        "e1 has 1 done, 1 ready; exited is false. Nothing is planned."
    )


def test_what_failed_lists_quarantines_and_planned_failure_events():
    f = [facts(("t1", "quarantined"), ("t2", "ready"))]
    assert answer({"kind": "what_failed"}, f, decide(f)) == (
        "Quarantined: t1 on e1. Failure events planned: publish(task=t1)."
    )
    assert answer({"kind": "what_failed"}, [facts(("t1", "done"))], []) == (
        "Nothing is quarantined. No failure events are planned."
    )


@pytest.mark.parametrize("kind", ["left_on_release", "why_not_launched"])
def test_an_unknown_subject_answers_that_the_facts_do_not_name_it(kind):
    assert answer({"kind": kind, "subject": "ghost"}, [facts(("t1", "ready"))], []) == "The facts do not name ghost."


def test_an_unknown_question_kind_is_named():
    assert answer({"kind": "why"}, [], []) == "Unknown question kind 'why'."


def test_state_summary_shows_directives_epics_and_recent_events():
    state, _ = apply_directives(
        empty_state(), [directive("m1", VALID["hold"]), directive("m2", VALID["feature"])], "t0"
    )
    f = [facts(("t1", "ready"), ("t2", "done"))]
    assert state_summary(state, f, [{"event": "directive_applied"}]) == "\n".join([
        "Directives applied: 2",
        "Scope: all",
        "Holds: initiative:e1",
        "Features: auto-merge=on",
        "Cut release: none",
        "Epics: e1 (1 left)",
        "Recent events: directive_applied",
    ])
