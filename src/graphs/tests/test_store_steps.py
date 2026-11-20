import json

import pytest

from harness.store_cli import main
from harness.store_migrate import open_store
from harness.store_steps import (
    belongs,
    put_step,
    refix_of,
    step_key,
    step_row,
    steps_for,
    steps_of_result,
    with_checks,
)

AT = "2026-11-20T00:00:00Z"

# A lifecycle result as the epic driver saves it after its checks ran: the shape `lifecycle_propose.run` returns,
# with `change_facts.checks` added by `epic_phase`.
RESULT = {
    "run_id": "ship-1:p1:t1",
    "ticket": "t1",
    "plan": {"steps": ["write the note", "test it"], "files_expected": ["NOTE.md"], "out_of_scope": []},
    "plan_attack": {"verdict": "proceed", "objections": []},
    "build": {"patch": "--- a\n+++ b\n", "summary": "s", "files_touched": ["NOTE.md"], "commands_run": []},
    "review": {"verdict": "approve", "findings": [], "rationale": "fine"},
    "adversary": {"verdict": "approve", "objections": ["a", "b"], "strongest_objection": "a"},
    "arbitration": None,
    "change_facts": {
        "patch_ok": True,
        "changed_lines": 3,
        "checks_run": "ship-1",
        "checks": [
            {"name": "note", "cmd": "test -f NOTE.md", "passed": True, "exit_code": 0, "outcome": "passed",
             "output_tail": "a long log that never reaches the row"},
        ],
    },
    "fix_loop": {"attempts": 2, "stopped": None, "continuations": 0, "rounds": []},
}


def rows_of(result, task="t1"):
    return steps_of_result(result, run_id="ship-1", initiative="ship", phase="p1", task=task, at=AT, claim_epoch=7)


# --- step_key -------------------------------------------------------------------------------------------------------


def test_a_task_step_is_keyed_by_task_and_kind_and_a_phase_step_by_kind_and_round():
    assert step_key("task", "t1", "build", 1) == "task/t1/build"
    assert step_key("task", "p1-rev-2", "build", 2) == "task/p1-rev-2/build"
    assert step_key("phase", "p1", "smoke", 3) == "phase/smoke/3"


@pytest.mark.parametrize(
    ("scope", "ident", "kind", "round_"),
    [("task", "t1", "smoke", 0), ("phase", "p1", "plan", 0), ("task", "", "build", 0), ("task", "a/b", "build", 0),
     ("task", "t1", "build", -1)],
)
def test_a_key_that_could_name_two_steps_is_refused(scope, ident, kind, round_):
    with pytest.raises(ValueError):
        step_key(scope, ident, kind, round_)


def test_a_refix_run_id_names_its_re_entry_and_any_other_names_none():
    assert refix_of("ship-1:p1:refix2:t1") == 2
    assert refix_of("ship-1:p1:refix1") == 1
    assert refix_of("ship-1:p1:t1") == 0
    assert refix_of("ship-1:p1:refixer:t1") == 0
    assert refix_of("") == 0


def test_a_step_row_fills_every_column_and_a_blank_start_reads_the_finish():
    row = step_row(run_id="r", step="task/t/plan", initiative="i", phase="p", task_id="t", kind="plan", round_=0,
                   ok=True, verdict="", result={"steps": 1}, finished_at=AT)
    assert row == {
        "run_id": "r", "step": "task/t/plan", "initiative": "i", "phase": "p", "task_id": "t", "kind": "plan",
        "round": 0, "ok": 1, "verdict": "", "subject_sha": "", "worker": "", "claim_epoch": 0, "started_at": AT,
        "finished_at": AT, "cost_usd": 0.0, "result_json": {"steps": 1}, "artifacts_json": [],
    }


# --- steps_of_result -------------------------------------------------------------------------------------------------


def test_a_full_result_gives_plan_build_review_adversary_and_checks_rows_in_order():
    rows = rows_of(RESULT)
    assert [(r["step"], r["round"], r["ok"], r["verdict"]) for r in rows] == [
        ("task/t1/plan", 0, 1, "proceed"),
        ("task/t1/build", 2, 1, ""),
        ("task/t1/review", 2, 1, "approve"),
        ("task/t1/adversary", 2, 1, "approve"),
        ("task/t1/checks", 2, 1, ""),
    ]
    assert {(r["run_id"], r["initiative"], r["phase"], r["task_id"], r["claim_epoch"], r["finished_at"]) for r in rows} == {
        ("ship-1", "ship", "p1", "t1", 7, AT)
    }
    by_kind = {r["kind"]: r["result_json"] for r in rows}
    assert by_kind["plan"] == {"steps": 2, "files_expected": ["NOTE.md"]}
    assert by_kind["build"] == {"patch_ok": True, "changed_lines": 3, "files_touched": ["NOTE.md"], "quarantine": ""}
    assert by_kind["adversary"] == {"verdict": "approve", "objections": 2}
    assert by_kind["checks"] == {"checks": [{"name": "note", "passed": True, "exit_code": 0, "outcome": "passed"}]}


def test_no_row_carries_a_patch_or_a_log():
    text = json.dumps([r["result_json"] for r in rows_of(RESULT)])
    assert "+++" not in text and "long log" not in text


