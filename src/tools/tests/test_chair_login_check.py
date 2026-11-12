import json

from agent_tools import chair_login_check, claude_token, host_cmd
from agent_tools.chair_exec import ArgvResult
from agent_tools.chair_login_check import check_login_on_host
from agent_tools.remote_argv import ssh_argv, token_status_argv

NOW = "2026-09-28T00:00:00+00:00"
SSH_BOUND = ["ssh", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
TOKEN_ARGV = token_status_argv(probe=True)
TOKEN_PATH = "/home/user/.config/coxswain/claude-token"
NO_FILE_REASON = f"claude token: no token file at {TOKEN_PATH}"
REJECTED_REASON = "claude token: rejected: invalid x-api-key"
# Built by the verb's own report function, so a change to its keys or reasons fails here.
LOGGED_IN = json.dumps(claude_token.status_report(TOKEN_PATH, True, '{"loggedIn": true}', 0, ""))
NO_FILE = json.dumps(claude_token.status_report(TOKEN_PATH, False, "", None, ""))
REJECTED = json.dumps(claude_token.status_report(TOKEN_PATH, True, "", 1, "invalid x-api-key"))


def _reply(code: int, stdout: str = "", stderr: str = "") -> ArgvResult:
    return ArgvResult(code, stdout, stderr, stdout + stderr)


def _versions_arg(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--versions") + 1])


def _recording_cli(captured: list, argvs: list | None = None) -> object:
    """Records a beat's `--versions` or a login-merge's `--login-json` payload in `captured`, and every
    argv in `argvs`. A set-state call is only in `argvs`."""

    def cli_run(argv: list[str]) -> dict:
        if argvs is not None:
            argvs.append(argv)
        if argv[:2] == ["host", "set-state"]:
            return {"state": argv[3]}
        flag = "--login-json" if argv[:2] == ["host", "login"] else "--versions"
        captured.append(json.loads(argv[argv.index(flag) + 1]))
        return {"versions_json": captured[-1]}

    return cli_run


def _fake(statuses: list[ArgvResult]):
    """A runner that answers token-verb calls from `statuses` in order, and records every argv. Any other
    argv raises, so a stray `claude` call fails loudly."""
    calls: list = []
    queue = list(statuses)

    def run(argv: list[str]) -> ArgvResult:
        calls.append(argv)
        text = argv[-1] if argv[0] == "ssh" else " ".join(argv)
        if "setup token status --probe" in text:
            return queue.pop(0)
        raise AssertionError(f"unexpected argv {argv}")

    return run, calls


def _check(statuses, versions=None) -> tuple[dict, list]:
    run, calls = _fake(statuses)
    captured: list = []
    check_login_on_host("shed", "user@shed", versions or {}, NOW, run, _recording_cli(captured))
    return captured[0], calls


def test_a_logged_in_host_merges_login_ok_true_and_now_and_keeps_existing_keys() -> None:
    run, calls = _fake([_reply(0, LOGGED_IN)])

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    row = check_login_on_host("shed", "user@shed", {"cox": "1.2.3"}, NOW, run, cli_run)

    versions = row["versions_json"]
    assert versions["login_ok"] is True
    assert versions["login_checked_at"] == NOW
    assert versions["login_failures"] == 0
    assert versions["cox"] == "1.2.3"
    assert calls == [ssh_argv("user@shed", TOKEN_ARGV)]


def test_a_missing_token_file_blocks_the_host_on_the_first_check_with_the_named_reason() -> None:
    versions, calls = _check([_reply(1, NO_FILE)])

    assert (versions["login_ok"], versions["login_failures"]) == (False, 1)
    assert versions["reason"] == NO_FILE_REASON
    assert len(calls) == 1


def test_a_rejected_token_counts_a_failure_and_blocks_on_the_second() -> None:
    run, calls = _fake([_reply(1, REJECTED)] * 2)
    captured: list = []
    cli_run = _recording_cli(captured)

    check_login_on_host("shed", "user@shed", {}, NOW, run, cli_run)
    check_login_on_host("shed", "user@shed", captured[0], NOW, run, cli_run)

    assert [(v["login_ok"], v["login_failures"]) for v in captured] == [(None, 1), (False, 2)]
    assert [v["reason"] for v in captured] == [REJECTED_REASON] * 2
    assert len(calls) == 2


def test_a_host_without_cox_replies_non_json_and_counts_a_failure_without_blocking_at_once() -> None:
    versions, _ = _check([_reply(127, "", "cox: command not found")])

    assert (versions["login_ok"], versions["login_failures"]) == (None, 1)
    assert versions["reason"] == "exit 127: cox: command not found"
    assert "unreachable_failures" not in versions


def test_a_failure_reply_without_a_reason_falls_back_to_the_exit_code_and_output() -> None:
    versions, _ = _check([_reply(1, '{"loggedIn": false, "tokenFile": true}')])

    assert versions["login_ok"] is None
    assert versions["reason"] == 'exit 1: {"loggedIn": false, "tokenFile": true}'


def test_no_argv_given_to_the_runner_carries_a_token(monkeypatch) -> None:
    secret = "sk-ant-oat01-secret"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", secret)
    _, calls = _check([_reply(0, LOGGED_IN)])

    assert all(secret not in part and not part.startswith("CLAUDE_CODE_OAUTH_TOKEN") for argv in calls for part in argv)


def test_a_stderr_line_before_the_json_still_parses_as_logged_in() -> None:
    versions, _ = _check([_reply(0, LOGGED_IN, "warning: update available\n")])

    assert versions["login_ok"] is True
    assert versions["login_failures"] == 0


def test_the_loop_s_own_host_is_checked_locally_with_no_ssh(monkeypatch) -> None:
    monkeypatch.setattr(chair_login_check.socket, "gethostname", lambda: "host-b")
    run, calls = _fake([_reply(0, LOGGED_IN)])
    captured: list = []

    check_login_on_host("host-b", "user@host-b", {}, NOW, run, _recording_cli(captured))

    assert calls == [TOKEN_ARGV]
    assert captured[0] == {"login_ok": True, "login_checked_at": NOW, "check": "claude_auth", "login_failures": 0}


def test_a_different_host_than_the_loop_s_own_is_still_checked_over_ssh(monkeypatch) -> None:
    monkeypatch.setattr(chair_login_check.socket, "gethostname", lambda: "host-b")
    run, calls = _fake([_reply(0, LOGGED_IN)])

    check_login_on_host("shed", "user@shed", {}, NOW, run, _recording_cli([]))

    assert calls == [ssh_argv("user@shed", TOKEN_ARGV)]


def test_a_claude_code_runner_call_builds_the_same_argv_and_never_calls_env_check_argv(monkeypatch) -> None:
    run, calls = _fake([_reply(0, LOGGED_IN)])
    captured: list = []

    def _boom(_var: str) -> list[str]:
        raise AssertionError("env_check_argv must not be called for the claude-code runner")

    monkeypatch.setattr(chair_login_check, "env_check_argv", _boom)

    check_login_on_host("shed", "user@shed", {}, NOW, run, _recording_cli(captured), runner="claude-code")

    assert calls == [ssh_argv("user@shed", TOKEN_ARGV)]
    assert captured[0] == {"login_ok": True, "login_checked_at": NOW, "check": "claude_auth", "login_failures": 0}


def test_one_unreadable_reply_stores_none_with_a_reason_and_does_not_block() -> None:
    versions, calls = _check([_reply(1, "", "connection refused")])

    assert versions["login_ok"] is None
    assert versions["login_failures"] == 1
    assert versions["reason"] == "exit 1: connection refused"
    assert len(calls) == 1


def test_a_second_unreadable_reply_blocks_the_host() -> None:
    versions, _ = _check([_reply(1, "", "connection refused")], {"login_ok": None, "login_failures": 1})

    assert versions["login_ok"] is False
    assert versions["login_failures"] == 2


def test_a_pass_after_a_failure_clears_the_stored_reason_and_the_failure_count() -> None:
    stored = {"login_ok": None, "login_failures": 1, "reason": REJECTED_REASON}
    versions, _ = _check([_reply(0, LOGGED_IN)], stored)

    assert versions["login_ok"] is True
    assert versions["login_failures"] == 0
    assert "reason" not in versions


def test_the_reason_keeps_only_the_first_200_characters_of_output() -> None:
    versions, _ = _check([_reply(7, "x" * 500)])

    assert versions["reason"] == "exit 7: " + "x" * 200


def test_login_ok_keeps_its_tuple_runner_contract_and_asks_the_token_verb() -> None:
    asked: list = []

    def run(argv: list[str]) -> tuple[int, str]:
        asked.append(argv)
        return 0, LOGGED_IN

    assert chair_login_check._login_ok("shed", "u@shed", run) is True
    assert asked == [ssh_argv("u@shed", token_status_argv())]
    assert "--probe" not in asked[0][-1]
    assert chair_login_check._login_ok("shed", "u@shed", lambda argv: (1, REJECTED)) is False
    assert chair_login_check._login_ok("shed", "u@shed", lambda argv: (1, NO_FILE)) is False
    assert chair_login_check._login_ok("shed", "u@shed", lambda argv: (1, "refused")) is None


def test_an_env_vars_runner_with_every_variable_present_is_logged_in() -> None:
    def ssh_run(argv: list[str]) -> ArgvResult:
        return _reply(0, "some-secret-value\n")

    row = check_login_on_host(
        "shed", "user@shed", {}, NOW, ssh_run, lambda argv: {"versions_json": _versions_arg(argv)},
        runner="litellm", env_names=("MY_API_KEY",),
    )

    versions = row["versions_json"]
    assert versions["login_ok"] is True
    assert versions["check"] == "env_vars"
    assert versions["login_checked_at"] == NOW
    assert "reason" not in versions
    assert "login_failures" not in versions


def test_an_env_vars_runner_with_a_missing_variable_names_it_in_the_reason() -> None:
    row = check_login_on_host(
        "shed", "user@shed", {}, NOW, lambda argv: _reply(0, ""), lambda argv: {"versions_json": _versions_arg(argv)},
        runner="litellm", env_names=("MY_API_KEY",),
    )

    versions = row["versions_json"]
    assert versions["login_ok"] is False
    assert versions["check"] == "env_vars"
    assert "MY_API_KEY" in versions["reason"]


def test_an_env_vars_runner_on_an_unreachable_host_is_counted_unreachable_not_logged_out() -> None:
    captured: list = []
    check_login_on_host(
        "shed", "user@shed", {}, NOW,
        lambda argv: _reply(255, "", "ssh: connect to host x: Connection refused"),
        _recording_cli(captured),
        runner="litellm", env_names=("MY_API_KEY",),
    )

    assert captured == [{"unreachable_failures": 1, "login_checked_at": NOW, "reason": captured[0]["reason"]}]
    assert captured[0]["reason"].startswith("exit 255: ssh: connect to host x: Connection refused")


def test_an_env_vars_runner_whose_printenv_exits_1_is_missing_not_unreachable() -> None:
    captured: list = []
    check_login_on_host(
        "shed", "user@shed", {}, NOW, lambda argv: _reply(1, ""), _recording_cli(captured),
        runner="litellm", env_names=("MY_API_KEY",),
    )

    assert captured[0]["login_ok"] is False
    assert captured[0]["reason"] == "missing MY_API_KEY"


def test_an_env_vars_runner_with_no_env_names_is_not_logged_in_and_runs_nothing() -> None:
    calls: list = []

    def ssh_run(argv: list[str]) -> ArgvResult:
        calls.append(argv)
        return _reply(0, "unused")

    row = check_login_on_host(
        "shed", "user@shed", {}, NOW, ssh_run, lambda argv: {"versions_json": _versions_arg(argv)}, runner="litellm"
    )

    assert row["versions_json"] == {
        "login_ok": False,
        "login_checked_at": NOW,
        "check": "env_vars",
        "reason": "no auth_env or endpoint_env configured",
    }
    assert calls == []


def test_a_failed_check_stores_the_login_url_from_the_ssh_output() -> None:
    output = "# Tailscale SSH requires an additional check.\n# To authenticate, visit: https://login.tailscale.com/a/abc123\n"
    versions, _ = _check([_reply(255, "", output)])

    assert versions["login_ok"] is None
    assert versions["login_url"] == "https://login.tailscale.com/a/abc123"


def test_a_failed_check_with_no_url_stores_no_login_url_key() -> None:
    versions, _ = _check([_reply(1, "", "connection refused")])

    assert "login_url" not in versions


def test_a_passing_check_clears_a_stored_login_url() -> None:
    stored = {"login_ok": False, "login_url": "https://login.tailscale.com/a/abc123"}
    versions, _ = _check([_reply(0, LOGGED_IN)], stored)

    assert versions == {"login_ok": True, "login_checked_at": NOW, "check": "claude_auth", "login_failures": 0}


TIMED_OUT = "ssh: timed out after 30s"
SET_STATE = host_cmd.set_state_argv("shed", "unreachable", "chair")


def _set_states(argvs: list) -> list:
    return [argv for argv in argvs if argv[:2] == ["host", "set-state"]]


def test_one_timed_out_reply_records_one_unreachable_failure_and_marks_nothing() -> None:
    run, calls = _fake([_reply(124, "", TIMED_OUT)])
    captured: list = []
    argvs: list = []

    row = check_login_on_host("shed", "user@shed", {"login_failures": 1}, NOW, run, _recording_cli(captured, argvs))

    assert captured == [{"unreachable_failures": 1, "login_checked_at": NOW, "reason": f"exit 124: {TIMED_OUT}"}]
    assert [argv[:3] for argv in argvs] == [["host", "login", "shed"]]
    assert "login_ok" not in captured[0]
    assert len(calls) == 1
    assert row == {"versions_json": captured[0]}


def test_a_second_timed_out_reply_in_a_row_adds_unreachable_since_and_sets_the_state() -> None:
    run, _ = _fake([_reply(124, "", TIMED_OUT)] * 2)
    captured: list = []
    argvs: list = []
    cli_run = _recording_cli(captured, argvs)

    check_login_on_host("shed", "user@shed", {}, NOW, run, cli_run)
    row = check_login_on_host("shed", "user@shed", captured[0], NOW, run, cli_run)

    assert captured[1]["unreachable_failures"] == 2
    assert captured[1]["unreachable_since"] == NOW
    assert "unreachable_since" not in captured[0]
    assert _set_states(argvs) == [SET_STATE]
    assert argvs[-1] == SET_STATE
    assert row == {"state": "unreachable"}


def test_a_rejected_token_twice_still_blocks_and_never_sets_the_state() -> None:
    run, _ = _fake([_reply(1, REJECTED)] * 2)
    captured: list = []
    argvs: list = []
    cli_run = _recording_cli(captured, argvs)

    check_login_on_host("shed", "user@shed", {}, NOW, run, cli_run)
    check_login_on_host("shed", "user@shed", captured[0], NOW, run, cli_run)

    assert [(v["login_ok"], v["login_failures"]) for v in captured] == [(None, 1), (False, 2)]
    assert all("unreachable_failures" not in v for v in captured)
    assert _set_states(argvs) == []


def test_a_permission_denied_255_reply_is_not_counted_unreachable() -> None:
    versions, _ = _check([_reply(255, "", "user@shed: Permission denied (publickey).")])

    assert versions["login_failures"] == 1
    assert versions["login_ok"] is None
    assert "unreachable_failures" not in versions


def test_a_pass_after_one_unreachable_check_clears_the_streak() -> None:
    run, _ = _fake([_reply(124, "", TIMED_OUT), _reply(0, LOGGED_IN)])
    captured: list = []
    cli_run = _recording_cli(captured)

    check_login_on_host("shed", "user@shed", {}, NOW, run, cli_run)
    check_login_on_host("shed", "user@shed", captured[0], NOW, run, cli_run)

    assert captured[1] == {"login_ok": True, "login_checked_at": NOW, "check": "claude_auth", "login_failures": 0}
