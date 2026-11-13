import json
from dataclasses import replace

from agent_tools import cli
from agent_tools.chair_facts import gather_facts, wrongly_released_facts
from tests.test_chair_facts import NOW, _deps

NOTE = "Released from approval on 2026-11-09: approval unmerged for 6 ticks. Rebuild on the current main."
OLD_NOTE = "Released from approval on 2026-11-09: approval not merged to main or its phase branch for 6 ticks; rebuild on the current main. Rebuild on the current main."
READY = {"id": "t1", "initiative": "i", "phase": "p1", "state": "ready", "body": f"Body.\n\n{NOTE}\n"}
TICKET = {"i": "ticket"}


def _listed(item: dict, merged: bool = True, land_at: dict | None = None) -> list:
    return wrongly_released_facts([item], lambda i: merged, TICKET if land_at is None else land_at)


def test_a_ready_ticket_with_the_release_note_and_a_true_check_is_listed():
    assert _listed(READY) == [{"initiative": "i", "task_id": "t1"}]


def test_a_false_check_lists_nothing():
    assert _listed(READY, merged=False) == []


def test_no_note_lists_nothing():
    assert _listed({**READY, "body": "Body.\n"}) == []


def test_a_note_in_a_task_that_is_not_ready_lists_nothing():
    assert _listed({**READY, "state": "approved"}) == []


def test_task_landing_lists_nothing_and_never_asks_the_check():
    asked = []
    assert wrongly_released_facts([READY], lambda i: asked.append(i) or True, {"i": "task"}) == []
    assert asked == []


def test_level_landing_is_listed():
    assert _listed(READY, land_at={"i": "level"}) == [{"initiative": "i", "task_id": "t1"}]


def test_an_initiative_missing_from_land_at_reads_ticket():
    assert _listed(READY, land_at={}) == [{"initiative": "i", "task_id": "t1"}]


def test_the_older_release_wording_is_listed():
    assert _listed({**READY, "body": f"Body.\n\n{OLD_NOTE}\n"}) == [{"initiative": "i", "task_id": "t1"}]


def test_a_note_without_the_hold_cause_lists_nothing():
    other = "Released from approval on 2026-11-09: steer deadlock. Rebuild on the current main."
    assert _listed({**READY, "body": other}) == []


def test_gather_facts_carries_the_reader_under_wrongly_released_and_defaults_to_empty():
    assert gather_facts(_deps(), NOW)["wrongly_released"] == []
    listed = [{"initiative": "i", "task_id": "t1"}]
    assert gather_facts(replace(_deps(), wrongly_released=lambda: listed), NOW)["wrongly_released"] == listed


def _record(runs_dir, run: str, task: str) -> None:
    path = runs_dir / run / "tasks" / "p1" / f"{task}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"branch": f"agents/{run}/{task}"}), encoding="utf-8")


def _binding(runs_dir, live: set[str], asked: list):
    return cli._wrongly_released_merged(lambda row: asked.append(row) or True, runs_dir, lambda: live, TICKET)


def test_the_binding_asks_the_approval_check_with_the_newest_run_of_the_task(tmp_path):
    _record(tmp_path, "i-2", "t1")
    _record(tmp_path, "i-10", "t1")
    _record(tmp_path, "other-11", "t1")
    asked: list = []
    assert _binding(tmp_path, set(), asked)(READY) is True
    assert [(r["run"], r["land_at"]) for r in asked] == [("i-10", "ticket")]


def test_the_binding_skips_a_task_with_a_live_run_and_one_with_no_record(tmp_path):
    _record(tmp_path, "i-1", "t1")
    asked: list = []
    assert _binding(tmp_path, {"i-1"}, asked)(READY) is False
    assert _binding(tmp_path, set(), asked)({**READY, "id": "t2"}) is False
    assert asked == []
