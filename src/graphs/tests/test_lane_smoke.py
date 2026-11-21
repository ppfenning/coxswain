"""L2: what one run of a phase's level gate means (`lane_smoke`), pure, no mocks."""

from harness import lane_smoke

PASS = {"name": "smoke", "cmd": "pytest -q smoke", "passed": True, "outcome": "passed", "exit_code": 0}
FAIL = {"name": "wiring", "cmd": "./wired.sh", "passed": False, "outcome": "failed", "exit_code": 1,
        "output_tail": "notes/t3.txt names no wiring\n"}
UNRUNNABLE = {"name": "smoke", "cmd": "nope", "passed": False, "outcome": "unrunnable", "exit_code": None,
              "error": "No such file or directory: 'nope'"}


def test_no_level_checks_is_a_smoke_that_passed_as_none():
    assert lane_smoke.outcome([]) == lane_smoke.Smoke(True, "none")


def test_every_check_passing_is_passed_and_the_first_failure_is_the_reason():
    assert lane_smoke.outcome([PASS]) == lane_smoke.Smoke(True, "passed")
    failed = lane_smoke.outcome([PASS, FAIL])
    assert (failed.ok, failed.verdict) == (False, "failed")
    assert failed.reason.startswith("the phase smoke failed: configured check failed: wiring")
    assert "notes/t3.txt names no wiring" in failed.reason


def test_a_check_that_could_not_run_fails_the_smoke_and_says_it_is_a_harness_fault():
    smoke = lane_smoke.outcome([UNRUNNABLE])
    assert not smoke.ok and "harness fault: check 'smoke' could not run" in smoke.reason


def test_the_row_result_names_the_tip_the_commands_and_each_check():
    smoke = lane_smoke.outcome([PASS, FAIL])
    result = lane_smoke.result_json("abc", ["pytest -q smoke", "./wired.sh"], smoke, [PASS, FAIL])
    assert result == {
        "tip": "abc", "cmds": ["pytest -q smoke", "./wired.sh"], "reason": smoke.reason,
        "checks": [{"name": "smoke", "passed": True, "outcome": "passed", "exit_code": 0},
                   {"name": "wiring", "passed": False, "outcome": "failed", "exit_code": 1}],
    }
    reused = lane_smoke.result_json("abc", [], lane_smoke.Smoke(True, "reused"), [], reused_from="ship.p1-1")
    assert reused["reused_from"] == "ship.p1-1" and reused["checks"] == []


def _row(run_id: str, ok: int, sha: str, cmds: list[str]) -> dict[str, object]:
    return {"run_id": run_id, "ok": ok, "subject_sha": sha, "result_json": {"tip": sha, "cmds": cmds}}


def test_only_a_pass_at_the_same_tip_with_the_same_commands_is_reused():
    cmds = ["pytest -q smoke"]
    rows = [_row("r1", 0, "abc", cmds), _row("r2", 1, "old", cmds), _row("r3", 1, "abc", ["other"]),
            _row("r4", 1, "abc", cmds), _row("r5", 1, "abc", cmds)]
    assert lane_smoke.reusable(rows, "abc", cmds) == rows[3]  # the first that qualifies
    assert lane_smoke.reusable(rows[:3], "abc", cmds) is None  # a failure, another tip, other commands
    assert lane_smoke.reusable([], "abc", cmds) is None
    assert lane_smoke.reusable([_row("r6", 1, "abc", [])], "abc", []) is not None
    assert lane_smoke.reusable([{"run_id": "r7", "ok": 1, "subject_sha": "abc", "result_json": "{}"}], "abc", []) is None
