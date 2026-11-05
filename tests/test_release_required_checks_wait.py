import json

from devtools import cli, release

BRANCH = "release/2026.11.0"
PROTECTED = (0, json.dumps({"checks": [{"context": "build"}]}))
UNPROTECTED = (1, "gh: Branch not protected (HTTP 404)")


def _reported(*rows):
    return 0, json.dumps([{"name": n, "bucket": b, "state": b} for n, b in rows])


class Clock:
    """A fake clock that only `sleep` advances; `sleeps` records every wait."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def _stub(protection, polls):
    """A runner serving the protection answer, then each checks answer in order (the last repeats)."""
    calls, queue = [], list(polls)

    def run(argv, cwd):
        calls.append(argv)
        if argv[:2] == ["gh", "api"]:
            return protection
        if argv[:3] == ["gh", "pr", "checks"] and "--json" in argv:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if argv[:3] == ["gh", "pr", "checks"]:
            return 0, "build\tpass\n"
        if argv[:4] == ["git", "-C", "/d", "ls-remote"]:
            return 0, f"{'a' * 40}\trefs/heads/{BRANCH}\n"
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, "[{\"number\": 1}]"
        return 0, ""

    return calls, run


def _merge_back(protection, polls, clock):
    calls, run = _stub(protection, polls)
    step = {"kind": "merge_back", "component": "dash", "branch": BRANCH}
    ok, detail = cli._merge_back(step, "/d", run, clock.sleep, clock.now)
    return ok, detail, calls, clock.sleeps


def _merged(calls):
    return any(argv[:3] == ["gh", "pr", "merge"] for argv in calls)


def test_required_check_absent_then_green_waits_once_and_merges():
    ok, detail, calls, sleeps = _merge_back(PROTECTED, [_reported(("lint", "pass")), _reported(("build", "pass"))], Clock())
    assert ok and sleeps == [15] and _merged(calls)


def test_required_check_that_never_appears_fails_with_name_and_timeout():
    clock = Clock()
    ok, detail, calls, _ = _merge_back(PROTECTED, [_reported(("lint", "pass"))], clock)
    assert not ok and not _merged(calls)
    assert detail.startswith(f"{BRANCH}: ") and "build" in detail and "180s" in detail


def test_unprotected_branch_merges_without_polling_checks():
    ok, _, calls, sleeps = _merge_back(UNPROTECTED, [_reported()], Clock())
    assert ok and sleeps == [] and _merged(calls)
    assert release.pr_checks_json_argv(BRANCH) not in calls


def test_required_check_pending_then_green_waits_and_merges():
    ok, _, calls, sleeps = _merge_back(PROTECTED, [_reported(("build", "pending")), _reported(("build", "pass"))], Clock())
    assert ok and sleeps == [15] and _merged(calls)


def test_failed_required_check_does_not_merge():
    ok, detail, calls, sleeps = _merge_back(PROTECTED, [_reported(("build", "fail"))], Clock())
    assert not ok and "build" in detail and sleeps == [] and not _merged(calls)


def test_unreadable_protection_prints_one_line_and_returns_green(capsys):
    calls, run = _stub((1, "gh: HTTP 500"), [_reported()])
    clock = Clock()
    assert cli._await_required("main", None, "/d", run, clock.sleep, clock.now) == (True, "green")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    assert calls == [release.required_checks_argv("main")]


def test_missing_prints_one_waiting_line_and_head_none_polls_current_branch(capsys):
    calls, run = _stub(PROTECTED, [_reported(), _reported(), _reported(("build", "pass"))])
    clock = Clock()
    assert cli._await_required("main", None, "/d", run, clock.sleep, clock.now) == (True, "green")
    assert clock.sleeps == [15, 15] and len(capsys.readouterr().out.strip().splitlines()) == 1
    assert calls[1] == release.pr_checks_json_argv()
