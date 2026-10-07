import json

from devtools.cli import _await_checks, _runs_verdict

NO_CHECKS = (1, "no checks reported on the 'release/1.0' branch")
URL = "https://github.com/o/r/actions/runs/1"


class Clock:
    """A fake clock that only `sleep` advances; `sleeps` records every wait."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def _listing(runs):
    calls = []

    def list_runs():
        calls.append(1)
        return 0, json.dumps(runs)

    return list_runs, calls


def _wait(poll, list_runs):
    clock = Clock()
    result = _await_checks(poll, timeout_s=180.0, sleep=clock.sleep, now=clock.now, list_runs=list_runs, branch="release/1.0")
    return result, clock


def test_startup_failure_fails_on_first_poll_with_url_and_no_sleep():
    list_runs, _ = _listing([{"conclusion": "startup_failure", "name": "CI", "url": URL}])
    (ok, message), clock = _wait(lambda: NO_CHECKS, list_runs)
    assert not ok
    assert URL in message and "startup_failure" in message
    assert clock.sleeps == []


def test_startup_failure_after_a_retry_still_stops_before_timeout():
    answers = iter([[], [{"conclusion": "startup_failure", "name": "CI", "url": URL}]])

    def list_runs():
        return 0, json.dumps(next(answers))

    (ok, message), clock = _wait(lambda: NO_CHECKS, list_runs)
    assert not ok and URL in message
    assert clock.sleeps == [15]


def test_empty_run_list_at_timeout_says_no_workflow_ran():
    list_runs, _ = _listing([])
    (ok, message), _clock = _wait(lambda: NO_CHECKS, list_runs)
    assert not ok
    assert message.startswith("no checks reported within 180s")
    assert "no workflow ran for branch release/1.0" in message


def test_runs_without_startup_failure_at_timeout_says_runs_exist():
    list_runs, _ = _listing([{"conclusion": "success", "name": "CI", "url": URL}])
    (ok, message), clock = _wait(lambda: NO_CHECKS, list_runs)
    assert not ok
    assert "no checks reported within 180s, though runs exist" in message
    assert "no startup_failure among the runs for branch release/1.0" in message
    assert sum(clock.sleeps) >= 180


def test_green_and_failed_results_pass_through_without_listing_runs():
    list_runs, calls = _listing([])
    assert _wait(lambda: (0, "all good"), list_runs)[0] == (True, "green")
    assert _wait(lambda: (1, " build failed \n"), list_runs)[0] == (False, "build failed")
    assert calls == []


def test_unusable_listing_keeps_the_bare_timeout_message():
    for listing in (lambda: (1, "gh: boom"), lambda: (0, "not json")):
        (ok, message), _clock = _wait(lambda: NO_CHECKS, listing)
        assert (ok, message) == (False, "no checks reported within 180s")


def test_runs_verdict_none_on_bad_json_and_no_workflow_on_empty_list():
    assert _runs_verdict(lambda: (0, "{")) is None
    assert _runs_verdict(lambda: (0, "[]")).kind == "no_workflow"


def test_without_list_runs_behaviour_is_unchanged():
    clock = Clock()
    result = _await_checks(lambda: NO_CHECKS, sleep=clock.sleep, now=clock.now)
    assert result == (False, "no checks reported within 180s")
