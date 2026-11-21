"""C6: the queue workers' units and the steps that swap them in for the tick's unit (§5.4 step 3) and back (§5.5).
Pure text and plans; nothing is stubbed. The edge that runs them is covered with a recording runner below, in a
sandboxed home, and end to end in `smoke/test_chair_queue_units.py`."""

from __future__ import annotations

import argparse
import os
import plistlib
from pathlib import Path, PurePosixPath

import pytest

from agent_tools import chair_queue_units as units
from agent_tools import cli_chair_units
from agent_tools.chair_kinds import QUEUE_KINDS
from agent_tools.chair_service import CHAIR_PLIST_LABEL, UNIT_NAME

HOME = PurePosixPath("/home/user")
INPUTS = units.UnitInputs("/opt/cox/bin/cox", "/home/user/repos/workspace", "chair", "/home/user/.config/agent-tools/profile.yaml",
                          "/opt/cox/bin:/usr/bin", "/home/user/.config/agent-tools/garage.env")
ALL = frozenset(QUEUE_KINDS)


def _argvs(steps) -> list[str]:
    return [" ".join(s.argv) for s in steps]


def test_the_template_runs_the_instance_kind_as_a_worker_under_the_chairs_label_and_profile():
    text = units.template_text(INPUTS)
    assert "ExecStart=/opt/cox/bin/cox chair worker --kind %i --label chair --profile /home/user/.config/agent-tools/profile.yaml" in text
    assert "EnvironmentFile=-/home/user/.config/agent-tools/garage.env" in text
    assert "Environment=PATH=/opt/cox/bin:/usr/bin" in text and "Restart=always" in text
    assert "WantedBy=default.target" in text


def test_each_kind_has_its_own_launchd_agent_that_sources_the_env_file():
    for kind in QUEUE_KINDS:
        plist = plistlib.loads(units.plist_text(INPUTS, kind).encode())
        assert plist["Label"] == f"coxswain-chair-{kind}" and plist["KeepAlive"] is True and plist["RunAtLoad"] is True
        args = plist["ProgramArguments"]
        assert args[:2] == ["/bin/sh", "-c"] and INPUTS.env_file in args
        assert args[-9:] == ["/opt/cox/bin/cox", "chair", "worker", "--kind", kind, "--label", "chair", "--profile", INPUTS.profile]
        assert plist["EnvironmentVariables"] == {"PATH": INPUTS.path}


def test_the_files_per_platform():
    assert [p for p, _t in units.queue_files("linux", HOME, INPUTS)] == [HOME / ".config/systemd/user/coxswain-chair@.service"]
    assert [p.name for p, _t in units.queue_files("darwin", HOME, INPUTS)] == [f"coxswain-chair-{k}.plist" for k in QUEUE_KINDS]
    assert units.queue_files("win32", HOME, INPUTS) == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_install_refuses_unless_every_kind_is_listed(platform: str):
    assert units.install_refusal(platform, ALL) is None
    why = units.install_refusal(platform, ALL - {"launch"})
    assert why is not None and "launch" in why and "chair.queues" in why
    assert units.install_refusal("win32", ALL) is not None


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_rollback_refuses_while_any_kind_is_listed(platform: str):
    assert units.uninstall_refusal(platform, frozenset()) is None
    why = units.uninstall_refusal(platform, frozenset({"watch"}))
    assert why is not None and "watch" in why and "[]" in why


def test_linux_install_stops_the_tick_first_then_starts_the_five():
    steps = units.install_steps("linux", HOME, [units.template_path(HOME)], True, {}, 501)
    instances = " ".join(units.instance_names())
    assert _argvs(steps) == [
        f"systemctl --user disable --now {UNIT_NAME}",
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {instances}",
        f"systemctl --user restart {instances}",
    ]
    assert not any(s.may_fail for s in steps)


def test_linux_install_again_with_nothing_changed_restarts_nothing():
    steps = units.install_steps("linux", HOME, [], True, {}, 501)
    assert not any(" restart " in f" {a} " for a in _argvs(steps))


