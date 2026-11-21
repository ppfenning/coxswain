"""The service units of the queue workers (C6 of the event-driven chair): one per kind, and the steps that swap them in
for the tick's unit and back.

Pure text and pure plans; the edge (`cli_chair_units`) writes the files, reads what is loaded, and runs the steps.

- **Linux.** One systemd template, `coxswain-chair@.service`, whose instance name is the kind: `coxswain-chair@launch
  .service` runs `cox chair worker --kind launch`. Installing enables and starts the five instances and disables the
  tick's `coxswain-chair.service`; uninstalling (the rollback) disables and stops them, removes the template, and
  enables and starts the tick's unit again.
- **macOS.** One launchd agent per kind, `coxswain-chair-<kind>.plist` beside the tick's `coxswain-chair.plist`, in the
  same shape. Installing boots each in (a changed file is booted out first) and boots out and disables the tick's agent,
  so it does not come back at the next login; uninstalling reverses each of those.

Every plan is idempotent: run twice, the second run changes nothing it does not have to. A file is written only when
its text changed, a unit already running is restarted only when its file changed, and a step that may find nothing to
undo (booting out an agent that is not loaded) is marked so a failure there does not stop the plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from xml.sax.saxutils import escape

from agent_tools.chair_kinds import QUEUE_KINDS
from agent_tools.chair_service import (
    CHAIR_PLIST_LABEL,
    UNIT_NAME,
    WATCHDOG_PLIST_NAME,
    WATCHDOG_TIMER_NAME,
    chair_plist_path,
    environment_variable_lines,
    program_arguments,
)
from agent_tools.unit_env import environment_file_line

__all__ = [
    "PLIST_LABEL_PREFIX",
    "TEMPLATE_NAME",
    "Step",
    "UnitInputs",
    "install_refusal",
    "install_steps",
    "instance_name",
    "instance_names",
    "plist_label",
    "plist_path",
    "plist_text",
    "queue_files",
    "restore_steps",
    "template_path",
    "template_text",
    "uninstall_refusal",
    "uninstall_steps",
    "watchdog_refusal",
]

TEMPLATE_NAME = "coxswain-chair@.service"
PLIST_LABEL_PREFIX = "coxswain-chair-"


@dataclass(frozen=True)
class UnitInputs:
    """The absolute values a queue unit is built from, resolved by the edge as the tick's unit's are."""

    cox: str
    workspace_dir: str
    label: str
    profile: str
    path: str
    env_file: str | None


@dataclass(frozen=True)
class Step:
    """One command of a plan. `may_fail` marks a step that undoes something that may not be there."""

    argv: tuple[str, ...]
    may_fail: bool = False


def instance_name(kind: str) -> str:
    """The systemd instance of `kind`: `coxswain-chair@housekeeping.service`."""
    return f"coxswain-chair@{kind}.service"


def instance_names() -> list[str]:
    """Every kind's instance, in `QUEUE_KINDS` order."""
    return [instance_name(kind) for kind in QUEUE_KINDS]


def template_path[Home: PurePath](home: Home) -> Home:
    return home / ".config/systemd/user" / TEMPLATE_NAME


def plist_label(kind: str) -> str:
    return f"{PLIST_LABEL_PREFIX}{kind}"


def plist_path[Home: PurePath](home: Home, kind: str) -> Home:
    return home / "Library/LaunchAgents" / f"{plist_label(kind)}.plist"


def _worker_argv(inputs: UnitInputs, kind: str) -> list[str]:
    return [inputs.cox, "chair", "worker", "--kind", kind, "--label", inputs.label, "--profile", inputs.profile]


