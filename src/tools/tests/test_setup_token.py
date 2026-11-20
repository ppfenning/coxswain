import json
import os
import stat
import sys

import pytest

from agent_tools.claude_token import TOKEN_ENV, read_token, write_token
from agent_tools.setup_token import resolve_source, run_command, status, store, token_fact

TOKEN = "sk-ant-oat01-secret"
LOGGED_IN = '{"loggedIn": true}'


def no_prompt(_):
    raise AssertionError("must not prompt")


def no_read(_):
    raise AssertionError("must not read")


def test_resolve_source_precedence():
    assert resolve_source("a", "b", "c") == "a"
    assert resolve_source(None, "b", "c") == "b"
    assert resolve_source(None, None, "c") == "c"
    assert resolve_source(None, None, None) is None


def test_resolve_source_given_but_unparseable_does_not_fall_through():
    assert resolve_source("two words", "valid", "valid") is None
    assert resolve_source(None, "", "valid") is None


def test_store_flag_writes_private_file_and_prints_no_token(tmp_path, capsys):
    path = str(tmp_path / "cfg" / "claude-token")
    code = store(
        TOKEN,
        None,
        path=path,
        interactive=False,
        ask_secret=no_prompt,
        read_text=no_read,
        write=write_token,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert read_token(path) == TOKEN
    assert path in out and "private" in out
    assert TOKEN not in out


def test_store_from_name_value_file(tmp_path, capsys):
    path = str(tmp_path / "claude-token")
    code = store(
        None,
        "/x/old",
        path=path,
        interactive=False,
        ask_secret=no_prompt,
        read_text=lambda p: f"{TOKEN_ENV}={TOKEN}\n",
        write=write_token,
    )
    assert code == 0
    assert read_token(path) == TOKEN
    assert TOKEN not in capsys.readouterr().out


def test_store_prompts_when_interactive(tmp_path):
    path = str(tmp_path / "claude-token")
    code = store(
        None,
        None,
        path=path,
        interactive=True,
        ask_secret=lambda p: TOKEN + "\n",
        read_text=no_read,
        write=write_token,
    )
    assert code == 0
    assert read_token(path) == TOKEN


def test_store_refuses_without_source_or_terminal(tmp_path, capsys):
    path = str(tmp_path / "claude-token")
    code = store(
        None,
        None,
        path=path,
        interactive=False,
        ask_secret=no_prompt,
        read_text=no_read,
        write=write_token,
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "claude setup-token" in out and "--token" in out and "--from-file" in out
    assert not os.path.exists(path)


def test_store_refuses_unparseable_flag_without_echo(tmp_path, capsys):
    path = str(tmp_path / "claude-token")
    code = store(
        "bad token",
        None,
        path=path,
        interactive=False,
        ask_secret=no_prompt,
        read_text=no_read,
        write=write_token,
    )
    assert code == 2
    assert "bad token" not in capsys.readouterr().out
    assert not os.path.exists(path)


def recorder(results):
    calls = []

    def run(argv, env):
        calls.append((argv, dict(env)))
        return results[len(calls) - 1]

    return run, calls


def test_status_no_file_runs_nothing(capsys):
    def run(argv, env):
        raise AssertionError("must not run")

    code = status(True, path="/p", read_token=lambda p: None, run=run, environ={})
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["tokenFile"] is False and report["loggedIn"] is False


def test_status_takes_a_token_kept_in_the_environment_when_there_is_no_file(capsys):
    run, calls = recorder([(0, LOGGED_IN), (0, "ok")])
    code = status(True, path="/p", read_token=lambda p: None, run=run, environ={TOKEN_ENV: f"  {TOKEN}\n", "A": "1"})
    out = capsys.readouterr().out
    report = json.loads(out)
    assert code == 0 and report["loggedIn"] is True and report["tokenFile"] is True
    assert report["tokenSource"] == TOKEN_ENV and TOKEN not in out, "the source is named; the value never is"
    assert calls[0][1][TOKEN_ENV] == TOKEN and calls[0][1]["A"] == "1" and len(calls) == 2


def test_status_prefers_the_file_over_the_environment_and_says_nothing_of_a_source(capsys):
    run, calls = recorder([(0, LOGGED_IN)])
    assert status(False, path="/p", read_token=lambda p: TOKEN, run=run, environ={TOKEN_ENV: "other"}) == 0
    assert "tokenSource" not in json.loads(capsys.readouterr().out) and calls[0][1][TOKEN_ENV] == TOKEN


def test_status_with_an_empty_environment_token_is_still_no_token(capsys):
    def run(argv, env):
        raise AssertionError("must not run")

    assert status(True, path="/p", read_token=lambda p: None, run=run, environ={TOKEN_ENV: "  "}) == 1
    assert json.loads(capsys.readouterr().out)["tokenFile"] is False


def test_token_fact_counts_a_token_in_the_environment_only_when_there_is_no_file(tmp_path):
    from agent_tools.setup_token import token_fact

    missing = str(tmp_path / "claude-token")
    assert token_fact(missing, {})["present"] is False
    fact = token_fact(missing, {TOKEN_ENV: TOKEN})
    assert fact == {"path": missing, "present": True, "empty": False, "mode": None, "source": TOKEN_ENV}
    stored = tmp_path / "stored"
    stored.write_text(TOKEN)
    stored.chmod(0o600)
    assert "source" not in token_fact(str(stored), {TOKEN_ENV: "other"})


def test_status_passes_stored_token_over_caller_env(capsys):
    run, calls = recorder([(0, LOGGED_IN)])
    code = status(False, path="/p", read_token=lambda p: TOKEN, run=run, environ={TOKEN_ENV: "other", "A": "1"})
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["loggedIn"] is True
    assert [c[0] for c in calls] == [["claude", "auth", "status"]]
    assert calls[0][1][TOKEN_ENV] == TOKEN
    assert calls[0][1]["A"] == "1"


def test_status_probe_runs_one_turn_and_nonzero_is_rejected(capsys):
    run, calls = recorder([(0, LOGGED_IN), (1, "401 invalid " + TOKEN)])
    code = status(True, path="/p", read_token=lambda p: TOKEN, run=run, environ={})
    out = capsys.readouterr().out
    report = json.loads(out)
    assert code == 1
    assert calls[1][0] == ["claude", "-p", "ok", "--max-turns", "1", "--model", "haiku"]
    assert calls[1][1][TOKEN_ENV] == TOKEN
    assert report["reason"].startswith("claude token: rejected: 401 invalid")
    assert TOKEN not in out


def test_status_probe_success(capsys):
    run, calls = recorder([(0, LOGGED_IN), (0, "ok")])
    assert status(True, path="/p", read_token=lambda p: TOKEN, run=run, environ={}) == 0
    assert len(calls) == 2


def test_status_missing_claude_binary_is_a_failure_not_a_raise(capsys):
    def run(argv, env):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'claude'")

    code = status(True, path="/p", read_token=lambda p: TOKEN, run=run, environ={})
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["loggedIn"] is False and "No such file" in report["reason"]


def test_token_fact_absent(tmp_path):
    path = str(tmp_path / "none")
    assert token_fact(path) == {"path": path, "present": False, "empty": False, "mode": None}


def test_token_fact_empty(tmp_path):
    p = tmp_path / "t"
    p.write_text("\n")
    p.chmod(0o600)
    assert token_fact(str(p)) == {"path": str(p), "present": True, "empty": True, "mode": 0o600}


def test_token_fact_wide_mode(tmp_path):
    p = tmp_path / "t"
    p.write_text(TOKEN + "\n")
    p.chmod(0o644)
    assert token_fact(str(p)) == {"path": str(p), "present": True, "empty": False, "mode": 0o644}


def test_token_fact_unreadable_file_is_not_called_empty(tmp_path):
    p = tmp_path / "t"
    p.write_text(TOKEN + "\n")
    p.chmod(0)
    try:
        if os.access(p, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        assert token_fact(str(p)) == {"path": str(p), "present": True, "empty": False, "mode": 0}
    finally:
        p.chmod(0o600)


def test_status_redacts_token_straddling_the_tail_cut(capsys):
    out = "x" * 10 + TOKEN + "y" * 190
    run, _ = recorder([(0, LOGGED_IN), (1, out)])
    status(True, path="/p", read_token=lambda p: TOKEN, run=run, environ={})
    printed = capsys.readouterr().out
    assert TOKEN[-8:] not in printed
    assert "***" in json.loads(printed)["reason"]


def test_status_redacts_token_in_a_failed_auth_status(capsys):
    run, _ = recorder([(2, "boom " + TOKEN)])
    assert status(False, path="/p", read_token=lambda p: TOKEN, run=run, environ={}) == 1
    printed = capsys.readouterr().out
    assert TOKEN not in printed and "boom ***" in printed


def test_status_not_logged_in_json_skips_the_paid_probe(capsys):
    run, calls = recorder([(1, '{"loggedIn": false}'), (0, "ok")])
    code = status(True, path="/p", read_token=lambda p: TOKEN, run=run, environ={})
    assert code == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["tokenFile"] is True


def fake_claude(tmp_path, body):
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return {"PATH": str(tmp_path)}


@pytest.mark.skipif(sys.platform == "win32", reason="needs /bin/sh")
def test_status_against_a_real_subprocess_with_stderr_noise(tmp_path, capsys):
    body = (
        'case "$1" in\n'
        '  auth) echo "warning" >&2; echo \'{"loggedIn": true}\';;\n'
        '  -p) echo "denied $CLAUDE_CODE_OAUTH_TOKEN" >&2; exit 1;;\n'
        "esac\n"
    )
    env = fake_claude(tmp_path, body)
    code = status(True, path="/p", read_token=lambda p: TOKEN, run=run_command, environ=env)
    printed = capsys.readouterr().out
    assert code == 1
    assert json.loads(printed)["reason"] == "claude token: rejected: denied ***\n"
    assert TOKEN not in printed
    code = status(False, path="/p", read_token=lambda p: TOKEN, run=run_command, environ=env)
    assert code == 0


def test_status_with_no_claude_binary_on_a_real_path(tmp_path, capsys):
    code = status(True, path="/p", read_token=lambda p: TOKEN, run=run_command, environ={"PATH": str(tmp_path)})
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["loggedIn"] is False and "claude" in report["reason"]