def test_linux_install_with_no_tick_unit_has_nothing_to_disable():
    steps = units.install_steps("linux", HOME, [], False, {}, 501)
    assert not any(UNIT_NAME in a for a in _argvs(steps))


def test_darwin_install_boots_the_tick_out_disables_it_and_boots_each_worker_in():
    changed = [units.plist_path(HOME, k) for k in QUEUE_KINDS]
    steps = _argvs(units.install_steps("darwin", HOME, changed, True, {CHAIR_PLIST_LABEL: True}, 501))
    assert steps[:2] == [f"launchctl bootout gui/501/{CHAIR_PLIST_LABEL}", f"launchctl disable gui/501/{CHAIR_PLIST_LABEL}"]
    for kind in QUEUE_KINDS:
        assert f"launchctl bootstrap gui/501 {units.plist_path(HOME, kind)}" in steps
        assert f"launchctl enable gui/501/coxswain-chair-{kind}" in steps


def test_darwin_install_again_leaves_loaded_unchanged_agents_alone_and_reloads_a_changed_one():
    loaded = {units.plist_label(k): True for k in QUEUE_KINDS}
    steps = _argvs(units.install_steps("darwin", HOME, [units.plist_path(HOME, "watch")], True, loaded, 501))
    assert [s for s in steps if "bootstrap" in s] == [f"launchctl bootstrap gui/501 {units.plist_path(HOME, 'watch')}"]
    assert [s for s in steps if "bootout" in s] == ["launchctl bootout gui/501/coxswain-chair-watch"]


def test_linux_rollback_stops_the_five_then_starts_the_tick():
    stop, start = units.uninstall_steps("linux", HOME, True, True, {}, 501)
    assert _argvs(stop) == [f"systemctl --user disable --now {' '.join(units.instance_names())}"]
    assert _argvs(start) == ["systemctl --user daemon-reload", f"systemctl --user enable --now {UNIT_NAME}"]


def test_linux_rollback_with_nothing_installed_may_find_nothing_to_stop():
    stop, _start = units.uninstall_steps("linux", HOME, False, True, {}, 501)
    assert all(s.may_fail for s in stop)


def test_darwin_rollback_boots_out_only_loaded_workers_and_brings_the_tick_back():
    loaded = {"coxswain-chair-watch": True}
    stop, start = units.uninstall_steps("darwin", HOME, True, True, loaded, 501)
    assert _argvs(stop) == ["launchctl bootout gui/501/coxswain-chair-watch"]
    assert _argvs(start) == [
        f"launchctl enable gui/501/{CHAIR_PLIST_LABEL}",
        f"launchctl bootstrap gui/501 {HOME / 'Library/LaunchAgents/coxswain-chair.plist'}",
    ]


def test_run_steps_stops_at_a_failure_and_prints_what_was_not_run():
    said: list[str] = []
    steps = [units.Step(("a",), may_fail=True), units.Step(("b",)), units.Step(("c",))]
    code = cli_chair_units.run_steps(steps, lambda argv: (0 if argv == ("c",) else 3, "no"), said.append)
    assert code == 1
    assert said == ["$ a", "  (exit 3, nothing to undo: no)", "$ b", "  exit 3: no", "c  # not run"]


# --- the edge in a sandboxed home, with a recording runner in place of systemctl and launchctl ---


