"""The tick sorts its own quarantines: one rebuild per harness state key; a review objection goes to the model's triage
once per state, then the task is held for a person. The annotation executor still writes one note per objection."""
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_sort
from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_remedy import remedy_for
from agent_tools.chair_types import Action, Facts

OBJECTION = "the parser drops a short row without a test"
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def _tick_facts() -> dict:
    return {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [], "initiatives": [], "quarantines": [], "intake": [], "work_store_ready": True,
        "sources_configured": True, "remote_unfetched": {}, "run_exited": {}, "lost_runs": {},
        "last_housekeeping_at": None, "housekeeping_hours": 24.0, "stale_days": 7, "stale_candidates": [],
        "stall_candidates": [], "home": {},
    }


def _quarantine(**over) -> dict:
    base = {"task_id": "t1", "initiative": "i", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False}
    return {**base, **over}


def _facts(quarantine: dict, handled: list[str] | None = None) -> Facts:
    facts = {"approved": [], "initiatives": [], "quarantines": [quarantine], "schema_deaths": {}, "landing": []}
    return {**facts, **({"handled_keys": handled} if handled is not None else {})}  # type: ignore[return-value]


def _review(**over) -> dict:
    return _quarantine(**{"cause": "review", "reason": OBJECTION, "held_objection": OBJECTION, "run": "i-4", "failures": 1, **over})


def _deps(tmp_path: Path, calls: list[str], note=None, steps: list | None = None) -> Deps:
    steps = [] if steps is None else steps
    return Deps(
        run=lambda argv: steps.append(("run", argv)) or (0, "ok"),
        delete_branches=lambda repo, pattern: steps.append(("clear", repo, pattern)) or ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None, run_id=lambda action: "", repo_for=lambda action: "r",
        work_dir=tmp_path, runs_dir=tmp_path, objection_note=note,
        mark_handled=calls.append,
    )


def _ticket(tmp_path: Path) -> Path:
    path = tmp_path / "work" / "i" / "p1" / "t1.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: t1\n---\n\nBody.\n", encoding="utf-8")
    return path


def test_harness_quarantine_plans_one_rebuild_and_none_once_handled() -> None:
    q = _quarantine(task_state_key="abc123")
    plan = plan_recover(_facts(q))
    assert [(a["kind"], a["key"]) for a in plan] == [("rebuild_quarantined", chair_sort.state_key("t1", "abc123"))]
    assert plan_recover(_facts(q, [chair_sort.state_key("t1", "abc123")])) == []


def test_quarantine_without_the_new_facts_keeps_its_recovery() -> None:
    assert [a["kind"] for a in plan_recover(_facts(_quarantine()))] == ["retry"]


def test_held_objection_plans_one_triage_request_and_no_annotation() -> None:
    plan = plan_recover(_facts(_review()))
    assert [(a["kind"], a["key"]) for a in plan] == [("request_triage", chair_sort.triage_key("t1", "i-4"))]


def _annotation() -> Action:
    return {"kind": "annotate_objection", "initiative": "i", "task_id": "t1", "objection": OBJECTION,
            "key": chair_sort.objection_key("t1", OBJECTION)}


def test_an_annotation_with_a_model_step_lands_its_note_once(tmp_path: Path) -> None:
    plan = [_annotation()]
    path, handled, asked = _ticket(tmp_path), [], []

    def note(objection: str) -> str:
        asked.append(objection)
        return "Add a test for the short row."

    actions: list[Action] = [{**plan[0], "epoch": 1}]
    [result] = perform(actions, _deps(tmp_path, handled, note), lambda: 1, False)
    assert result["status"] == "done"
    assert path.read_text(encoding="utf-8").count(chair_sort.NOTE_HEADING) == 1
    assert "Instruction: Add a test for the short row." in path.read_text(encoding="utf-8")
    assert (asked, handled) == ([OBJECTION], [plan[0]["key"]])


def test_second_quarantine_in_the_same_state_holds_the_task_and_makes_no_model_call() -> None:
    key = chair_sort.triage_key("t1", "i-4")
    plan = plan_recover(_facts(_review(), [key]))
    assert [(a["kind"], a["key"]) for a in plan] == [("hold_task", chair_sort.hold_key("t1", "i-4"))]


