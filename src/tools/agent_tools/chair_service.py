"""The systemd user unit that keeps `cox chair run` alive. Pure text; the edge in cli.py does the file and PATH work."""

from __future__ import annotations

import posixpath
import re
from itertools import pairwise
from pathlib import PurePath
from xml.sax.saxutils import escape

from agent_tools.chair_restart import RESTART_EXIT_CODE

# cli.py and setup_install.py still call these through chair_service; the rules now live in unit_env.
from agent_tools.unit_env import (
    environment_file_line,
    launchd_arguments,
)
from agent_tools.unit_env import export_lines as export_lines
from agent_tools.unit_env import unit_environment_file as unit_environment_file
from agent_tools.unit_env import unsafe_values as unsafe_values

UNIT_NAME = "coxswain-chair.service"
WATCHDOG_SERVICE_NAME = "coxswain-chair-watchdog.service"
WATCHDOG_TIMER_NAME = "coxswain-chair-watchdog.timer"
CHAIR_PLIST_LABEL = "coxswain-chair"
CHAIR_PLIST_NAME = "coxswain-chair.plist"
WATCHDOG_PLIST_LABEL = "coxswain-chair-watchdog"
WATCHDOG_PLIST_NAME = "coxswain-chair-watchdog.plist"

_EXEC_START = re.compile(r"^ExecStart=(.*)$", re.MULTILINE)


def unit_path[Home: PurePath](home: Home) -> Home:  # a Path in, a Path out: the edge calls .exists on the result
    return home / ".config/systemd/user" / UNIT_NAME


def systemctl_lines() -> list[str]:
    return [
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {UNIT_NAME}",
        f"systemctl --user status {UNIT_NAME}",
    ]


def path_value(cox: str, tools_bin: str, graphs_bin: str, default_path: str) -> str:
    """The unit's PATH: cox's directory, then the two venv bins, then the default; empties dropped, first duplicate kept."""
    entries = [posixpath.dirname(cox), tools_bin, graphs_bin, *default_path.split(":")]
    return ":".join(dict.fromkeys(entry for entry in entries if entry))


def unit_text(
    cox: str, workspace_dir: str, label: str, interval: int, environment_file: str | None, profile: str | None = None,
    path: str | None = None,
) -> str:
    """`EnvironmentFile=-` keeps a missing file from failing the unit; `path` becomes the one `Environment=PATH=` line."""
    env_lines = [environment_file_line(environment_file)] if environment_file is not None else []
    path_lines = [f"Environment=PATH={path}"] if path is not None else []
    profile_args = f" --profile {profile}" if profile is not None else ""
    lines = [
        "[Unit]",
        "Description=Coxswain autonomous chair",
        "",
        "[Service]",
        f"ExecStart={cox} chair run --label {label} --interval {interval}{profile_args}",
        f"WorkingDirectory={workspace_dir}",
        *path_lines,
        *env_lines,
        "Restart=on-failure",
        f"RestartForceExitStatus={RESTART_EXIT_CODE}",
        "RestartSec=30",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


def watchdog_service_text(cox: str, profile: str, path: str, environment_file: str | None) -> str:
    """`EnvironmentFile=-` is written only when a file is given; the oneshot is fired by the timer."""
    env_lines = [environment_file_line(environment_file)] if environment_file is not None else []
    lines = [
        "[Unit]",
        "Description=Coxswain chair watchdog",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={cox} chair watchdog --profile {profile}",
        f"Environment=PATH={path}",
        *env_lines,
    ]
    return "\n".join(lines) + "\n"


def watchdog_timer_text(minutes: int) -> str:
    lines = [
        "[Unit]",
        "Description=Run the Coxswain chair watchdog on a schedule",
        "",
        "[Timer]",
        f"OnBootSec={minutes}min",
        f"OnUnitActiveSec={minutes}min",
        f"Unit={WATCHDOG_SERVICE_NAME}",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]
    return "\n".join(lines) + "\n"


def environment_variable_lines(path: str) -> list[str]:
    """The EnvironmentVariables dict: PATH only. The env file's values are never copied into a plist; the agent
    sources the file at launch (`program_arguments`)."""
    return ["  <key>EnvironmentVariables</key>", "  <dict>", "    <key>PATH</key>", f"    <string>{escape(path)}</string>",
            "  </dict>"]


def program_arguments(environment_file: str | None, argv: list[str]) -> list[str]:
    """`argv`, run through `unit_env.launchd_arguments` when an env file is given."""
    return argv if environment_file is None else launchd_arguments(environment_file, argv)


def watchdog_plist_text(cox: str, profile: str, path: str, minutes: int, environment_file: str | None = None) -> str:
    """StartInterval is in seconds; every interpolated value is XML-escaped; the agent sources `environment_file`."""
    arguments = program_arguments(environment_file, [cox, "chair", "watchdog", "--profile", profile])
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key>",
        f"  <string>{escape(WATCHDOG_PLIST_LABEL)}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
        *(f"    <string>{escape(argument)}</string>" for argument in arguments),
        "  </array>",
        "  <key>StartInterval</key>",
        f"  <integer>{minutes * 60}</integer>",
        *environment_variable_lines(path),
        "</dict>",
        "</plist>",
    ]
    return "\n".join(lines) + "\n"


def chair_plist_path[Home: PurePath](home: Home) -> Home:
    return home / "Library/LaunchAgents" / CHAIR_PLIST_NAME


def service_file[Home: PurePath](platform: str, home: Home) -> Home | None:
    """Where `setup install` puts the chair service file; None for a platform with no service manager supported."""
    if platform == "linux":
        return unit_path(home)
    if platform == "darwin":
        return chair_plist_path(home)
    return None


def chair_plist_text(
    cox: str, workspace_dir: str, label: str, interval: int, path: str, profile: str,
    environment_file: str | None = None,
) -> str:
    """KeepAlive true restarts the chair on any exit, including RESTART_EXIT_CODE; every interpolated value is XML-escaped.
    The agent sources `environment_file` at each launch."""
    arguments = program_arguments(
        environment_file, [cox, "chair", "run", "--label", label, "--interval", str(interval), "--profile", profile])
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key>",
        f"  <string>{escape(CHAIR_PLIST_LABEL)}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
        *(f"    <string>{escape(argument)}</string>" for argument in arguments),
        "  </array>",
        "  <key>RunAtLoad</key>",
        "  <true/>",
        "  <key>KeepAlive</key>",
        "  <true/>",
        "  <key>WorkingDirectory</key>",
        f"  <string>{escape(workspace_dir)}</string>",
        *environment_variable_lines(path),
        "</dict>",
        "</plist>",
    ]
    return "\n".join(lines) + "\n"


def _flag_value(tokens: list[str], flag: str) -> str | None:
    return next((value for name, value in pairwise(tokens) if name == flag), None)


def unit_args(text: str) -> dict[str, str | None] | None:
    """Reads label, interval, profile and env_file back out of a unit; an absent flag or line is None, interval stays a string."""
    commands = [match.group(1).split() for match in _EXEC_START.finditer(text)]
    tokens = next((t for t in commands if ("chair", "run") in pairwise(t)), None)
    if tokens is None:
        return None
    env_file = unit_environment_file(text)
    return {
        "label": _flag_value(tokens, "--label"),
        "interval": _flag_value(tokens, "--interval"),
        "profile": _flag_value(tokens, "--profile"),
        "env_file": env_file,
    }
