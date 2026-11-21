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
from agent_tools.chair_service import CHAIR_PLIST_LABEL, UNIT_NAME, WATCHDOG_PLIST_LABEL, WATCHDOG_TIMER_NAME
from agent_tools.cli_chair_service import _watchdog_paths

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


def test_the_template_stops_only_the_worker_so_a_stop_or_restart_leaves_the_watch_workers_lands_running():
    service = units.template_text(INPUTS).split("[Service]\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert service.count("KillMode=process") == 1
    assert not any(line.startswith("KillMode=") and line != "KillMode=process" for line in service)


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
        *(f"systemctl --user enable --now {instance}" for instance in units.instance_names()),
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
    """A recording systemctl and launchctl. `loaded` are the launchd labels loaded (the watchdog's too, unless
    `watchdog` is off); `watchdog` is whether systemd says the watchdog timer is active; `fail_start` fails that start
    (1-based, counting `systemctl enable --now` and `launchctl bootstrap` calls)."""

    def __init__(self, loaded: set[str] | None = None, *, watchdog: bool = True, fail_start: int | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded = (loaded if loaded is not None else set()) | ({WATCHDOG_PLIST_LABEL} if watchdog else set())
        self.watchdog, self.fail_start, self.starts = watchdog, fail_start, 0

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[:2] == ("launchctl", "print"):
            return (0 if argv[2].rsplit("/", 1)[1] in self.loaded else 113), ""
        if argv == ("systemctl", "--user", "is-active", WATCHDOG_TIMER_NAME):
            return (0, "active") if self.watchdog else (3, "inactive")
        if argv[:4] == ("systemctl", "--user", "enable", "--now") or argv[:2] == ("launchctl", "bootstrap"):
            self.starts += 1
            if self.starts == self.fail_start:
                return 1, "Failed to start: unit failed"
        return 0, ""


def _watchdog_files(platform: str) -> None:
    """The watchdog timer's files, as `cox chair watchdog --install` leaves them."""
    for path in _watchdog_paths(platform, Path.home()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("the watchdog\n", encoding="utf-8")


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
    _watchdog_files(platform)
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
    _watchdog_files("linux")
    run = _Recorder()
    assert cli_chair_units.chair_service_queues(a, run) == 0
    # The one call is the read-only question whether the watchdog timer runs.
    assert run.calls == [("systemctl", "--user", "is-active", WATCHDOG_TIMER_NAME)]
    assert not units.template_path(Path.home()).exists()
    out = capsys.readouterr().out
    assert "would write" in out and "would run: systemctl --user enable --now coxswain-chair@housekeeping.service" in out


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("files", [False, True], ids=["not installed", "installed but not running"])
def test_install_refuses_before_writing_unless_the_watchdog_timer_runs(tmp_path, monkeypatch, capsys, platform: str, files: bool):
    """C6: with the tick's unit disabled the watchdog timer is the only alarm when every worker is down."""
    a = _sandbox(tmp_path, monkeypatch, ", ".join(QUEUE_KINDS))
    monkeypatch.setattr(cli_chair_units.sys, "platform", platform)
    if files:
        _watchdog_files(platform)
    run = _Recorder(watchdog=False)
    assert cli_chair_units.chair_service_queues(a, run) == 2
    out = capsys.readouterr().out
    assert "refusing, nothing written: the chair watchdog timer is" in out
    assert ("`cox chair watchdog --install`" in out) is not files
    assert not any(p.exists() for p, _t in units.queue_files(platform, Path.home(), INPUTS))
    assert all(c[:3] == ("systemctl", "--user", "is-active") or c[:2] == ("launchctl", "print") for c in run.calls), run.calls


def test_the_watchdog_refusal_names_what_is_missing_and_how_to_start_it():
    assert units.watchdog_refusal("linux", True, True) is None
    linux = units.watchdog_refusal("linux", True, False) or ""
    assert "not active" in linux and f"enable --now {WATCHDOG_TIMER_NAME}" in linux and "--install" not in linux
    darwin = units.watchdog_refusal("darwin", False, False) or ""
    assert "not installed" in darwin and "cox chair watchdog --install" in darwin and "launchctl bootstrap" in darwin


def test_the_restore_re_enables_and_starts_the_ticks_unit_and_needs_one():
    assert _argvs(units.restore_steps("linux", HOME, True, 501)) == [f"systemctl --user enable --now {UNIT_NAME}"]
    darwin = units.restore_steps("darwin", HOME, True, 501)
    assert _argvs(darwin) == [f"launchctl enable gui/501/{CHAIR_PLIST_LABEL}",
                              f"launchctl bootstrap gui/501 {units.chair_plist_path(HOME)}"]
    assert darwin[-1].may_fail
    assert units.restore_steps("linux", HOME, False, 501) == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_an_install_whose_third_start_fails_puts_the_tick_back(tmp_path, monkeypatch, capsys, platform: str):
    """C6: a failed install never leaves nothing running. The tick's unit was stopped first; the third worker's start
    fails, so the tick's unit is enabled and started again, and the command exits 1."""
    a = _sandbox(tmp_path, monkeypatch, ", ".join(QUEUE_KINDS))
    monkeypatch.setattr(cli_chair_units.sys, "platform", platform)
    home = Path.home()
    tick = (home / ".config/systemd/user" / UNIT_NAME) if platform == "linux" else (home / "Library/LaunchAgents/coxswain-chair.plist")
    tick.parent.mkdir(parents=True, exist_ok=True)
    tick.write_text("the tick's unit\n", encoding="utf-8")
    _watchdog_files(platform)
    run = _Recorder({CHAIR_PLIST_LABEL}, fail_start=3)
    assert cli_chair_units.chair_service_queues(a, run) == 1
    joined = [" ".join(c) for c in run.calls]
    third = f"coxswain-chair@{QUEUE_KINDS[2]}.service" if platform == "linux" else f"coxswain-chair-{QUEUE_KINDS[2]}.plist"
    failed = next(i for i, c in enumerate(joined) if third in c and (" enable --now " in c or " bootstrap " in c))
    after = joined[failed + 1:]
    if platform == "linux":
        assert after == [f"systemctl --user enable --now {UNIT_NAME}"]
    else:
        assert after == [f"launchctl enable gui/{os.getuid()}/{CHAIR_PLIST_LABEL}", f"launchctl bootstrap gui/{os.getuid()} {tick}"]
    out = capsys.readouterr().out
    assert "failed partway; putting the tick's unit back" in out and "the tick's unit is back" in out
