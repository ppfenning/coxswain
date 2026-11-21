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


def test_the_row_result_names_the_tip_the_commands_the_host_and_each_check():
    smoke = lane_smoke.outcome([PASS, FAIL])
    result = lane_smoke.result_json("abc", ["pytest -q smoke", "./wired.sh"], "host-a", smoke, [PASS, FAIL])
    assert result == {
        "tip": "abc", "cmds": ["pytest -q smoke", "./wired.sh"], "host": "host-a", "reason": smoke.reason,
        "harness_fault": False, "reusable": False,
        "checks": [{"name": "smoke", "passed": True, "outcome": "passed", "exit_code": 0},
                   {"name": "wiring", "passed": False, "outcome": "failed", "exit_code": 1}],
    }
    reused = lane_smoke.result_json("abc", [], "host-a", lane_smoke.Smoke(True, "reused"), [], reused_from="ship.p1-1")
    assert reused["reused_from"] == "ship.p1-1" and reused["checks"] == [] and reused["reusable"] is False


def _row(run_id: str, ok: int, sha: str, cmds: list[str], reusable: bool = True,
         host: str = "host-a") -> dict[str, object]:
    return {"run_id": run_id, "ok": ok, "subject_sha": sha,
            "result_json": {"tip": sha, "cmds": cmds, "host": host, "reusable": reusable}}


def test_only_a_real_pass_on_the_same_host_at_the_same_tip_with_the_same_commands_is_reused():
    cmds = ["pytest -q smoke"]
    rows = [_row("r1", 0, "abc", cmds), _row("r2", 1, "old", cmds), _row("r3", 1, "abc", ["other"]),
            _row("r4", 1, "abc", cmds, reusable=False), _row("r5", 1, "abc", cmds, host="host-b"),
            _row("r6", 1, "abc", cmds), _row("r7", 1, "abc", cmds)]
    assert lane_smoke.reusable(rows, "abc", cmds, "host-a") == rows[5]  # the first that qualifies
    assert lane_smoke.reusable(rows, "abc", cmds, "host-b") == rows[4]
    # A failure, another tip, other commands, a pass of skips, another host (or a row reused itself): none stands.
    assert lane_smoke.reusable(rows[:5], "abc", cmds, "host-a") is None
    assert lane_smoke.reusable([], "abc", cmds, "host-a") is None
    assert lane_smoke.reusable(rows, "abc", cmds, "") is None  # a lane that names no host reuses nothing
    # A row written before `reusable` or `host` was recorded is not trusted.
    unhosted = {"run_id": "r8", "ok": 1, "subject_sha": "abc", "result_json": {"tip": "abc", "cmds": cmds, "reusable": True}}
    assert lane_smoke.reusable([unhosted], "abc", cmds, "host-a") is None
    assert lane_smoke.reusable([{"run_id": "r9", "ok": 1, "subject_sha": "abc", "result_json": "{}"}], "abc", [], "host-a") is None


def test_a_pass_of_9_with_126_skipped_on_a_host_without_docker_is_never_reused_on_a_host_with_it():
    """The reviewer's case: with docker hidden, the smoke suite passed 9 and skipped 126. It is a real pass, so a
    lane on that host may reuse it, but a lane of the same phase, tip and commands on another host runs the suite."""
    cmds = ['pytest -q -rs smoke -m "not forge"']
    hidden = {"name": cmds[0], "cmd": cmds[0], "passed": True, "outcome": "passed", "exit_code": 0,
              "counts": {"passed": 9, "skipped": 126}}
    smoke = lane_smoke.outcome([hidden])
    assert (smoke.ok, smoke.reusable) == (True, True)  # legitimate skips beside real passes keep reuse
    row = {"run_id": "ship.p1-1", "ok": 1, "subject_sha": "abc",
           "result_json": lane_smoke.result_json("abc", cmds, "no-docker", smoke, [hidden])}
    assert lane_smoke.reusable([row], "abc", cmds, "no-docker") == row
    assert lane_smoke.reusable([row], "abc", cmds, "with-docker") is None
    # The skip-only rule still holds on its own host.
    only = {**hidden, "counts": {"skipped": 135}}
    skipped = {"run_id": "ship.p1-2", "ok": 1, "subject_sha": "abc",
               "result_json": lane_smoke.result_json("abc", cmds, "no-docker", lane_smoke.outcome([only]), [only])}
    assert lane_smoke.reusable([skipped], "abc", cmds, "no-docker") is None
