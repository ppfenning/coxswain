"""L2: what one run of a phase's level gate means (`lane_smoke`), pure, no mocks."""

from harness import lane_smoke

PASS = {"name": "smoke", "cmd": "pytest -q smoke", "passed": True, "outcome": "passed", "exit_code": 0,
        "counts": {"passed": 12, "skipped": 2}}
SKIPPED = {"name": "smoke", "cmd": "pytest -q smoke", "passed": True, "outcome": "passed", "exit_code": 0,
           "counts": {"skipped": 40}}  # a smoke suite on a machine without docker: every test skipped, exit 0
NO_TESTS = {"name": "unit", "cmd": "pytest -q", "passed": True, "outcome": "skipped", "exit_code": 5, "counts": {}}
SCRIPT = {"name": "wired", "cmd": "./wired.sh", "passed": True, "outcome": "passed", "exit_code": 0, "counts": {}}
FAIL = {"name": "wiring", "cmd": "./wired.sh", "passed": False, "outcome": "failed", "exit_code": 1,
        "output_tail": "notes/t3.txt names no wiring\n"}
UNRUNNABLE = {"name": "smoke", "cmd": "nope", "passed": False, "outcome": "unrunnable", "exit_code": None,
              "error": "No such file or directory: 'nope'"}


def test_no_level_checks_is_a_smoke_that_passed_as_none():
    assert lane_smoke.outcome([]) == lane_smoke.Smoke(True, "none")


def test_every_check_passing_is_passed_and_the_first_failure_is_the_reason():
    assert lane_smoke.outcome([PASS, SCRIPT]) == lane_smoke.Smoke(True, "passed", reusable=True)
    failed = lane_smoke.outcome([PASS, FAIL])
    assert (failed.ok, failed.verdict, failed.harness_fault, failed.reusable) == (False, "failed", False, False)
    assert failed.reason.startswith("the phase smoke failed: configured check failed: wiring")
    assert "notes/t3.txt names no wiring" in failed.reason


def test_a_check_that_could_not_run_is_a_harness_fault():
    smoke = lane_smoke.outcome([UNRUNNABLE])
    assert not smoke.ok and smoke.harness_fault
    assert "harness fault: check 'smoke' could not run" in smoke.reason


def test_a_pass_that_only_skipped_passes_but_is_never_evidence_to_reuse():
    assert lane_smoke.skips_only(SKIPPED) and lane_smoke.skips_only(NO_TESTS)
    assert not lane_smoke.skips_only(PASS) and not lane_smoke.skips_only(SCRIPT)
    assert lane_smoke.outcome([PASS, SKIPPED]) == lane_smoke.Smoke(True, "passed", reusable=False)
    assert lane_smoke.outcome([NO_TESTS]) == lane_smoke.Smoke(True, "passed", reusable=False)


def test_the_row_result_names_the_tip_the_commands_and_each_check():
    smoke = lane_smoke.outcome([PASS, FAIL])
    result = lane_smoke.result_json("abc", ["pytest -q smoke", "./wired.sh"], smoke, [PASS, FAIL])
    assert result == {
        "tip": "abc", "cmds": ["pytest -q smoke", "./wired.sh"], "reason": smoke.reason, "harness_fault": False,
        "reusable": False,
        "checks": [{"name": "smoke", "passed": True, "outcome": "passed", "exit_code": 0},
                   {"name": "wiring", "passed": False, "outcome": "failed", "exit_code": 1}],
    }
    reused = lane_smoke.result_json("abc", [], lane_smoke.Smoke(True, "reused"), [], reused_from="ship.p1-1")
    assert reused["reused_from"] == "ship.p1-1" and reused["checks"] == [] and reused["reusable"] is False


def _row(run_id: str, ok: int, sha: str, cmds: list[str], reusable: bool = True) -> dict[str, object]:
    return {"run_id": run_id, "ok": ok, "subject_sha": sha,
            "result_json": {"tip": sha, "cmds": cmds, "reusable": reusable}}


def test_only_a_real_pass_at_the_same_tip_with_the_same_commands_is_reused():
    cmds = ["pytest -q smoke"]
    rows = [_row("r1", 0, "abc", cmds), _row("r2", 1, "old", cmds), _row("r3", 1, "abc", ["other"]),
            _row("r4", 1, "abc", cmds, reusable=False), _row("r5", 1, "abc", cmds), _row("r6", 1, "abc", cmds)]
    assert lane_smoke.reusable(rows, "abc", cmds) == rows[4]  # the first that qualifies
    # A failure, another tip, other commands, a pass of skips (or a row reused itself): none stands.
    assert lane_smoke.reusable(rows[:4], "abc", cmds) is None
    assert lane_smoke.reusable([], "abc", cmds) is None
    # A row written before `reusable` was recorded is not trusted.
    assert lane_smoke.reusable([{"run_id": "r7", "ok": 1, "subject_sha": "abc",
                                 "result_json": {"tip": "abc", "cmds": cmds}}], "abc", cmds) is None
    assert lane_smoke.reusable([{"run_id": "r8", "ok": 1, "subject_sha": "abc", "result_json": "{}"}], "abc", []) is None
