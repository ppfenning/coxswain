import shlex

import pytest
from test_cli_launch_on import _fake_edge, _no_local_process, _setup
from test_route_cli import _unmeasured_window

from agent_tools import cli, usage_window
from agent_tools.cli import main
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import chair_courier_env, decompose_launch_argv, launch_argv, ssh_argv
from agent_tools.remote_launch import courier_ssh_of, launch_on_host, launch_plan

pytestmark = pytest.mark.usefixtures("board_store")  # the store is the board; these launches read its rows

HOST = LaneHost("box", "me@box", "/srv/ws")
COURIER = "pat@chair.example"
EPIC_NOW = [
    "cox", "route", "launch", "epic", "--local",
    "--initiative", "/srv/ws/work/init-x",
    "--run-id", "run-1",
    "--label", "lbl",
    "--no-claim",
]
DECOMPOSE_NOW = [
    "cox", "route", "launch", "decompose", "--local", "--detach",
    "--idea", "/srv/ws/work/intake/init-x.md",
    "--initiative-id", "init-x",
    "--run-id", "run-1",
    "--label", "lbl",
    "--no-claim",
]


def _decompose(**kwargs):
    return decompose_launch_argv("/srv/ws/work/intake/init-x.md", "init-x", "run-1", "lbl", **kwargs)


def test_without_a_courier_both_argvs_equal_the_current_output():
    assert launch_argv("/srv/ws/work/init-x", "run-1", "lbl") == EPIC_NOW
    assert _decompose() == DECOMPOSE_NOW
    assert launch_argv("/srv/ws/work/init-x", "run-1", "lbl", chair_courier=None) == EPIC_NOW
    assert _decompose(chair_courier="") == DECOMPOSE_NOW


def test_with_a_courier_both_argvs_set_the_variable_and_start_the_same_launch():
    prefix = ["env", f"COX_CHAIR_COURIER={COURIER}"]
    assert launch_argv("/srv/ws/work/init-x", "run-1", "lbl", chair_courier=COURIER) == [*prefix, *EPIC_NOW]
    assert _decompose(chair_courier=COURIER) == [*prefix, *DECOMPOSE_NOW]


def test_the_phase_flag_survives_the_courier_prefix():
    argv = launch_argv("/srv/ws/work/init-x", "run-1", "lbl", "p1", chair_courier=COURIER)
    assert argv[-2:] == ["--phase", "p1"]


def test_the_env_prefix_is_empty_for_no_destination():
    assert chair_courier_env(None) == []
    assert chair_courier_env("") == []
    assert chair_courier_env(COURIER) == ["env", f"COX_CHAIR_COURIER={COURIER}"]


def test_ssh_argv_keeps_the_login_shell_wrapping_for_a_launch_with_the_prefix():
    plain = ssh_argv("me@box", EPIC_NOW)
    prefixed = ssh_argv("me@box", launch_argv("/srv/ws/work/init-x", "run-1", "lbl", chair_courier=COURIER))
    assert prefixed[:-1] == plain[:-1]
    assert shlex.split(prefixed[-1])[:3] == ["/bin/sh", "-c", 'exec "${SHELL:-/bin/sh}" -l -i -c "$1"']
    decompose = ssh_argv("me@box", _decompose(chair_courier=COURIER))
    assert shlex.split(decompose[-1])[1] == "-c"


def test_a_user_at_host_destination_survives_both_quoting_layers():
    argv = ssh_argv("me@box", launch_argv("/srv/ws/work/init-x", "run-1", "lbl", chair_courier=COURIER))
    assert argv[-2] == "me@box"
    outer = shlex.split(argv[-1])
    assert shlex.split(outer[-1]) == ["env", f"COX_CHAIR_COURIER={COURIER}", *EPIC_NOW]


def test_a_non_launch_command_is_not_wrapped_by_the_env_prefix_rule():
    assert ssh_argv("me@box", ["env", "A=b", "printenv", "A"])[-1] == "env A=b printenv A"


