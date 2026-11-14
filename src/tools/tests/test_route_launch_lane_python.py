import os

import pytest
from test_cli_repo_home_edges import _live_launch

from agent_tools import cli

pytestmark = pytest.mark.usefixtures("board_store")  # the store is the board; these launches read its rows


def _stage(tmp_path, monkeypatch, *, venv_python):
    """No running interpreter on disk; an install checkout with or without its venv python; a probe that finds
    `python` only when the PATH it is given carries the install venv bin. Returns the probe's seen PATHs."""
    install = tmp_path / "install"
    install.mkdir()
    if venv_python:
        (install / ".venv" / "bin").mkdir(parents=True)
        (install / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(cli, "_running_python", lambda: tmp_path / "nowhere" / "python")
    monkeypatch.setattr(cli, "_install_checkout", lambda: install)
    monkeypatch.setattr(cli, "_repo_check_tools", lambda repo: cli.lane_toolchain.CheckTools(("python",), ()))
    seen = []

    def probe(argv, env=None):
        path = (env or os.environ)["PATH"]
        seen.append(path)
        found = str(install / ".venv" / "bin") in path.split(os.pathsep)
        return 0, "" if found else "python\n"

    monkeypatch.setattr(cli, "_host_env_probe", probe)
    return install, seen


def test_install_venv_python_is_found_and_follows_the_repo_and_harness_bins_on_the_lane_path(tmp_path, monkeypatch):
    install, seen = _stage(tmp_path, monkeypatch, venv_python=True)
    rc, started = _live_launch(tmp_path, monkeypatch)
    assert rc == 0
    assert seen[0].split(os.pathsep)[0] == str(install / ".venv" / "bin")
    [(_, env)] = started
    lane = env["PATH"].split(os.pathsep)
    assert lane[:3] == [str(tmp_path / "repo" / ".venv" / "bin"), str(tmp_path / "harness" / ".venv" / "bin"), str(install / ".venv" / "bin")]


def test_no_lane_python_returns_2_and_the_refusal_names_each_path_tried(tmp_path, monkeypatch, capsys):
    install, seen = _stage(tmp_path, monkeypatch, venv_python=False)
    rc, started = _live_launch(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 2
    assert started == []
    assert seen == []
    assert str(tmp_path / "nowhere" / "python") in out
    assert str(install / ".venv" / "bin" / "python") in out