def test_a_failed_model_step_is_not_asked_again_on_the_next_tick(tmp_path: Path) -> None:
    path, handled, asked = _ticket(tmp_path), [], []

    def note(objection: str) -> str:
        asked.append(objection)
        raise RuntimeError("model down")

    first = _annotation()
    [result] = perform([{**first, "epoch": 1}], _deps(tmp_path, handled, note), lambda: 1, False)
    assert result["status"] == "refused" and "model down" in result["reason"]
    assert (asked, handled, chair_sort.NOTE_HEADING in path.read_text(encoding="utf-8")) == ([OBJECTION], [first["key"]], False)
    assert '"needs_attention"' in (tmp_path / "courier.jsonl").read_text(encoding="utf-8")
    # The next tick plans no annotation at all: the objection goes to the model's triage in the next run.
    second = plan_recover(_facts(_review(), handled))
    assert [a["kind"] for a in second] == ["request_triage"]
    assert asked == [OBJECTION]


def test_a_missing_model_step_marks_the_key_handled_too(tmp_path: Path) -> None:
    _ticket(tmp_path)
    handled: list[str] = []
    action: Action = {"kind": "annotate_objection", "initiative": "i", "task_id": "t1", "objection": OBJECTION, "key": "k", "epoch": 1}
    [result] = perform([action], _deps(tmp_path, handled), lambda: 1, False)
    assert (result["status"], handled) == ("refused", ["k"])


def test_a_rebuild_clears_branches_onto_main_then_relaunches_and_marks_its_key(tmp_path: Path) -> None:
    steps: list = []
    handled: list[str] = []
    [plan] = plan_recover(_facts(_quarantine(task_state_key="abc123")))
    [result] = perform([{**plan, "epoch": 1}], _deps(tmp_path, handled, steps=steps), lambda: 1, False)
    launch = ["cox", "route", "launch", "epic", "--initiative", "work/i", "--no-claim"]
    assert result["status"] == "done"
    assert ("clear", "r", "epic/i/*") in steps and steps.index(("clear", "r", "epic/i/*")) < len(steps) - 1
    assert steps[-1][0] == "run" and steps[-1][1][: len(launch)] == launch
    assert handled == [plan["key"]]


def test_a_rebuild_whose_clear_failed_launches_nothing_and_stays_unhandled(tmp_path: Path) -> None:
    steps: list = []
    handled: list[str] = []
    deps = replace(_deps(tmp_path, handled, steps=steps), repo_for=lambda action: "")
    [plan] = plan_recover(_facts(_quarantine(task_state_key="abc123")))
    [result] = perform([{**plan, "epoch": 1}], deps, lambda: 1, False)
    assert (result["status"], steps, handled) == ("refused", [], [])


def test_a_harness_quarantine_the_old_rules_rescue_stays_a_rescue() -> None:
    plan = plan_recover(_facts(_quarantine(task_state_key="abc123", has_patch=True)))
    assert [a["kind"] for a in plan] == ["rescue"]


def test_a_blank_objection_still_goes_to_triage_which_reads_the_whole_record() -> None:
    plan = plan_recover(_facts(_review(held_objection="  ", reason="  ")))
    assert [a["kind"] for a in plan] == ["request_triage"]


def test_a_review_objection_with_no_state_in_its_fact_reports_under_its_own_cause() -> None:
    plan = plan_recover(_facts(_quarantine(cause="review", reason=OBJECTION, held_objection=OBJECTION)))
    assert [(a["kind"], a["cause"]) for a in plan] == [("needs_chair", "review")]
    assert plan[0]["remedy"] == remedy_for("review", "", "t1", "")["kind"]


def test_a_held_review_quarantine_does_not_hold_back_a_sibling_retry() -> None:
    sibling = _quarantine(task_id="t2")
    facts = {**_facts(_review(), [chair_sort.triage_key("t1", "i-4")]), "quarantines": [_review(), sibling]}
    plan = plan_recover(facts)  # type: ignore[arg-type]
    assert [(a["kind"], a["task_id"]) for a in plan] == [("hold_task", "t1"), ("retry", "t2")]


def test_a_tick_plans_the_sorted_actions_through_plan_tick() -> None:
    facts = {**_tick_facts(), "quarantines": [_quarantine(task_state_key="abc123")]}
    kinds = [a["kind"] for a in plan_tick(facts, NOW) if a["kind"] == "rebuild_quarantined"]  # type: ignore[arg-type]
    assert kinds == ["rebuild_quarantined"]
    handled = {**facts, "handled_keys": [chair_sort.state_key("t1", "abc123")]}
    assert [a for a in plan_tick(handled, NOW) if a["kind"] == "rebuild_quarantined"] == []  # type: ignore[arg-type]