def test_the_plan_carries_the_variable_for_both_graphs_and_only_when_given():
    epic = launch_plan(HOST, "init-x", "run-1", "lbl", chair_courier=COURIER)
    assert epic[-1] == ssh_argv("me@box", ["env", f"COX_CHAIR_COURIER={COURIER}", *EPIC_NOW])
    assert launch_plan(HOST, "init-x", "run-1", "lbl")[-1] == ssh_argv("me@box", EPIC_NOW)
    decompose = launch_plan(
        HOST, "init-x", "run-1", "lbl", graph="decompose", intake="work/intake/init-x.md", chair_courier=COURIER
    )
    assert decompose[-1] == ssh_argv("me@box", ["env", f"COX_CHAIR_COURIER={COURIER}", *DECOMPOSE_NOW])
    bare = launch_plan(HOST, "init-x", "run-1", "lbl", graph="decompose", intake="work/intake/init-x.md")
    assert bare[-1] == ssh_argv("me@box", DECOMPOSE_NOW)


def test_the_courier_is_read_from_the_courier_ssh_key_only():
    assert courier_ssh_of({"courier_ssh": COURIER}) == COURIER
    assert courier_ssh_of({}) is None
    assert courier_ssh_of(None) is None
    assert courier_ssh_of({"courier_ssh": ""}) is None
    assert courier_ssh_of({"courier_ssh": 7}) is None


def _launched(profile, graph="epic"):
    seen: list[list[str]] = []

    def run(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    extra = {"graph": "decompose", "intake": "work/intake/init-x.md"} if graph == "decompose" else {}
    result = launch_on_host(HOST, "init-x", "run-1", "lbl", "2026-11-06T00:00:00Z", run, profile=profile, **extra)
    assert isinstance(result, dict)
    return seen[-1]


def test_launch_on_host_passes_the_profile_key_to_the_launch_command():
    assert f"COX_CHAIR_COURIER={COURIER}" in shlex.join(_launched({"courier_ssh": COURIER}))
    assert f"COX_CHAIR_COURIER={COURIER}" in shlex.join(_launched({"courier_ssh": COURIER}, "decompose"))
    assert "COX_CHAIR_COURIER" not in shlex.join(_launched({"runner": "claude-code"}))
    assert _launched(None) == ssh_argv("me@box", EPIC_NOW)


def _cli_launch_ssh(tmp_path, monkeypatch, provider_lines):
    """The launch ssh argv that `cox route launch epic --on box` runs, with the provider file named by the profile
    holding `provider_lines`. cli.py reads that file as `provider`, the mapping launch_on_host receives as `profile`."""
    _, _, argv = _setup(tmp_path)
    provider = tmp_path / "provider.yaml"
    provider.write_text(f"command: claude\n{provider_lines}")
    profile = tmp_path / "profile.yaml"
    profile.write_text(profile.read_text().replace("/opt/providers/acme.yaml", str(provider)))
    calls: list[list[str]] = []
    _fake_edge(monkeypatch, calls)
    _no_local_process(monkeypatch)
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)
    monkeypatch.setattr(cli, "_host_auth_output", lambda argv: '{"loggedIn": true}')
    assert main([*argv, "--on", "box"]) == 0
    [launch] = [c for c in calls if c[0] == "ssh" and "route launch epic" in c[-1]]
    return launch


def test_route_launch_on_a_host_sets_the_variable_from_the_provider_profile_key(tmp_path, monkeypatch):
    launch = _cli_launch_ssh(tmp_path, monkeypatch, f"courier_ssh: {COURIER}\n")
    assert f"COX_CHAIR_COURIER={COURIER}" in launch[-1]


def test_route_launch_on_a_host_without_the_key_leaves_the_launch_unchanged(tmp_path, monkeypatch):
    launch = _cli_launch_ssh(tmp_path, monkeypatch, "")
    assert "COX_CHAIR_COURIER" not in launch[-1]