class _Recorder:
    def __init__(self, loaded: set[str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded = loaded if loaded is not None else set()

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if argv[:2] == ("launchctl", "print"):
            return (0 if argv[2].rsplit("/", 1)[1] in self.loaded else 113), ""
        return 0, ""


def _sandbox(tmp_path: Path, monkeypatch, queues: str) -> argparse.Namespace:
    home = tmp_path / "home"
    workspace = home / "repos" / "workspace"
    workspace.mkdir(parents=True)
    profile = home / ".config" / "agent-tools" / "profile.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(f"workspace_dir: {workspace}\nchair:\n  queues: [{queues}]\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "cox").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "cox").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    return argparse.Namespace(install=True, uninstall=False, queues=True, dry_run=False, label="chair", profile=str(profile),
                              environment_file=None, host=None, status=False)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_install_twice_then_uninstall_in_a_sandboxed_home(tmp_path, monkeypatch, platform: str):
    a = _sandbox(tmp_path, monkeypatch, ", ".join(QUEUE_KINDS))
    monkeypatch.setattr(cli_chair_units.sys, "platform", platform)
    home = Path.home()
    tick = (home / ".config/systemd/user" / UNIT_NAME) if platform == "linux" else (home / "Library/LaunchAgents/coxswain-chair.plist")
    tick.parent.mkdir(parents=True, exist_ok=True)
    tick.write_text("the tick's unit\n", encoding="utf-8")
    first = _Recorder({CHAIR_PLIST_LABEL})
    assert cli_chair_units.chair_service_queues(a, first) == 0
    files = [p for p, _t in units.queue_files(platform, home, units.UnitInputs("", "", "", "", "", None))]
    assert all(p.is_file() and p.stat().st_mode & 0o777 == 0o600 for p in files)
    assert any(UNIT_NAME in " ".join(c) or CHAIR_PLIST_LABEL in " ".join(c) for c in first.calls)
    if platform == "linux":
        assert cli_chair_units.queue_unit_names(home) == units.instance_names()
    # Again: nothing rewritten, nothing restarted, no agent booted out.
    mtimes = [p.stat().st_mtime_ns for p in files]
    again = _Recorder({CHAIR_PLIST_LABEL, *(units.plist_label(k) for k in QUEUE_KINDS)})
    assert cli_chair_units.chair_service_queues(a, again) == 0
    assert [p.stat().st_mtime_ns for p in files] == mtimes
    assert not any(c[:3] == ("systemctl", "--user", "restart") or (c[:2] == ("launchctl", "bootstrap")) for c in again.calls)
    # The rollback refuses while chair.queues lists a kind, then runs once it lists none.
    a.install, a.uninstall = False, True
    assert cli_chair_units.chair_service_queues(a, _Recorder()) == 2
    assert all(p.is_file() for p in files)
    Path(a.profile).write_text(f"workspace_dir: {home / 'repos' / 'workspace'}\nchair:\n  queues: []\n", encoding="utf-8")
    back = _Recorder({units.plist_label(k) for k in QUEUE_KINDS})
    assert cli_chair_units.chair_service_queues(a, back) == 0
    assert not any(p.exists() for p in files) and tick.is_file()
    joined = [" ".join(c) for c in back.calls]
    if platform == "linux":
        assert joined[-1] == f"systemctl --user enable --now {UNIT_NAME}"
        assert cli_chair_units.queue_unit_names(home) == []
    else:
        assert joined[-1] == f"launchctl bootstrap gui/{os.getuid()} {tick}"


def test_install_refuses_before_writing_when_a_kind_is_not_listed(tmp_path, monkeypatch, capsys):
    a = _sandbox(tmp_path, monkeypatch, "housekeeping, watch")
    monkeypatch.setattr(cli_chair_units.sys, "platform", "linux")
    run = _Recorder()
    assert cli_chair_units.chair_service_queues(a, run) == 2
    assert run.calls == [] and not units.template_path(Path.home()).exists()
    assert "quarantine, decompose, launch" in capsys.readouterr().out


def test_a_dry_run_writes_and_runs_nothing(tmp_path, monkeypatch, capsys):
    a = _sandbox(tmp_path, monkeypatch, ", ".join(QUEUE_KINDS))
    a.dry_run = True
    monkeypatch.setattr(cli_chair_units.sys, "platform", "linux")
    run = _Recorder()
    assert cli_chair_units.chair_service_queues(a, run) == 0
    assert run.calls == [] and not units.template_path(Path.home()).exists()
    out = capsys.readouterr().out
    assert "would write" in out and "would run: systemctl --user enable --now coxswain-chair@housekeeping.service" in out
