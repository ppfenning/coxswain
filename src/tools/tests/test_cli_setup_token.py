import json
import os
import stat

import pytest

from agent_tools import claude_token
from agent_tools.cli import main


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(claude_token.TOKEN_ENV, raising=False)


def _path(tmp_path):
    return claude_token.token_path(str(tmp_path / "home"))


def test_from_file_writes_a_mode_600_file_and_exits_zero(tmp_path, capsys):
    source = tmp_path / "given.txt"
    source.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-abc\n")
    rc = main(["setup", "token", "--from-file", str(source)])
    path = _path(tmp_path)
    assert rc == 0
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert claude_token.read_token(path) == "sk-ant-oat01-abc"
    assert path in capsys.readouterr().out


def test_token_flag_stores_the_given_token(tmp_path):
    rc = main(["setup", "token", "--token", "sk-ant-oat01-flag"])
    assert rc == 0
    assert claude_token.read_token(_path(tmp_path)) == "sk-ant-oat01-flag"


def test_status_with_no_file_exits_one_and_prints_token_file_false(tmp_path, capsys):
    rc = main(["setup", "token", "status"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["tokenFile"] is False


def test_status_does_not_set_the_token_variable_on_the_host(tmp_path, monkeypatch):
    main(["setup", "token", "status"])
    assert claude_token.TOKEN_ENV not in os.environ


def test_help_names_the_source_the_mode_and_the_untouched_sessions(capsys):
    with pytest.raises(SystemExit):
        main(["setup", "token", "--help"])
    out = capsys.readouterr().out
    assert "claude setup-token" in out
    assert "mode 600" in out
    assert "Interactive claude sessions are untouched" in out
