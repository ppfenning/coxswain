import json

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import doctor_argv, ssh_argv, token_status_argv
from agent_tools.remote_doctor import auth_verdict, doctor_on_host

_NOT_LOGGED_IN = "claude auth: not logged in on the host (run claude auth login there)"
_NO_FILE = "claude token: no token file at /srv/coxswain/claude-token"
_REJECTED = "claude token: rejected: 401 invalid bearer token"
_LOGGED_IN = json.dumps({"loggedIn": True, "tokenFile": True, "reason": None})
_FILE_REMOVED = json.dumps({"loggedIn": False, "tokenFile": False, "reason": _NO_FILE})
_TOKEN_REJECTED = json.dumps({"loggedIn": False, "tokenFile": True, "reason": _REJECTED})


def _fake_run(auth_output, doctor_code=0, seen=None):
    def run(argv):
        if seen is not None:
            seen.append(argv)
        if "setup token status --probe" in argv[-1]:
            return 0, auth_output
        return doctor_code, "ok rows"

    return run


def test_token_status_argv_probe_is_the_cox_verb():
    assert token_status_argv(probe=True) == ["cox", "setup", "token", "status", "--probe"]


def test_a_logged_in_status_has_no_verdict():
    assert auth_verdict(_LOGGED_IN) is None


def test_a_reply_without_a_reason_says_to_log_in_on_the_host():
    assert auth_verdict('{"loggedIn": false, "authMethod": "none"}') == _NOT_LOGGED_IN


def test_a_removed_token_file_gives_the_replys_own_reason():
    assert auth_verdict(_FILE_REMOVED) == _NO_FILE


def test_a_rejected_token_gives_the_replys_own_reason():
    assert auth_verdict(_TOKEN_REJECTED) == _REJECTED


def test_output_that_is_not_json_is_unreadable():
    assert auth_verdict("garbage") == "claude token: status unreadable: garbage"


def test_unreadable_output_is_cut_at_eighty_characters():
    assert auth_verdict("x" * 100) == f"claude token: status unreadable: {'x' * 80}"


def test_doctor_on_host_fails_a_passing_doctor_when_the_token_file_is_removed():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run(_FILE_REMOVED))
    assert code == 1
    assert rows == ["ok rows", _NO_FILE]


def test_doctor_on_host_fails_a_passing_doctor_when_the_token_is_rejected():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run(_TOKEN_REJECTED))
    assert code == 1
    assert rows == ["ok rows", _REJECTED]


def test_doctor_on_host_reports_garbage_output_as_unreadable():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run("garbage"))
    assert code == 1
    assert rows == ["ok rows", "claude token: status unreadable: garbage"]


def test_doctor_on_host_adds_an_ok_row_when_the_token_is_accepted():
    host = LaneHost("box", "me@box", "/srv")
    assert doctor_on_host(host, _fake_run(_LOGGED_IN)) == (0, ["ok rows", "claude token     ok"])


def test_doctor_on_host_keeps_the_claude_token_row_when_runner_is_named_explicitly():
    host = LaneHost("box", "me@box", "/srv")
    result = doctor_on_host(host, _fake_run(_LOGGED_IN), runner="claude-code")
    assert result == (0, ["ok rows", "claude token     ok"])


def test_the_argv_the_host_receives_contains_no_token():
    host = LaneHost("box", "me@box", "/srv")
    seen = []
    doctor_on_host(host, _fake_run(_LOGGED_IN, seen=seen))
    assert seen == [ssh_argv("me@box", doctor_argv()), ssh_argv("me@box", token_status_argv(probe=True))]


def _fake_run_env(env_output, doctor_code=0):
    def run(argv):
        if argv[-1] == "printenv MY_API_KEY":
            return 0, env_output
        return doctor_code, "ok rows"

    return run


def test_doctor_on_host_adds_an_ok_env_vars_row_when_a_non_claude_runner_has_the_variable():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run_env("secret"), runner="codex", env_names=("MY_API_KEY",))
    assert code == 0
    assert rows == ["ok rows", "env vars      ok"]


def test_doctor_on_host_fails_a_passing_doctor_when_a_non_claude_runner_is_missing_a_variable():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run_env(""), runner="codex", env_names=("MY_API_KEY",))
    assert code == 1
    assert any("MY_API_KEY" in row for row in rows)