def test_a_result_saved_before_its_checks_ran_has_no_checks_row():
    before = {**RESULT, "change_facts": {"patch_ok": True, "changed_lines": 3}}
    assert [r["kind"] for r in rows_of(before)] == ["plan", "build", "review", "adversary"]


def test_the_lifecycle_entry_points_checks_at_the_top_are_read_too():
    single = {**RESULT, "change_facts": {}, "checks": [{"name": "note", "passed": False, "exit_code": 1}]}
    checks = rows_of(single)[-1]
    assert (checks["kind"], checks["ok"]) == ("checks", 0)


def test_an_arbitration_is_its_own_row_and_a_revise_is_not_ok():
    judged = {**RESULT, "review": {"verdict": "revise", "findings": ["x"]},
              "arbitration": {"verdict": "approve", "sided_with": "adversary", "reasoning": "r"}}
    rows = {r["kind"]: r for r in rows_of(judged)}
    assert (rows["review"]["ok"], rows["review"]["result_json"]) == (0, {"verdict": "revise", "findings": 1})
    assert (rows["arbitration"]["ok"], rows["arbitration"]["result_json"]) == (
        1, {"verdict": "approve", "sided_with": "adversary"})


def test_a_failed_build_node_and_an_empty_patch_are_not_ok():
    own = {"run_id": "ship-1:p1:t1"}
    assert rows_of({**own, "build": {"patch": "x"}, "failed_node": "build"})[0]["ok"] == 0
    assert rows_of({**own, "build": {"patch": "  "}})[0]["ok"] == 0
    assert rows_of({**own, "build": {"patch": "x", "quarantine": "outside surfaces"}})[0]["ok"] == 0


def test_a_record_with_no_steps_gives_no_rows():
    assert rows_of({"ticket": "t1", "cause": "flake", "review": None, "plan": {}}) == ()


def test_a_refix_re_entry_is_keyed_as_a_task_of_its_own_and_keeps_the_task_id():
    rows = rows_of({**RESULT, "run_id": "ship-1:p1:refix1:t1"})
    assert {r["step"].split("/")[1] for r in rows} == {"t1-refix-1"}
    assert {r["task_id"] for r in rows} == {"t1"}


# --- put_step and steps_for, on both engines ------------------------------------------------------------------------


def test_each_step_is_written_once_and_read_back_as_written(store_conn):
    rows = rows_of(RESULT)
    assert [put_step(store_conn, r) for r in rows] == [1] * len(rows)
    assert [put_step(store_conn, r) for r in rows] == [0] * len(rows)
    changed = {**rows[0], "ok": 0, "verdict": "revise"}
    assert put_step(store_conn, changed) == 0
    assert steps_for(store_conn, "ship-1") == sorted(rows, key=lambda r: r["step"])
    assert steps_for(store_conn, "other-run") == []


# --- the CLI ---------------------------------------------------------------------------------------------------------


def test_steps_list_prints_the_runs_rows_as_one_json_array(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'cox.db'}"
    conn = open_store(url, AT)
    try:
        for r in rows_of(RESULT):
            put_step(conn, r)
    finally:
        conn.close()
    assert main(["--store-url", url, "steps", "list", "ship-1"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["kind"] for r in listed] == ["adversary", "build", "checks", "plan", "review"]
    assert main(["--store-url", url, "steps", "list", "no-such-run"]) == 0
    assert json.loads(capsys.readouterr().out) == []


# --- only the steps this run performed -------------------------------------------------------------------------------


def test_a_run_owns_its_own_id_and_its_graphs_and_nothing_else():
    assert belongs("ship-1", "ship-1") and belongs("ship-1:p1:t1", "ship-1")
    assert not belongs("ship-10:p1:t1", "ship-1") and not belongs("", "ship-1") and not belongs("ship-1", "")


def test_a_result_another_run_produced_gives_this_run_no_row():
    assert steps_of_result(RESULT, run_id="ship-2", initiative="ship", phase="p1", task="t1", at=AT) == ()


def test_checks_this_run_ran_on_another_runs_result_are_its_only_row():
    reused = with_checks(RESULT, [{"name": "note", "passed": True, "exit_code": 0}], "ship-2")
    rows = steps_of_result(reused, run_id="ship-2", initiative="ship", phase="p1", task="t1", at=AT)
    assert [(r["step"], r["ok"]) for r in rows] == [("task/t1/checks", 1)]


def test_checks_with_no_stamp_are_no_runs_and_a_reused_build_is_no_build():
    unstamped = {**RESULT, "change_facts": {"checks": [{"name": "note", "passed": True}]}}
    assert "checks" not in [r["kind"] for r in rows_of(unstamped)]
    kept = {**RESULT, "build": {**RESULT["build"], "reused": True}}
    assert "build" not in [r["kind"] for r in rows_of(kept)]


def test_with_checks_stamps_the_run_and_keeps_the_other_facts():
    stamped = with_checks({"change_facts": {"patch_ok": True}}, [{"name": "n", "passed": True}], "r9")
    assert stamped == {"change_facts": {"patch_ok": True, "checks": [{"name": "n", "passed": True}], "checks_run": "r9"}}