def template_text(inputs: UnitInputs) -> str:
    """The template every kind's instance runs; `%i` is the instance name, the kind. A worker exits only when stopped
    or on an error, so systemd restarts it on any exit it did not ask for."""
    env_lines = [environment_file_line(inputs.env_file)] if inputs.env_file is not None else []
    lines = [
        "[Unit]",
        "Description=Coxswain chair queue worker (%i)",
        "",
        "[Service]",
        f"ExecStart={' '.join(_worker_argv(inputs, '%i'))}",
        f"WorkingDirectory={inputs.workspace_dir}",
        f"Environment=PATH={inputs.path}",
        *env_lines,
        "Restart=always",
        "RestartSec=30",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


def plist_text(inputs: UnitInputs, kind: str) -> str:
    """One kind's launchd agent, in the tick agent's shape (`chair_service.chair_plist_text`): KeepAlive restarts it on
    any exit, every interpolated value is XML-escaped, and the agent sources the env file at each launch."""
    arguments = program_arguments(inputs.env_file, _worker_argv(inputs, kind))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key>",
        f"  <string>{escape(plist_label(kind))}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
        *(f"    <string>{escape(argument)}</string>" for argument in arguments),
        "  </array>",
        "  <key>RunAtLoad</key>",
        "  <true/>",
        "  <key>KeepAlive</key>",
        "  <true/>",
        "  <key>WorkingDirectory</key>",
        f"  <string>{escape(inputs.workspace_dir)}</string>",
        *environment_variable_lines(inputs.path),
        "</dict>",
        "</plist>",
    ]
    return "\n".join(lines) + "\n"


def queue_files[Home: PurePath](platform: str, home: Home, inputs: UnitInputs) -> list[tuple[Home, str]]:
    """The files an install writes on `platform`, with their text; none on a platform with no service manager."""
    if platform == "linux":
        return [(template_path(home), template_text(inputs))]
    if platform == "darwin":
        return [(plist_path(home, kind), plist_text(inputs, kind)) for kind in QUEUE_KINDS]
    return []


def install_refusal(platform: str, queued: frozenset[str]) -> str | None:
    """Why the queue units may not be installed now, or None. The tick's unit is disabled by the install, so every
    kind must already be listed in `chair.queues`: a kind left off would be planned by no one."""
    if platform not in ("linux", "darwin"):
        return f"the queue units are a systemd template or launchd agents, and {platform} has neither"
    missing = [kind for kind in QUEUE_KINDS if kind not in queued]
    if missing:
        return (f"chair.queues does not list {', '.join(missing)}; with the tick's unit disabled nothing would plan "
                f"{'it' if len(missing) == 1 else 'them'}. Set chair.queues to [{', '.join(QUEUE_KINDS)}] first")
    return None


def watchdog_refusal(platform: str, installed: bool, active: bool) -> str | None:
    """Why the queue units may not be installed while the chair watchdog timer is not running, or None. The install
    disables the tick's unit, and with every worker down the timer (`cox chair watchdog`, a new process every few
    minutes) is the one alarm left: the workers watch each other, but a dead machine or a crash of all five says nothing
    unless something outside them reads the leases. `installed` is whether its files exist; `active` whether the
    service manager runs it (systemd: the timer is active; launchd: the agent is loaded)."""
    if installed and active:
        return None
    if platform == "darwin":
        start = f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{WATCHDOG_PLIST_NAME}"
        state = "not loaded" if installed else "not installed"
    else:
        start = f"systemctl --user daemon-reload && systemctl --user enable --now {WATCHDOG_TIMER_NAME}"
        state = "not active" if installed else "not installed"
    first = "" if installed else "`cox chair watchdog --install`, then "
    return (f"the chair watchdog timer is {state}; once the tick's unit is disabled it is the only alarm that fires "
            f"with every worker down. Run {first}`{start}` first")


def restore_steps(platform: str, home: PurePath, tick_unit: bool, uid: int) -> list[Step]:
    """The steps that put the tick's unit back after an install that failed partway: re-enable it and start it, so a
    failed install never leaves nothing running. The workers that did start are left running: the tick skips the
    kinds they hold, and while it holds the chair lease it raises `queue_unowned` for each one that did not start.
    With no tick unit file there is nothing to put back."""
    if not tick_unit:
        return []
    if platform == "linux":
        return [_systemctl("enable", "--now", UNIT_NAME)]
    if platform == "darwin":
        domain = f"gui/{uid}"
        # The bootstrap may find it loaded (the bootout failed before anything else ran): nothing to undo then.
        return [_launchctl("enable", f"{domain}/{CHAIR_PLIST_LABEL}"),
                _launchctl("bootstrap", domain, str(chair_plist_path(home)), may_fail=True)]
    return []


