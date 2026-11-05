from datetime import UTC, datetime

from agent_tools.chair_facts import epic_facts
from agent_tools.chair_report import format_status

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)  # 14:05 EDT
FACTS = {
    "limits": {
        "hard_stop": False,
        "weekly_fraction": 0.61,
        "hard_stop_fraction": 0.9,
        "five_hour_fraction": 0.42,
        "window_start_day": None,
        "window_source": "meter",
        "weekly_source": "meter",
    },
    "dispatch": {"max_in_flight": 4, "live_runs": 2},
    "last_housekeeping_at": None,
}
OLD_LINE = (
    "chair 09-26 14:05 EDT | lanes 2/4 | lands 0 | limits 5h 42% (meter) weekly 61%/90% (meter) | holding"
    " | needs chair: none | housekeeping never"
)


def _initiative(name: str, epic: str | None) -> dict:
    extra = {"epic": epic} if epic else {}
    return {"kind": "initiative", "initiative": name, "task_id": name, "phase": "", "state": "todo", "extra": extra}


def _task(initiative: str, state: str) -> dict:
    return {"kind": "task", "initiative": initiative, "task_id": "t", "phase": "p1", "state": state, "extra": {}}


def test_a_partial_epic_counts_only_its_landed_initiatives() -> None:
    rows = [_initiative("a", "e1"), _initiative("b", "e1"), _task("a", "done"), _task("b", "ready")]
    [epic] = epic_facts(rows, {})
    assert (epic["id"], epic["landed"], epic["total"], epic["complete"]) == ("e1", 1, 2, False)
    assert epic["initiatives"] == [{"id": "a", "landed": True}, {"id": "b", "landed": False}]


def test_a_complete_epic_is_marked_complete() -> None:
    rows = [_initiative("a", "e1"), _initiative("b", "e1"), _task("a", "done"), _task("b", "dropped")]
    assert [e["complete"] for e in epic_facts(rows, {})] == [True]


def test_an_initiative_with_no_epic_is_omitted() -> None:
    rows = [_initiative("a", "e1"), _initiative("solo", None), _task("a", "done"), _task("solo", "done")]
    [epic] = epic_facts(rows, {})
    assert [i["id"] for i in epic["initiatives"]] == ["a"]


def test_no_epic_gives_an_empty_list() -> None:
    assert epic_facts([_initiative("solo", None), _task("solo", "done")], {}) == []


def test_a_definition_supplies_title_and_target_branch() -> None:
    [epic] = epic_facts([_initiative("a", "e1")], {"e1": {"title": "T", "target_branch": "release"}})
    assert (epic["title"], epic["target_branch"]) == ("T", "release")


def test_status_line_without_epics_is_unchanged() -> None:
    assert format_status(FACTS, [], [], NOW) == OLD_LINE


def test_status_line_with_epics_adds_one_part() -> None:
    epics = [
        {"id": "e1", "landed": 1, "total": 2, "complete": False},
        {"id": "e2", "landed": 2, "total": 2, "complete": True},
    ]
    line = format_status({**FACTS, "epics": epics}, [], [], NOW)
    assert line == OLD_LINE.replace("| needs chair", "| epics: e1 1/2 landed, e2 2/2 landed complete | needs chair")
