import json

from lane_probe_replies import PASSING, PASSING_LINES, is_lane_probe

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import auth_status_argv, ssh_argv, token_status_argv
from agent_tools.remote_doctor import auth_verdict, doctor_on_host

_NOT_LOGGED_IN = "claude auth: not logged in on the host (run claude auth login there)"
_NO_FILE = "claude token: no token file at /srv/.config/coxswain/claude-token"
_REJECTED = "claude token: rejected: Invalid API key"
_TOKEN = "sk-ant-oat01-not-a-real-token"


def _reply(logged_in, token_file=True, reason=None):
    return json.dumps({"loggedIn": logged_in, "tokenFile": token_file, "reason": reason})


def _is_token_verb(argv):
    return " ".join(token_status_argv(probe=True)) in " ".join(argv)


def _fake_run(status_output, doctor_code=0, seen=None):
    def run(argv):
        if seen is not None:
            seen.append(argv)
        if is_lane_probe(argv):
            return 0, PASSING
        if _is_token_verb(argv):
            return 0, status_output
        return doctor_code, "ok rows"

    return run


def test_auth_status_argv_is_the_claude_command():
    assert auth_status_argv() == ["claude", "auth", "status"]


def test_a_logged_in_status_has_no_verdict():
    assert auth_verdict('{"loggedIn": true}') is None


def test_a_logged_out_status_with_no_reason_says_to_log_in_on_the_host():
    assert auth_verdict('{"loggedIn": false, "authMethod": "none"}') == _NOT_LOGGED_IN


def test_a_reply_for_a_removed_token_file_gives_its_own_reason():
    assert auth_verdict(_reply(False, token_file=False, reason=_NO_FILE)) == _NO_FILE


def test_a_rejected_token_gives_the_rejected_reason():
    assert auth_verdict(_reply(False, reason=_REJECTED)) == _REJECTED


def test_output_that_is_not_json_is_unreadable():
    assert auth_verdict("garbage") == "claude token: status unreadable: garbage"


def test_the_unreadable_line_keeps_the_first_80_characters():
    assert auth_verdict("x" * 200) == "claude token: status unreadable: " + "x" * 80


def test_doctor_on_host_asks_the_hosts_token_verb_and_never_claude_auth_status():
    seen: list[list[str]] = []
    doctor_on_host(LaneHost("box", "me@box", "/srv"), _fake_run(_reply(True), seen=seen))
    assert ssh_argv("me@box", token_status_argv(probe=True)) in seen
    assert not any("claude auth status" in " ".join(argv) for argv in seen)


def test_the_argv_given_to_the_runner_holds_no_token():
    seen: list[list[str]] = []
    doctor_on_host(LaneHost("box", "me@box", "/srv"), _fake_run(_reply(True), seen=seen))
    assert not any(_TOKEN in part or "CLAUDE_CODE_OAUTH_TOKEN" in part for argv in seen for part in argv)


def test_doctor_on_host_adds_an_ok_row_when_the_token_is_logged_in():
    host = LaneHost("box", "me@box", "/srv")
    assert doctor_on_host(host, _fake_run(_reply(True))) == (0, ["ok rows", "claude token     ok", *PASSING_LINES])


def test_doctor_on_host_keeps_the_claude_token_row_when_runner_is_named_explicitly():
    host = LaneHost("box", "me@box", "/srv")
    result = doctor_on_host(host, _fake_run(_reply(True)), runner="claude-code")
    assert result == (0, ["ok rows", "claude token     ok", *PASSING_LINES])


def test_doctor_on_host_names_a_removed_token_file_and_exits_1():
    host = LaneHost("box", "me@box", "/srv")
    assert doctor_on_host(host, _fake_run(_reply(False, token_file=False, reason=_NO_FILE))) == (1, ["ok rows", _NO_FILE, *PASSING_LINES])


def test_doctor_on_host_gives_a_rejected_tokens_reason_and_exits_1():
    host = LaneHost("box", "me@box", "/srv")
    assert doctor_on_host(host, _fake_run(_reply(False, reason=_REJECTED))) == (1, ["ok rows", _REJECTED, *PASSING_LINES])


def test_doctor_on_host_gives_the_unreadable_line_for_garbage():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run("sh: cox: command not found"))
    assert code == 1
    assert rows == ["ok rows", "claude token: status unreadable: sh: cox: command not found", *PASSING_LINES]


def test_a_failing_host_doctor_keeps_its_own_exit_code():
    host = LaneHost("box", "me@box", "/srv")
    assert doctor_on_host(host, _fake_run(_reply(False, reason=_REJECTED), doctor_code=3))[0] == 3


def _fake_run_env(env_output, doctor_code=0):
    def run(argv):
        if is_lane_probe(argv):
            return 0, PASSING
        if argv[-1] == "printenv MY_API_KEY":
            return 0, env_output
        return doctor_code, "ok rows"

    return run


def test_doctor_on_host_adds_an_ok_env_vars_row_when_a_non_claude_runner_has_the_variable():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run_env("secret"), runner="codex", env_names=("MY_API_KEY",))
    assert code == 0
    assert rows == ["ok rows", "env vars      ok", *PASSING_LINES]


def test_doctor_on_host_fails_a_passing_doctor_when_a_non_claude_runner_is_missing_a_variable():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run_env(""), runner="codex", env_names=("MY_API_KEY",))
    assert code == 1
    assert any("MY_API_KEY" in row for row in rows)