def uninstall_refusal(platform: str, queued: frozenset[str]) -> str | None:
    """Why the rollback may not run now, or None. The tick skips every kind `chair.queues` lists whatever the leases
    say, so the list must be empty before the tick's unit comes back (§5.5: step 2 before step 3)."""
    if platform not in ("linux", "darwin"):
        return f"the queue units are a systemd template or launchd agents, and {platform} has neither"
    if queued:
        return (f"chair.queues still lists {', '.join(sorted(queued))}; the tick would skip "
                f"{'it' if len(queued) == 1 else 'them'}. Set chair.queues to [] first")
    return None


def _systemctl(*args: str, may_fail: bool = False) -> Step:
    return Step(("systemctl", "--user", *args), may_fail)


def _launchctl(*args: str, may_fail: bool = False) -> Step:
    return Step(("launchctl", *args), may_fail)


def install_steps(
    platform: str, home: PurePath, changed: Sequence[PurePath], tick_unit: bool, loaded: Mapping[str, bool], uid: int,
) -> list[Step]:
    """The commands that start the five workers and stop the tick, after the files in `changed` were written.

    `tick_unit` is whether the tick's unit file exists (there is nothing to disable otherwise); `loaded` says, on macOS,
    which agent labels launchd has loaded now. The tick goes first, so the two never plan side by side for longer than
    the install takes (and they would not double up if they did: the tick skips every listed kind)."""
    if platform == "linux":
        instances = instance_names()
        tick = [_systemctl("disable", "--now", UNIT_NAME)] if tick_unit else []
        restart = [_systemctl("restart", *instances)] if template_path(home) in changed else []
        # One start per instance, so a failure names its kind and the ones before it are known to be up.
        starts = [_systemctl("enable", "--now", instance) for instance in instances]
        return [*tick, _systemctl("daemon-reload"), *starts, *restart]
    if platform == "darwin":
        domain = f"gui/{uid}"
        # Booted out now, and disabled so the tick's agent (RunAtLoad) does not come back at the next login.
        steps = [_launchctl("bootout", f"{domain}/{CHAIR_PLIST_LABEL}")] if loaded.get(CHAIR_PLIST_LABEL, False) else []
        if tick_unit or loaded.get(CHAIR_PLIST_LABEL, False):
            steps.append(_launchctl("disable", f"{domain}/{CHAIR_PLIST_LABEL}"))
        for kind in QUEUE_KINDS:
            label, path = plist_label(kind), plist_path(home, kind)
            steps.append(_launchctl("enable", f"{domain}/{label}"))
            if loaded.get(label, False) and path not in changed:
                continue  # running from the same file: nothing to do
            if loaded.get(label, False):
                steps.append(_launchctl("bootout", f"{domain}/{label}"))
            steps.append(_launchctl("bootstrap", domain, str(path)))
        return steps
    return []


def uninstall_steps(
    platform: str, home: PurePath, installed: bool, tick_unit: bool, loaded: Mapping[str, bool], uid: int,
) -> tuple[list[Step], list[Step]]:
    """The rollback's commands (§5.5 steps 1 and 3), as the steps before the queue files are removed and the steps
    after: stop and disable the five workers, then enable and start the tick's unit. `installed` is whether any queue
    file was there; `tick_unit` whether the tick's unit file is (with none, there is nothing to start)."""
    if platform == "linux":
        stop = [_systemctl("disable", "--now", *instance_names(), may_fail=not installed)]
        tick = [_systemctl("enable", "--now", UNIT_NAME)] if tick_unit else []
        return stop, [_systemctl("daemon-reload"), *tick]
    if platform == "darwin":
        domain = f"gui/{uid}"
        stop = [_launchctl("bootout", f"{domain}/{plist_label(kind)}") for kind in QUEUE_KINDS
                if loaded.get(plist_label(kind), False)]
        start = [_launchctl("enable", f"{domain}/{CHAIR_PLIST_LABEL}")]
        if tick_unit and not loaded.get(CHAIR_PLIST_LABEL, False):
            start.append(_launchctl("bootstrap", domain, str(chair_plist_path(home))))
        return stop, start
    return [], []
