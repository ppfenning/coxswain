import os
import stat

from agent_tools.claude_token import (
    TOKEN_ENV,
    claude_env,
    parse_token_text,
    read_token,
    status_report,
    token_path,
    write_token,
)

LOGGED_IN = '{"loggedIn": true}'


def test_token_path():
    assert token_path("/h") == "/h/.config/agent-tools/claude-token"


def test_parse_bare():
    assert parse_token_text("sk-abc\n") == "sk-abc"


def test_parse_name_equals():
    assert parse_token_text(f"{TOKEN_ENV}=sk-abc\n") == "sk-abc"


def test_parse_export():
    assert parse_token_text(f"export {TOKEN_ENV}=sk-abc") == "sk-abc"


def test_parse_double_quotes():
    assert parse_token_text(f'{TOKEN_ENV}="sk-abc"') == "sk-abc"


def test_parse_single_quotes():
    assert parse_token_text(f"export {TOKEN_ENV}='sk-abc'") == "sk-abc"


def test_parse_surrounding_whitespace():
    assert parse_token_text("  \n  sk-abc  \n\n") == "sk-abc"


def test_parse_comment_plus_one_line():
    assert parse_token_text("# note\nsk-abc\n") == "sk-abc"


def test_parse_empty():
    assert parse_token_text("") is None
    assert parse_token_text("  \n") is None


def test_parse_several_lines():
    assert parse_token_text("sk-a\nsk-b\n") is None


def test_parse_inner_whitespace():
    assert parse_token_text("sk a") is None
    assert parse_token_text(f'{TOKEN_ENV}="sk a"') is None


def test_parse_empty_value():
    assert parse_token_text(f"{TOKEN_ENV}=") is None
    assert parse_token_text(f'{TOKEN_ENV}=""') is None


def test_claude_env_replaces_caller_value():
    environ = {"A": "1", TOKEN_ENV: "old"}
    got = claude_env(environ, "new")
    assert got == {"A": "1", TOKEN_ENV: "new"}
    assert environ == {"A": "1", TOKEN_ENV: "old"}


def test_claude_env_none_copies():
    environ = {"A": "1", TOKEN_ENV: "old"}
    got = claude_env(environ, None)
    assert got == environ
    assert got is not environ
    got["B"] = "2"
    assert "B" not in environ


def test_status_no_file():
    assert status_report("/p", False, LOGGED_IN, 0, "") == {
        "loggedIn": False,
        "tokenFile": False,
        "reason": "claude token: no token file at /p",
    }


def test_status_rejected():
    assert status_report("/p", True, LOGGED_IN, 1, "bad token") == {
        "loggedIn": False,
        "tokenFile": True,
        "reason": "claude token: rejected: bad token",
    }


def test_status_rejected_truncates_tail():
    got = status_report("/p", True, LOGGED_IN, 2, "x" * 300)
    assert got["reason"] == "claude token: rejected: " + "x" * 200


def test_status_not_logged_in():
    reason = "claude token: claude auth status does not report logged in"
    for stdout in ('{"loggedIn": false}', "not json", "[]", "{}", ""):
        assert status_report("/p", True, stdout, 0, "") == {
            "loggedIn": False,
            "tokenFile": True,
            "reason": reason,
        }


def test_status_ok():
    want = {"loggedIn": True, "tokenFile": True, "reason": None}
    assert status_report("/p", True, LOGGED_IN, 0, "") == want


def test_status_probe_not_run_is_ok():
    want = {"loggedIn": True, "tokenFile": True, "reason": None}
    assert status_report("/p", True, LOGGED_IN, None, "") == want


def test_read_token_good_and_missing(tmp_path):
    path = tmp_path / "t"
    path.write_text(f"export {TOKEN_ENV}=sk-abc\n")
    assert read_token(str(path)) == "sk-abc"
    assert read_token(str(tmp_path / "missing")) is None


def test_read_token_two_lines(tmp_path):
    path = tmp_path / "t"
    path.write_text("sk-a\nsk-b\n")
    assert read_token(str(path)) is None


def test_write_token_modes(tmp_path):
    os.umask(0o022)
    path = str(tmp_path / "a" / "b" / "claude-token")
    write_token(path, "sk-abc")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert read_token(path) == "sk-abc"
    assert os.listdir(os.path.dirname(path)) == ["claude-token"]


def test_write_token_replaces(tmp_path):
    path = str(tmp_path / "d" / "claude-token")
    write_token(path, "sk-one")
    write_token(path, "sk-two")
    assert read_token(path) == "sk-two"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
