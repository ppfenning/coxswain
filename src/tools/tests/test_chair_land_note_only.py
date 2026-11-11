"""A land_phase that exits 0 with only the ticket-granularity note is deferred, not a failed land."""

import pytest

from agent_tools import chair_exec, cli, land
from agent_tools.chair_exec import Deps, land_outcome

NOTE = "phase p1: ticket granularity, the one pull request opens at the last phase"
ACTION = {"kind": "land_phase", "initiative": "alpha", "phase": "p1", "repo": "/repo", "epoch": 1}


def _deps() -> Deps:
    return Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "run-1",
        repo_for=lambda action: "/repo",
    )


def _status(code: int, output: str) -> str:
    return land_outcome(ACTION, _deps(), code, output)["status"]


def test_the_note_alone_at_exit_zero_is_deferred() -> None:
    result = land_outcome(ACTION, _deps(), 0, NOTE + "\n")
    assert (result["status"], result["reason"]) == ("deferred", NOTE)


def test_the_note_at_a_non_zero_exit_is_not_deferred() -> None:
    assert _status(1, NOTE + "\n") != "deferred"


def test_the_note_with_step_lines_is_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chair_exec, "land_commit", lambda repo: "abc123")
    assert _status(0, f"{NOTE}\nmark_done: p1-a\nmerge: squashed\n") == "landed"


def test_the_note_with_a_stop_line_or_a_landed_via_line_is_not_deferred() -> None:
    assert _status(0, f"{NOTE}\nland: stopped at gate: held\n") != "deferred"
    assert _status(0, f"{NOTE}\nlanded via pull request #4\n") != "deferred"


def test_forge_line_alone_stays_not_landed() -> None:
    assert _status(0, "forge: github\n") == "not_landed"


def test_the_output_of_a_real_apply_walk_over_the_planned_note_is_deferred(tmp_path, capsys) -> None:
    record = {"run": "epic-x-5", "phase": "p1", "initiative": "x", "card_branch": "", "last_phase": False}
    task = {"task": "a", "title": "a", "repo": "wire", "status": "approved", "review": {"verdict": "approve"}}
    steps = land.land_plan(record, {}, "main", items=[{"id": "a", "status": "approved"}], task_records=[task],
                           repo="wire", ticket_tasks=("a", "z"), ticket_present=("z",), phases=())
    code, reached, _ = cli._land_walk(tmp_path, steps, steps, None, None, "full", False, None, [])
    printed = "forge: github\n" + capsys.readouterr().out
    assert (code, reached) == (0, [])
    assert _status(code, printed) == "deferred"
