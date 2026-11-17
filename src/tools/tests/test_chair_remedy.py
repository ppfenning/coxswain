import pytest

from agent_tools.chair_remedy import (
    choose_remedy,
    initiative_of,
    remedy_command,
    remedy_for,
    set_state_argv,
    trim_reason,
)


def test_trim_reason_cuts_to_600():
    assert len(trim_reason("x" * 700)) == 600


def test_trim_reason_none_is_empty():
    assert trim_reason(None) == ""


def test_trim_reason_strips():
    assert trim_reason("  why  \n") == "why"


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        ("ticket", "re-ground"),
        ("code", "re-ground"),
        ("review", "re-ground"),
        ("stranded", "carry"),
        ("harness", "retry"),
        ("stalled", "retry"),
        ("stale", "drop"),
        ("superseded", "drop"),
        ("mystery", "re-ground"),
        (None, "re-ground"),
    ],
)
def test_choose_remedy(cause, kind):
    assert choose_remedy(cause) == kind


def test_remedy_command_unknown_kind_is_reground():
    assert remedy_command("bogus", "r1", "t1", "/repo") == remedy_command("re-ground", "r1", "t1", "/repo")


def test_remedy_for_carry():
    assert remedy_for("stranded", "run-1", "task-a", "/repo") == {
        "kind": "carry",
        "command": ["cox", "runs", "land", "run-1", "--repo", "/repo", "--task", "task-a", "--apply"],
    }


def test_remedy_for_reground():
    assert remedy_for("ticket", "run-1", "task-a", "/repo") == {
        "kind": "re-ground",
        "command": ["cox", "route", "edit", "task-a", "--state", "ready"],
    }


def test_a_set_state_remedy_names_the_initiative_so_a_repeated_task_id_moves_the_right_ticket():
    assert remedy_for("stale", "init-3", "t1", "/repo", "init")["command"] == [
        "cox", "route", "edit", "t1", "--initiative", "init", "--state", "dropped",
    ]
    assert remedy_for("harness", "init-3", "t1", "/repo", "init")["command"][-4:] == ["--initiative", "init", "--state", "ready"]


def test_set_state_argv_without_an_initiative_is_the_id_only_argv():
    assert set_state_argv("", "t1", "ready") == ["cox", "route", "edit", "t1", "--state", "ready"]


def test_initiative_of_prefers_the_row_then_reads_the_run_series():
    assert initiative_of("init", "other-3") == "init"
    assert initiative_of("", "init-3") == "init"
    assert initiative_of("", "init.p1-2") == "init"
    assert initiative_of("", "no-number-here") == ""
    assert initiative_of("", "") == ""
