import argparse
import socket

from lane_probe_replies import PASSING, is_lane_probe

from agent_tools import cli, lane_prereqs
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import doctor_argv, env_check_argv, ssh_argv, token_status_argv
from agent_tools.remote_doctor import doctor_on_host


LANE = lane_prereqs.probe_argv()


def _recorded_doctor(monkeypatch, tmp_path, host_name, local_name):
    argvs: list[list[str]] = []

    def run_ssh(argv):
        argvs.append(argv)
        return 0, PASSING if is_lane_probe(argv) else '{"loggedIn": true}'

    monkeypatch.setattr(socket, "gethostname", lambda: local_name)
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda a: ({}, tmp_path, None))
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: [])
    monkeypatch.setattr(cli, "_host_ssh", lambda a, profile, runs_dir: LaneHost(host_name, "operator@" + host_name, ""))
    monkeypatch.setattr(cli, "_run_ssh", run_ssh)
    monkeypatch.setattr(cli, "_host_repos", lambda profile: [])  # no repo checks, so no toolchain probe among the argvs
    code = cli._host_doctor(argparse.Namespace(name=host_name))
    return code, argvs


def test_host_doctor_runs_every_command_locally_on_the_loops_own_host(monkeypatch, tmp_path, capsys):
    code, argvs = _recorded_doctor(monkeypatch, tmp_path, "chair-host", "chair-host")
    assert code == 0
    assert argvs == [doctor_argv(), token_status_argv(probe=True), LANE]
    assert not any(argv[0] == "ssh" for argv in argvs)


def test_host_doctor_wraps_every_command_in_ssh_for_another_host(monkeypatch, tmp_path, capsys):
    code, argvs = _recorded_doctor(monkeypatch, tmp_path, "host-b", "chair-host")
    assert code == 0
    assert argvs == [
        ssh_argv("operator@host-b", doctor_argv()), ssh_argv("operator@host-b", token_status_argv(probe=True)),
        ssh_argv("operator@host-b", LANE),
    ]


def test_doctor_on_host_wraps_in_ssh_by_default():
    argvs: list[list[str]] = []

    def run(argv):
        argvs.append(argv)
        return 0, '{"loggedIn": true}'

    doctor_on_host(LaneHost("h", "operator@h", ""), run)
    assert argvs == [
        ssh_argv("operator@h", doctor_argv()), ssh_argv("operator@h", token_status_argv(probe=True)),
        ssh_argv("operator@h", LANE),
    ]


def test_doctor_on_host_wraps_the_env_check_in_ssh_by_default():
    argvs: list[list[str]] = []

    def run(argv):
        argvs.append(argv)
        return 0, "secret"

    doctor_on_host(LaneHost("h", "operator@h", ""), run, runner="codex", env_names=("MY_API_KEY",))
    assert argvs == [
        ssh_argv("operator@h", doctor_argv()), ssh_argv("operator@h", env_check_argv("MY_API_KEY")),
        ssh_argv("operator@h", LANE),
    ]
