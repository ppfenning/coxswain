import pytest
from test_cli_repo_home_edges import _live_launch

from agent_tools import cli

pytestmark = pytest.mark.usefixtures("board_store")  # the store is the board; these launches read its rows


def test_capacity_is_exported_as_a_string_and_the_rest_is_kept():
    assert cli.lane_capacity_env({"PATH": "/bin"}, 3) == {"PATH": "/bin", "COX_LANE_CAPACITY": "3"}


def test_a_value_already_set_is_kept():
    assert cli.lane_capacity_env({"COX_LANE_CAPACITY": "7"}, 3) == {"COX_LANE_CAPACITY": "7"}


def test_the_input_is_not_mutated():
    env = {"PATH": "/bin"}
    cli.lane_capacity_env(env, 3)
    assert env == {"PATH": "/bin"}


def test_local_epic_launch_hands_the_spawn_the_host_capacity(tmp_path, monkeypatch):
    monkeypatch.delenv("COX_LANE_CAPACITY", raising=False)
    monkeypatch.setattr(cli, "_local_max_in_flight", lambda runs_dir, profile, hostname: 3)
    rc, started = _live_launch(tmp_path, monkeypatch)
    assert rc == 0
    [(_, env)] = started
    assert env["COX_LANE_CAPACITY"] == "3"


def test_local_epic_launch_keeps_a_capacity_already_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COX_LANE_CAPACITY", "7")
    monkeypatch.setattr(cli, "_local_max_in_flight", lambda runs_dir, profile, hostname: 3)
    rc, started = _live_launch(tmp_path, monkeypatch)
    assert rc == 0
    [(_, env)] = started
    assert env["COX_LANE_CAPACITY"] == "7"
