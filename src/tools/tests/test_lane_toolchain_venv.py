import argparse
import datetime
import json
import shlex

import pytest

from agent_tools import cli
from agent_tools.lane_hosts import LaneHost
from agent_tools.lane_toolchain import CheckTools, lane_argv, lookup_argv
from agent_tools.remote_argv import ssh_argv
from agent_tools.remote_launch import LaunchError, launch_on_host

HOME = "/home/user"
REPO = "~/work/proj"
ABSOLUTE = "/home/user/work/proj"
RELATIVE = "work/proj"
FAR = LaneHost("far", "me@far", "/srv/ws")
LOGIN_LACKS = {"python", "ruff"}
VENV_HAS = {"python"}


def _words(argv):
    """The sh -c words after the script, however many ssh and login-shell quotings wrap them."""
    if argv[0] == "ssh":
        argv = shlex.split(shlex.split(argv[-1])[-1])
    return argv[argv.index("lane-toolchain") + 1:]


class Probe:
    """Reports each tool the login PATH lacks, minus the tools the repo venv holds when a repo directory was given."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, env=None):
        self.calls.append(argv)
        words = _words(argv)
        has_repo = "/" in words[0]
        tools = words[1:] if has_repo else words
        missing = [t for t in tools if t in LOGIN_LACKS and not (has_repo and t in VENV_HAS)]
        return 0, "\n".join(missing) + "\n"


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


@pytest.fixture
def probe(monkeypatch):
    stub = Probe()
    monkeypatch.setattr(cli, "_host_env_probe", stub)
    return stub


def test_the_repo_dir_is_absolute_here_home_relative_remote_and_empty_without_a_repo():
    assert cli._lane_repo_dir(REPO, remote=False) == ABSOLUTE
    assert cli._lane_repo_dir(REPO, remote=True) == RELATIVE
    assert cli._lane_repo_dir("", remote=True) == ""
    assert cli._lane_repo_dir(None, remote=False) == ""


def test_a_tool_in_the_repo_venv_is_not_refused_here_or_on_a_remote_host(probe):
    assert cli._toolchain_refusal("box", None, ("python",), repo=REPO) is None
    assert cli._toolchain_refusal("far", FAR.ssh, ("python",), repo=REPO) is None
    assert len(probe.calls) == 2


def test_with_no_repo_the_lookup_and_its_message_are_unchanged(probe):
    assert cli._toolchain_refusal("box", None, ("python",)) == "box: python not on the lane PATH"
    assert probe.calls == [lookup_argv(["python"])]


def test_a_tool_missing_from_both_names_the_lane_path_and_the_repo_venv(probe):
    expected = "far: ruff not on the lane PATH or in the repo venv"
    assert cli._toolchain_refusal("far", FAR.ssh, ("python", "ruff"), repo=REPO) == expected
    assert cli._toolchain_refusal("far", None, ("python", "ruff"), repo=REPO) == expected


def test_a_pinned_host_gets_the_same_probe_and_message_from_launch_on_host():
    stub = Probe()
    issued = []
    result = launch_on_host(
        FAR, "init", "run-1", "label", "2026-11-03T12:00:00+00:00", lambda argv: issued.append(argv) or 0,
        repo=REPO, home=HOME, probe=stub, tools=("python", "ruff"),
    )
    assert result == LaunchError("toolchain", "far: ruff not on the lane PATH or in the repo venv")
    assert stub.calls == [ssh_argv(FAR.ssh, lane_argv(["python", "ruff"], RELATIVE))]
    assert issued == []


def test_launch_on_host_without_a_repo_keeps_the_lane_path_only_probe():
    stub = Probe()
    result = launch_on_host(
        FAR, "init", "run-1", "label", "2026-11-03T12:00:00+00:00", lambda argv: 0,
        probe=stub, tools=("python",),
    )
    assert result == LaunchError("toolchain", "far: python not on the lane PATH")
    assert stub.calls == [ssh_argv(FAR.ssh, lane_argv(["python"]))]


def test_a_remote_probe_carries_the_home_relative_repo_and_ignores_a_venv_on_this_machine(probe, tmp_path):
    local_venv = tmp_path / "work" / "proj" / ".venv" / "bin"
    local_venv.mkdir(parents=True)
    (local_venv / "ruff").write_text("#!/bin/sh\n")
    (local_venv / "ruff").chmod(0o755)
    line = cli._toolchain_refusal("far", FAR.ssh, ("ruff",), repo=REPO)
    assert line == "far: ruff not on the lane PATH or in the repo venv"
    assert probe.calls == [ssh_argv(FAR.ssh, lane_argv(["ruff"], RELATIVE))]
    assert ABSOLUTE not in probe.calls[0][-1]


def test_a_local_probe_carries_the_absolute_expanded_repo(probe):
    cli._toolchain_refusal("box", None, ("python",), repo=REPO)
    assert probe.calls == [lookup_argv(["python"], ABSOLUTE)]


def test_placement_probes_a_remote_candidate_against_the_epic_repo(monkeypatch, probe, capsys):
    row = {
        "name": "far", "ssh": FAR.ssh, "capacity": 1, "weight": 1, "state": "active",
        "beat_at": datetime.datetime.now(datetime.UTC).isoformat(), "versions_json": json.dumps({"workspace_dir": FAR.workspace_dir}),
    }
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: [row])
    monkeypatch.setattr(cli.run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(cli, "_now_iso", lambda: "2026-11-03T12:00:00+00:00")
    monkeypatch.setattr(cli, "_local_max_in_flight", lambda runs_dir, profile, local: 1)
    monkeypatch.setattr(cli, "_read_text_or_none", lambda path: "")
    monkeypatch.setattr(cli, "_profile_path", lambda a: "profile.yaml")
    monkeypatch.setattr(cli, "_profile_lane_hosts", lambda text: ())
    monkeypatch.setattr(cli, "_lane_host_candidates", lambda text, rows, local="": (FAR,))
    monkeypatch.setattr(cli, "_dropped_ceilings", lambda a: ())
    monkeypatch.setattr(cli, "_epic_repo", lambda a, profile: REPO)
    monkeypatch.setattr(cli, "_repo_check_tools", lambda repo: CheckTools(("python",), ()))
    host, code = cli._place_or_refuse(argparse.Namespace(graph="epic", force=False), {"workspace_dir": "/ws"}, False)
    assert (host, code) == (FAR, None)
    assert probe.calls == [ssh_argv(FAR.ssh, lane_argv(["python"], RELATIVE))]
    assert "placed on far" in capsys.readouterr().out
