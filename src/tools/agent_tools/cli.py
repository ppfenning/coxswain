"""cox — the coxswain's operator tools (alias: agent-tools, removed next release)."""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import importlib.metadata
import importlib.util
import sys
import types
from collections.abc import Sequence
from typing import Any

from agent_tools import commands
from core import module_facade


# Importing `agent_tools.cli` does not import the command modules: a name like `chair` below is a
# `_LazyModule`, not the module. Writes reach the real module, so `monkeypatch.setattr(cli.chair, ...)` holds.
class _LazyModule:
    """A module imported with importlib on its first attribute read; writes and deletes go to the real module."""

    def __init__(self, path: str) -> None:
        object.__setattr__(self, "_lazy_path", path)
        _LAZY_MODULES.append(self)

    def _lazy_load(self) -> types.ModuleType:
        return importlib.import_module(self._lazy_path)

    def __getattr__(self, name: str):
        return getattr(self._lazy_load(), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._lazy_load(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._lazy_load(), name)

    def __repr__(self) -> str:
        return f"<lazy module {self._lazy_path}>"


_LAZY_MODULES: list[_LazyModule] = []


@dataclasses.dataclass(frozen=True)
class _LazyHandler:
    """A `"dotted.module:function"` table entry; calling it imports the module with importlib, not before."""

    target: str

    def __call__(self, *args, **kwargs):
        module_path, _, func = self.target.partition(":")
        return getattr(importlib.import_module(module_path), func)(*args, **kwargs)


# H9 (docs/hardening.md): the handlers live in the part modules below, one command group or stage each. This module
# keeps the command table, the parser and `main`, and answers for every name the parts define (core.module_facade):
# the first read of one imports the parts, so `cox --help` imports none of them, and a write (a test's monkeypatch)
# reaches every part that holds the name.
_PARTS = (
    "agent_tools.cli_lazy",
    "agent_tools.cli_common",
    "agent_tools.cli_lake",
    "agent_tools.cli_usage",
    "agent_tools.cli_stats",
    "agent_tools.cli_runs_fetch",
    "agent_tools.cli_route_sync",
    "agent_tools.cli_land_steps",
    "agent_tools.cli_land_facts",
    "agent_tools.cli_runs",
    "agent_tools.cli_land",
    "agent_tools.cli_board",
    "agent_tools.cli_route",
    "agent_tools.cli_route_launch",
    "agent_tools.cli_courier",
    "agent_tools.cli_leader",
    "agent_tools.cli_launcher",
    "agent_tools.cli_views",
    "agent_tools.cli_host",
    "agent_tools.cli_chair",
    "agent_tools.cli_chair_service",
    "agent_tools.cli_install",
    "agent_tools.cli_setup_store",
    "agent_tools.cli_doctor",
    "agent_tools.cli_migrate",
)


def __getattr__(name: str) -> Any:
    return module_facade.attribute(__name__, name)


# Names the code kept here calls, defined in a part: each imports its part on the first call.
_launcher = _LazyHandler("agent_tools.cli_launcher:_launcher")
_refresh_board_files = _LazyHandler("agent_tools.cli_common:_refresh_board_files")
_release = _LazyHandler("agent_tools.cli_install:_release")
_route_status = _LazyHandler("agent_tools.cli_route:_route_status")
_setup_tui = _LazyHandler("agent_tools.cli_views:_setup_tui")
_store_refusal = _LazyHandler("agent_tools.cli_common:_store_refusal")
reads_board = _LazyHandler("agent_tools.cli_common:reads_board")


# Argument defaults the parser needs at build time, copied so `--help` imports no command module.
# tests/test_cli_lazy_dispatch.py pins each to the module constant it copies.
_STATS_CAUSES = ("ticket", "code", "harness", "unknown")  # stats_chair.CAUSES
_GATES_MIN_SAMPLE = 50  # stats_gates.MIN_SAMPLE
_CHAIR_RUN_INTERVAL = 60.0  # chair_run.DEFAULT_INTERVAL


# Commands in a board group that read only the files they are pointed at, so a machine with no store can run them.
_STORE_FREE_COMMANDS = frozenset({("route", "lint")})


def _leader_takeover_hours() -> float:
    """`policy.leader.takeover_hours`, default 3; answers the default for the same reason `_leader_heartbeat_minutes` does."""
    return 3.0


def _bare_group(parser: argparse.ArgumentParser):
    """Default `fn` for a group parser whose subcommand is optional: an
    operator who runs the group alone sees that group's own help and a exit
    code of 2, not a traceback or silence."""
    def _fn(_a: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _fn


RUNS_GROUP = commands.Group(
    name="runs", help="what a harness run recorded, and cleaning up after it",
    description="What a harness run recorded, and cleaning up after it.",
    epilog="examples:\n  cox runs fetch <run>\n  cox runs land <run> --repo PATH --apply\n  cox runs recover <run> <task> --repo PATH\n"
           "  cox runs usage <run> --json\n  cox runs detail <run> --json",
)
RUNS_COMMANDS = [
    commands.Command(
        "usage", "runs", "usage stats and cost for one run",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_usage", False, (),
    ),
    commands.Command(
        "trace", "runs", "the tool-call trace for one run",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--role",)), commands.Arg(("-v", "--verbose"), {"action": "store_true"})),
        "agent_tools.cli:_runs_trace", False, (),
    ),
    commands.Command(
        "clean", "runs", "delete a run's worktree and branches locally",
        (
            commands.Arg(("run_id",)), commands.Arg(("--repo",), {"required": True}), commands.Arg(("--worktree-root",), {"default": "~/worktrees"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--apply",), {"action": "store_true"}), commands.Arg(("--force",), {"action": "store_true", "help": "delete every branch regardless of whether its task is on main"}),
        ),
        "agent_tools.cli:_runs_clean", False, (),
    ),
    commands.Command(
        "land", "runs", "merge a run's branch into the target repo",
        (
            commands.Arg(("run_id",)), commands.Arg(("--repo",), {"required": True}), commands.Arg(("--task",)),
            commands.Arg(("--phase",), {"help": "land the whole phase off its own epic branch instead of one task"}), commands.Arg(("--label",)),
            commands.Arg(("--force",), {"action": "store_true", "help": "land despite a foreign live leader"}),
            commands.Arg(("--no-claim",), {"action": "store_true", "help": "land without taking an unheld or stale loop"}),
            commands.Arg(("--behind-limit",), {"type": int, "default": 20, "help": "warn when the task's base is more than this many commits behind the base branch"}),
            commands.Arg(("--base",), {"help": "branch to land onto; default: the repo's base: setting, then origin/HEAD, then main"}),
            commands.Arg(("--worktree-root",), {"default": "~/worktrees"}), commands.Arg(("--apply",), {"action": "store_true"}), commands.Arg(("--no-merge",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--gate",), {"choices": ["ticket", "phase", "epic", "full"], "help": "override the resolved gate level for this invocation"}),
            commands.Arg(("--wait-lock",), {"action": "store_true", "help": "when another land holds the repo lease, wait for it instead of refusing"}),
            commands.Arg(("--wait-lock-timeout",), {"type": int, "default": 600, "help": "seconds --wait-lock waits before refusing (default 600)"}),
        ),
        "agent_tools.cli:_runs_land", False, (),
    ),
    commands.Command(
        "land-batch", "runs", "land disjoint approved phases together as one pull request (the merge queue)",
        (
            commands.Arg(("--repo",), {"required": True}), commands.Arg(("--label",)),
            commands.Arg(("--source",), {"action": "append", "help": "initiative/phase/run to queue, repeatable; default: every ready phase"}),
            commands.Arg(("--batch-size",), {"type": int, "help": "sources per batch; default: the profile's merge_queue.batch_size"}),
            commands.Arg(("--wait-s",), {"type": float, "help": "seconds to wait for a full batch; default: the profile's merge_queue.wait_s"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "land despite a foreign live leader"}),
            commands.Arg(("--no-claim",), {"action": "store_true", "help": "land without taking an unheld or stale loop"}),
            commands.Arg(("--base",), {"help": "branch to land onto; default: the repo's base: setting, then origin/HEAD, then main"}),
            commands.Arg(("--worktree-root",), {"default": "~/worktrees"}), commands.Arg(("--apply",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--gate",), {"choices": ["ticket", "phase", "epic", "full"], "help": "override the resolved gate level for this invocation"}),
        ),
        "agent_tools.cli:_runs_land_batch", False, (),
    ),
    commands.Command(
        "fetch", "runs", "pull an ended remote lane's run directory, log and branches to this machine",
        (
            commands.Arg(("--runs-dir",), {"help": "override: resolve the run's records here instead of the profile's workspace_dir"}),
            commands.Arg(("--profile",)),
            # Not an argparse group: Python 3.14 prints a required group holding a positional as `(--all | run_id)`
            # where 3.12 prints `[--all] [run_id]`, so the help could not read the same on both. _runs_fetch checks it.
            commands.Arg(("run_id",), {"nargs": "?"}),
            commands.Arg(("--all",), {"action": "store_true", "help": "fetch every remote run with no local tasks directory yet; a live run is skipped"}),
        ),
        "agent_tools.cli:_runs_fetch", False, (),
    ),
    commands.Command(
        "review", "runs", "review a contributor PR with the review graph and post the verdict",
        (commands.Arg(("--pr",), {"required": True, "help": "https://github.com/<owner>/<repo>/pull/<n>"}), commands.Arg(("--profile",))),
        "agent_tools.cli:_runs_review", False, (),
    ),
    commands.Command(
        "recover", "runs", "merge an approved task's commit into its phase branch after an escalated merge",
        (
            commands.Arg(("run_id",)), commands.Arg(("task_id",)), commands.Arg(("--repo",), {"required": True}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the merge and the record that would be made; writes no branch, row or file, but fetches origin's default branch first"}),
            commands.Arg(("--reopen",), {"action": "store_true", "help": "put a task an earlier recover closed as done (no merge commit recorded) back to approved, then record the merge"}),
        ),
        "agent_tools.cli:_runs_recover", False, (),
    ),
    commands.Command(
        "series", "runs", "per-run summary rows across a runs directory",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"}), commands.Arg(("--append",))),
        "agent_tools.cli:_runs_series", False, (),
    ),
    commands.Command(
        "wait", "runs", "block until a busy lane's run exits, then print its outcome lines",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--max-seconds",), {"type": float, "default": 3600}),
            commands.Arg(("--interval",), {"type": float, "default": 15}), commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_wait", False, (),
    ),
    commands.Command(
        "events", "runs", "poll a run's log for structured events",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--follow",), {"action": "store_true"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_events", False, (),
    ),
    commands.Command(
        "top", "runs", "live table of busy lanes (runs in flight); --once prints it and exits",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--interval",), {"type": float, "default": 3}), commands.Arg(("--once",), {"action": "store_true"})),
        "agent_tools.cli:_runs_top", False, (),
    ),
    commands.Command(
        "bar", "runs", "one Waybar JSON line: busy lanes, cost, class idle|running|attention",
        (commands.Arg(("--runs-dir",), {"default": "runs"}),),
        "agent_tools.cli:_runs_bar", False, (),
    ),
    commands.Command(
        "herdr", "runs", "report fleet state (idle|working|blocked) to the herdr pane this runs in; no-op outside herdr",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--interval",), {"type": float, "default": 10}), commands.Arg(("--once",), {"action": "store_true"})),
        "agent_tools.cli:_runs_herdr", False, (),
    ),
    commands.Command(
        "notify", "runs", "desktop notifications for exits, quarantines, budget stops and cost",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--once",), {"action": "store_true"}), commands.Arg(("--interval",), {"type": float, "default": 10}),
            commands.Arg(("--replay",), {"action": "store_true", "help": "emit history on first start; default is silent for existing runs when no state file is present"}),
        ),
        "agent_tools.cli:_runs_notify", False, (),
    ),
    commands.Command(
        "detail", "runs", "one run's timeline, objection and last tool calls",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_detail", False, (),
    ),
    commands.Command(
        "stranded", "runs", "every approved task record whose work item is not done, with its remedy",
        (
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_stranded", False, (),
    ),
    commands.Command(
        "cause", "runs", "record why one run's attempt at a task was quarantined",
        (
            commands.Arg(("run_id",)), commands.Arg(("task_id",)), commands.Arg(("cause",), {"choices": list(_STATS_CAUSES)}),
            commands.Arg(("--note",), {"help": "free text kept beside the cause"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_runs_cause", False, (),
    ),
    commands.Command(
        "stop", "runs", "stop a run's process, local or remote, and wait for it to exit",
        (
            commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--profile",)),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_stop", False, (),
    ),
    commands.Command(
        "artifacts", "runs", "list the object-storage keys a run's step rows name (artifacts_url)",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_artifacts", False, (),
    ),
    commands.Command(
        "show", "runs", "print a run's transcripts: the run folder's, else the ones its step rows name in artifacts_url",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"})),
        "agent_tools.cli:_runs_show", False, (),
    ),
    commands.Command(
        "log", "runs", "print a run's check logs: the run folder's, else the ones its step rows name in artifacts_url",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"})),
        "agent_tools.cli:_runs_log", False, (),
    ),
    commands.Command(
        "pause", "runs", "mark a run paused in the store",
        (
            commands.Arg(("run_id",)), commands.Arg(("--reason",)), commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_pause", False, (),
    ),
    commands.Command(
        "resume", "runs", "mark a paused run resumed in the store",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_runs_resume", False, (),
    ),
    commands.Command(
        "move", "runs", "stop a run at its node boundary and relaunch it on another host",
        (
            commands.Arg(("run_id",)), commands.Arg(("--to",), {"required": True}), commands.Arg(("--reason",)),
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--profile",)),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_runs_move", False, (),
    ),
    commands.Command(
        "triage", "runs", "print a quarantined task's facts; reads the store and git only, makes no model call",
        (
            commands.Arg(("task_id",)),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--repo",), {"default": ".", "metavar": "PATH", "help": "the task's git repository (default: the current directory)"}),
        ),
        "agent_tools.cli:_runs_triage", False, (),
        defaults={"profile": None},
    ),
]

VERSIONS_GROUP = commands.Group(
    name="versions", help="component versions against the manifest", description="", epilog="",
    args=(
        commands.Arg(("--root",)),
        commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
        commands.Arg(("--manifest",)),
    ),
    fn="agent_tools.cli:_versions",
)

CONSOLE_GROUP = commands.Group(
    name="console", help="one screen of hosts, drafts, lanes and the chair; keys run cox commands after a y/n",
    description="", epilog="",
    args=(
        commands.Arg(("--runs-dir",), {"default": "runs"}),
        commands.Arg(("--work-dir",), {"default": "."}),
        commands.Arg(("--interval",), {"type": float, "default": 5}),
        commands.Arg(("--once",), {"action": "store_true"}),
        commands.Arg(("--profile",)),
    ),
    fn="agent_tools.cli:_console",
)

DASH_GROUP = commands.Group(
    name="dash",
    help="a versioned JSON snapshot of the workspace: --feed streams it, --once prints one, --detail drills into one kind",
    description="", epilog="",
    args=(
        commands.Arg(("--runs-dir",), {"default": None}),
        commands.Arg(("--work-dir",), {"default": None}),
        commands.Arg(("--feed",), {"action": "store_true"}),
        commands.Arg(("--once",), {"action": "store_true"}),
        commands.Arg(("--interval",), {"type": float, "default": 5}),
        commands.Arg(("--detail",), {"nargs": "+", "metavar": "KIND"}),
    ),
    fn="agent_tools.cli:_dash",
)


SETTINGS_GROUP = commands.Group(
    name="settings", help="the cartridge and profile settings, with where each lives and whether git tracks it",
    description="The cartridge and profile settings, with where each lives and whether git tracks it.",
    epilog="examples:\n  cox settings get\n  cox settings get --json\n  cox settings set cartridge policy.dispatch.max_in_flight 4 --dry-run",
)
SETTINGS_COMMANDS = [
    commands.Command(
        "get", "settings", "print every setting, grouped by section; unset keys appear as built-in defaults, which set can override",
        (commands.Arg(("--json",), {"action": "store_true"}), commands.Arg(("--profile",))),
        "agent_tools.cli:_settings_get", False, (),
    ),
    commands.Command(
        "set", "settings", "change one setting: print the diff and write the file; --commit also commits a git-tracked cartridge",
        (
            commands.Arg(("scope",), {"choices": ("cartridge", "profile", "host")}),
            commands.Arg(("key",)),
            commands.Arg(("value",)),
            commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--commit",), {"action": "store_true", "help": "commit the changed cartridge file in git; profile files are never committed"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_settings_set", False, (),
    ),
]

INSTALL_GROUP = commands.Group(
    name="install", help="clone/update coxswain components against the manifest", description="", epilog="",
    args=(
        commands.Arg(("--root",), {"help": "the installs folder; defaults to the home's installs folder"}),
        commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
        commands.Arg(("--manifest",)),
        commands.Arg(("--provider",), {"default": "claude-code"}),
        commands.Arg(("--with",), {"action": "append", "default": None, "dest": "with_", "metavar": "FLAG"}),
        commands.Arg(("--team",)),
        commands.Arg(("--workspace",)),
        commands.Arg(("--edge",), {"action": "store_true"}),
        commands.Arg(("--dry-run",), {"action": "store_true"}),
    ),
    fn="agent_tools.cli:_install",
)

UPGRADE_GROUP = commands.Group(
    name="upgrade", help="fetch and check out newer pinned versions; refuses dirty checkouts",
    description=(
        "On the stable channel (the default) the checkout moves to the newest release tag.\n"
        "On the edge channel (profile upgrade_channel: edge) it moves to the newest commit on main whose CI passed,\n"
        "restarts the running chair and lander units, runs the doctor, and moves back when any of that fails."
    ),
    epilog="examples:\n  cox upgrade\n  cox upgrade --to 2026.11.8\n  cox upgrade --install-timer",
    args=(
        commands.Arg(("--root",), {"help": "the installs folder; defaults to the home's installs folder"}),
        commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
        commands.Arg(("--manifest",)),
        commands.Arg(("--provider",), {"default": "claude-code"}),
        commands.Arg(("--with",), {"action": "append", "default": None, "dest": "with_", "metavar": "FLAG"}),
        commands.Arg(("--team",)),
        commands.Arg(("--workspace",)),
        commands.Arg(("--to",), {"help": "a release version or a commit; skips the edge channel's CI lookup"}),
        commands.Arg(("--dry-run",), {"action": "store_true"}),
        commands.Arg(("--profile",), {"help": "the routing profile that holds upgrade_channel (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        commands.Arg(("--scheduled",), {"action": "store_true", "help": "what the upgrade timer runs: does nothing unless this machine's channel is edge"}),
        commands.Arg(("--install-timer",), {"action": "store_true", "help": "write the schedule that runs the scheduled upgrade (a systemd user timer on Linux, a launchd agent on macOS) and print the lines that activate it; runs nothing itself"}),
        commands.Arg(("--uninstall-timer",), {"action": "store_true", "help": "remove the schedule files --install-timer wrote on this platform"}),
        commands.Arg(("--interval-minutes",), {"type": int, "default": 30, "help": "minutes between scheduled upgrades in the installed timer (default: 30)"}),
        commands.Arg(("--environment-file",), {"help": "with --install-timer: the NAME=value file the timer's service loads (default: ~/.config/agent-tools/garage.env)"}),
    ),
    fn="agent_tools.cli:_upgrade",
)

HOME_GROUP = commands.Group(
    name="home", help="the live dashboard: runs, leader, backlog", description="", epilog="",
    args=(commands.Arg(("--profile",)),), fn="agent_tools.cli:_home",
)

SESSION_GROUP = commands.Group(
    name="session", help="open the coxswain Claude session",
    description="Open the coxswain Claude session, with the same argv bare `cox` uses.",
    epilog="examples:\n  cox session\n  cox session --print-argv\n  cox session -- -r resume-me",
    args=(
        commands.Arg(("--profile",), {"help": "the profile to launch claude against"}),
        # distinct dests: the top-level parser owns no_plugin and print_argv, and argparse copies a matched subparser's namespace over it
        commands.Arg(("--no-plugin",), {"action": "store_true", "dest": "session_no_plugin", "help": "start claude without --plugin-dir"}),
        commands.Arg(("--print-argv",), {"action": "store_true", "dest": "session_print_argv", "help": "print the claude argv and cwd instead of exec'ing it"}),
        commands.Arg(("--workspace",), {"action": "store_true", "dest": "session_workspace", "help": "force the workspace session that can act as the chair"}),
        commands.Arg(("session_extra",), {"nargs": "*", "metavar": "CLAUDE_ARG", "help": "after --: passed to claude verbatim, as bare cox does"}),
    ),
    fn="agent_tools.cli:_session",
)

USAGE_GROUP = commands.Group(
    name="usage", help="spend pacing against the ceiling for the current window",
    description="Spend pacing against the ceiling for the current window.",
    epilog="examples:\n  cox usage assess\n  cox usage assess --json --runs-dir runs",
)
USAGE_COMMANDS = [
    commands.Command(
        "assess", "usage", "the pacing verdict for the current spend window",
        (
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the window ceiling (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_usage_assess", False, (),
    ),
]

STATS_GROUP = commands.Group(
    name="stats", help="load the run corpus into the stats store",
    description="Load the run corpus into the stats store.",
    epilog="examples:\n  cox stats ingest\n  cox stats ingest runs --db workspace/stats/stats.db"
           "\n  cox stats roles --json\n  cox stats tiers --since 2026-09-01\n  cox stats gates --since 2026-09-01\n  cox stats explain build --json\n  cox stats series --json"
           "\n  cox stats coverage --json\n  cox stats bounds --json\n  cox stats spend-mix --json"
           "\n  cox stats examples --role handoff --out examples.jsonl"
           "\n  cox stats system-one --role handoff --propose",
)
STATS_COMMANDS = [
    commands.Command(
        "ingest", "stats", "load usage, task, node and launch records into stats.db",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--work-store-root",), {"default": "work"}),
            commands.Arg(("--cartridges-repo",), {"default": None}),
        ),
        "agent_tools.cli:_stats_ingest", False, (),
    ),
    commands.Command(
        "roles", "stats", "landed rate, attempts-to-land and $/landed per role and model",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--runs-dir",), {"default": "runs", "help": "where lands are read from"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--cartridge-sha",), {"default": None, "help": "keep only runs on this cartridge_sha"}),
            commands.Arg(("--provider-profile",), {"default": None, "help": "keep only runs on this provider_profile"}),
        ),
        "agent_tools.cli:_stats_roles", False, (),
    ),
    commands.Command(
        "tiers", "stats", "per-role model summaries, a cost-aware pick and the spend it would save",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--min-samples",), {"type": int, "default": 20, "help": "tasks a model needs before it can be picked (default 20)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_tiers", False, (),
    ),
    commands.Command(
        "models", "stats", "per-role cost, tokens, turns, $/landed and first-try per model id",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--role",), {"default": None, "help": "keep only this role"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only calls whose run started on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_models", False, (),
    ),
    commands.Command(
        "causes", "stats", "quarantined attempts by cause and kind, with sample reasons",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_causes", False, (),
    ),
    commands.Command(
        "efficiency", "stats", "cost per turn, cost per completion, first-try rate, waste share, per day",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--days",), {"type": int, "default": 7, "help": "UTC days to report, today included (default 7)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_efficiency", False, (),
    ),
    commands.Command(
        "gates", "stats", "what each review, validation and plan gate costs and how often it changes the outcome",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--store",), {"action": "store_true", "help": "read task and call rows from the run store instead of stats.db"}),
            commands.Arg(("--runs-dir",), {"default": "runs", "help": "the run store's directory, with --store"}),
            commands.Arg(("--min-sample",), {"type": int, "default": _GATES_MIN_SAMPLE, "help": "decided tasks a role needs before it is judged, else 'not enough data' (default %(default)s)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_gates", False, (),
    ),
    commands.Command(
        "explain", "stats", "the failure-class breakdown behind one role",
        (
            commands.Arg(("role",)),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_explain", False, (),
    ),
    commands.Command(
        "series", "stats", "per-run summary rows read from the stats store",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--cartridge-sha",), {"default": None, "help": "keep only runs on this cartridge_sha"}),
            commands.Arg(("--provider-profile",), {"default": None, "help": "keep only runs on this provider_profile"}),
        ),
        "agent_tools.cli:_stats_series", False, (),
    ),
    commands.Command(
        "coverage", "stats", "known/total provenance rows for runs, calls and tasks",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_coverage", False, (),
    ),
    commands.Command(
        "bounds", "stats", "n/p50/p95/max and strict/moderate/liberal candidate ceilings per role and model",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--level",), {"choices": ("strict", "moderate", "liberal"), "default": None}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--write",), {"default": None, "help": "also write the JSON table to PATH inside the repo checkout"}),
        ),
        "agent_tools.cli:_stats_bounds", False, (),
    ),
    commands.Command(
        "spend-mix", "stats", "per-model token counts and cost share by class, plus the build-only split",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_spend_mix", False, (),
    ),
    commands.Command(
        "chair", "stats", "harness PRs and $, chair $ per PR, hand-finished lands and quarantine $ by cause",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only records and transcript lines dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--session",), {"action": "append", "default": None, "help": "a Claude session id to count as chair cost, beside those in chair.json (repeatable)"}),
            commands.Arg(("--work-store-root",), {"default": "work"}),
            commands.Arg(("--projects-dir",), {"default": "~/.claude/projects"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming cartridges_dir (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_stats_chair", False, (),
    ),
    commands.Command(
        "examples", "stats", "the local system-one backend's training file (JSON lines) from task records",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"action": "append", "required": True, "help": "handoff or review_charter; repeat for several"}),
            commands.Arg(("--out",), {"default": None, "help": "write the file to PATH (default: stdout)"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only records dated on or after DATE (YYYY-MM-DD)"}),
        ),
        "agent_tools.cli:_stats_examples", False, (),
    ),
    commands.Command(
        "flaky", "stats", "tests that needed a serial rerun three times in a week, from task records",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--days",), {"type": int, "default": 7, "help": "the window, today included"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_flaky", False, (),
    ),
    commands.Command(
        "system-one", "stats", "shadow-to-on graduation report per role, and a proposal for the maintainer to approve",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"default": None, "help": "report this role only"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--propose",), {"action": "store_true", "help": "write a graduation proposal for each READY role; never edits a profile"}),
            commands.Arg(("--plans-dir",), {"default": "plans", "help": "where --propose writes system-one-graduation-<role>-<date>.md"}),
        ),
        "agent_tools.cli:_stats_system_one", False, (),
    ),
    commands.Command(
        "shadow-tier", "stats", "cheap-tier shadow trial per role: agreement, costly rate, cost saved, and the graduation bar; changes nothing",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"default": None, "help": "report this role only"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_shadow_tier", False, (),
    ),
    commands.Command(
        "lanes", "stats", "per hour, the average and peak busy lanes and the idle minutes, from the store's run spans",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--hours",), {"type": int, "default": 24, "help": "how many clock hours back to report"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_stats_lanes", False, (),
    ),
]


LAKE_GROUP = commands.Group(
    name="lake", help="the Iceberg lake: sync the run store and traces into it, query it, check it",
    description="The Iceberg lake: sync the run store and traces into it, query it with SQL, and check it. Needs the optional extra `lake`.",
    epilog="examples:\n  cox lake sync\n  cox lake sync --dry-run --json\n"
           "  cox lake query \"SELECT count(*) AS n FROM runs\"\n  cox lake doctor",
)
LAKE_COMMANDS = [
    commands.Command(
        "sync", "lake", "append new run-store rows and register new trace files; --dry-run writes nothing",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "report what would be synced; create and write nothing"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_lake_sync", False, (),
    ),
    commands.Command(
        "query", "lake", "run DuckDB SQL over the lake tables (runs, phases, ...) and print the rows",
        (
            commands.Arg(("sql",), {"help": "the SQL to run; a lake table is named by its bare name, such as runs"}),
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_lake_query", False, (),
    ),
    commands.Command(
        "doctor", "lake", "check the lake: catalog, namespace, warehouse and each table; exits 1 on a failed check",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_lake_doctor", False, (),
    ),
]


_DEV_MOVED = "moved: run `uv run --frozen python -m devtools <command> ...` from the coxswain checkout (from 0.15.0)"


def _dev_moved(a: argparse.Namespace) -> int:
    print(_DEV_MOVED)
    return 2


_RELEASE_HELP = "deploy a release to every host: not available yet (the other old release commands moved to the checkout)"


def version_line(version: str) -> str:
    return f"cox {version}"


class _VersionAction(argparse.Action):
    """The metadata lookup runs here, at parse time, so ordinary commands never pay for it."""

    def __init__(
        self, option_strings: Sequence[str], dest: str = argparse.SUPPRESS, default: object = argparse.SUPPRESS, help: str | None = None
    ) -> None:
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(
        self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: object, option_string: str | None = None
    ) -> None:
        from agent_tools.version_info import package_version  # local: keeps the import off every other command's path

        print(version_line(package_version()))
        parser.exit(0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cox",
        description=__doc__,
        epilog=(
            "examples:\n"
            "  bare cox opens the coxswain Claude session: attached over MCP to a live chair service, from any\n"
            "    directory and without touching its lease; with no live chair, the workspace session that can act\n"
            "    as the chair. cox --workspace forces the workspace session; cox --print-argv shows which would run\n"
            "  cox home opens the towpath dashboard\n"
            "  cox session opens the coxswain Claude session, with the same argv\n"
            "  cox setup doctor checks this machine\n"
            "  cox route launch epic runs a filed initiative"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # dest="launcher_profile", not "profile": eight subparsers below declare
    # their own `--profile` with default None, and argparse copies a matched
    # subparser's namespace back over the parent's, so a shared dest would
    # let `cox --profile P route status` silently lose P to that default.
    p.add_argument("--profile", dest="launcher_profile", help="bare cox: the profile to launch claude against")
    p.add_argument("--no-plugin", action="store_true", help="bare cox: start claude without --plugin-dir")
    p.add_argument("--print-argv", action="store_true", help="bare cox: print the claude argv and cwd instead of exec'ing it")
    p.add_argument("--workspace", action="store_true", dest="workspace_session", help="bare cox: force the workspace session that can act as the chair")
    p.add_argument("--version", "-V", action=_VersionAction, help="print the cox version and exit")
    sub = p.add_subparsers(dest="group", required=False)

    group, rows = _table_entry("runs")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("stats")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("lake")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("usage")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("router")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("steward")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("epic")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("pr")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("plan")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("route")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("chair")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("lander")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("worker")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("host")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("courier")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("mcp")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("install")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("upgrade")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("versions")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("console")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("dash")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("settings")
    commands.build_parser(rows, [group], sub)

    dev = sub.add_parser("dev", help="moved: maintainer commands now run from the coxswain checkout",
                         description=_DEV_MOVED)
    # swallow every subcommand and flag the old group took, so the pointer prints instead of argparse erroring
    dev.add_argument("rest", nargs=argparse.REMAINDER)
    dev.set_defaults(fn=_dev_moved)

    release_p = sub.add_parser("release", help=_RELEASE_HELP,
                               description=f"cox release deploy VERSION is not available yet: its host-by-host edge was never built.\n"
                                           f"Other release commands: {_DEV_MOVED}",
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    # `deploy` is read from `rest` by `_release`; the old maintainer subcommands and flags land there too and print the pointer
    release_p.add_argument("rest", nargs=argparse.REMAINDER)
    release_p.set_defaults(fn=_release)

    group, rows = _table_entry("home")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("session")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("setup")
    setup_p = commands.build_parser(rows, [group], sub)["setup"]
    # commands.build_parser's generic bare-group fallback prints help and
    # exits 2; a bare `cox setup` instead hands off to towpath, so override it here.
    setup_p.set_defaults(fn=_setup_tui)
    return p


MCP_GROUP = commands.Group(
    name="mcp", help="serve the chair's tools to MCP clients",
    description=(
        "Serve the board, the queue and the cartridge settings to an MCP client. `cox mcp` and `cox mcp serve` are the same.\n"
        "Run it on the machine that runs the chair and holds the cartridge.\n"
        "Reads answer from the board as it is now. Plain writes (file an intake, set a stage or a priority, hold, "
        "release, change a cartridge setting) run the cox command a person would type and return its outcome.\n"
        "A release cut, a scope change and a feature switch are sent to the chair as requests.\n"
        "With no --http it speaks MCP on stdin and stdout, which is what a local client launches. "
        "--http binds only a loopback, private, tailnet or link-local address and has no TLS."
    ),
    epilog="examples:\n  cox mcp\n  cox mcp serve --http 127.0.0.1:8765 --token-file tokens",
    args=(
        commands.Arg(("mode",), {"nargs": "?", "choices": ("serve",), "help": "optional; serving is all this command does"}),
        commands.Arg(("--http",), {"metavar": "HOST:PORT", "help": "serve over HTTP on this literal private address instead of stdin and stdout"}),
        commands.Arg(("--token-file", "--tokens-file"), {"metavar": "PATH", "dest": "tokens_file", "help": "with --http: lines of 'label token'; a token is at least 24 characters"}),
        commands.Arg(("--label",), {"help": "who the stdio client is, on every entry it sends; default $COXSWAIN_MCP_CONVERSATION, else stdio:<user>. Over HTTP the token's label is used"}),
        commands.Arg(("--profile",)),
    ),
    fn="agent_tools.cli:_mcp_serve",
)


COURIER_GROUP = commands.Group(name="courier", help="the courier bus: hand a reference to another label", description="", epilog="")
COURIER_COMMANDS = [
    commands.Command(
        "send", "courier", "append a bus entry naming a courier reference",
        (commands.Arg(("ref",)), commands.Arg(("--to",), {"required": True}), commands.Arg(("--note",), {"required": True}), commands.Arg(("--profile",)),
         commands.Arg(("--event",), {"help": "a typed event name"}), commands.Arg(("--topic",), {"help": "a topic to file the entry under"}),
         commands.Arg(("--field",), {"action": "append", "help": "key=value, repeatable"})),
        "agent_tools.cli:_courier_send", False, (),
    ),
    commands.Command(
        "inbox", "courier", "list this label's unacknowledged bus entries",
        (commands.Arg(("--label",)), commands.Arg(("--profile",)),
         commands.Arg(("--topic",)), commands.Arg(("--event",)), commands.Arg(("--consumer",)),
         commands.Arg(("--wake",), {"action": "store_true", "help": "only entries that need a model"})),
        "agent_tools.cli:_courier_inbox", False, (),
    ),
    commands.Command(
        "ack", "courier", "acknowledge one bus entry by id",
        (commands.Arg(("id",)), commands.Arg(("--profile",)), commands.Arg(("--consumer",))),
        "agent_tools.cli:_courier_ack", False, (),
    ),
]

PLAN_GROUP = commands.Group(
    name="plan", help="visual plans through the local bridge",
    description="Visual plans through the local bridge.",
    epilog="examples:\n  cox plan serve work/<id> --kind plan\n  cox plan serve work/<id> --check",
)
PLAN_COMMANDS = [
    commands.Command(
        "serve", "plan", "serve a visual plan through the local bridge",
        (commands.Arg(("dir",)), commands.Arg(("--kind",), {"default": "plan"}), commands.Arg(("--check",), {"action": "store_true"}), commands.Arg(("--no-open",), {"action": "store_true"})),
        "agent_tools.cli:_plan_serve", False, (),
    ),
]

EPIC_GROUP = commands.Group(
    name="epic", help="watch a detached run",
    description="Watch a detached run.",
    epilog="examples:\n  cox epic watch RUN.pid --log RUN.log\n  cox epic watch RUN.pid --json",
)
EPIC_COMMANDS = [
    commands.Command(
        "watch", "epic", "poll a detached run's pidfile until it exits",
        (
            commands.Arg(("pidfile",)), commands.Arg(("--log",)),
            commands.Arg(("--max-seconds",), {"type": float, "default": 570}), commands.Arg(("--interval",), {"type": float, "default": 20}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_epic_watch", False, (),
    ),
]

PR_GROUP = commands.Group(
    name="pr", help="land a pull request once its required checks pass",
    description="Land a pull request once its required checks pass.",
    epilog="examples:\n  cox pr land 123 --repo ~/repos/x",
)
PR_COMMANDS = [
    commands.Command(
        "land", "pr", "wait for the required checks, squash-merge a pull request and mark its tasks done",
        (
            commands.Arg(("number",), {"type": int, "help": "pull request number"}), commands.Arg(("--repo",), {"required": True}),
            commands.Arg(("--timeout",), {"type": float, "default": 600, "help": "seconds to wait for the required checks (default 600)"}),
        ),
        "agent_tools.cli:_pr_land", False, (),
    ),
]

ROUTE_GROUP = commands.Group(
    name="route", help="file work for the harness, and see what is queued or running",
    description="File work for the harness, and see what is queued or running.",
    epilog="examples:\n  cox route launch epic --initiative work/<id> --repo PATH\n  cox route status --profile PATH",
)
_CHAIR_PID_ARG = commands.Arg(("--pid",), {"type": int, "help": "the durable pid that owns the loop (default: the parent process)"})
_LAUNCH_SHARED_ARGS = (
    commands.Arg(("--tier-ceiling",), {"choices": ("cheap", "standard", "deep")}),
    commands.Arg(("--effort-ceiling",), {"choices": ("low", "high")}),
    commands.Arg(("--force",), {"action": "store_true", "help": "launch despite a usage stop, a foreign live leader or a full local host"}),
    commands.Arg(("--no-claim",), {"action": "store_true", "help": "launch without taking an unheld or stale loop"}),
    commands.Arg(("--label",)),
)
ROUTE_COMMANDS = [
    commands.Command(
        "context", "route", "the routing profile's resolved context",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_route_context", False, (),
    ),
    commands.Command(
        "status", "route", "what is queued or running for this profile",
        (
            commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--all",), {"action": "store_true", "help": "every run, not only live and recent ones"}),
        ),
        "agent_tools.cli:_route_status", False, (),
    ),
    commands.Command(
        "file", "route", "file a new ticket for the harness",
        (
            commands.Arg(("--profile",)), commands.Arg(("--repo",)), commands.Arg(("--title",)), commands.Arg(("--body",)),
            commands.Arg(("--phase",), {"default": "build"}), commands.Arg(("--intake",), {"action": "store_true"}),
            commands.Arg(("--from-intake",), {"help": "link and file an existing intake file's initiative, then retire it"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "file an intake even when its title duplicates an open issue"}),
            commands.Arg(("--link",), {"default": None, "help": "with --intake: the issue url this intake is filed from, so a later pull skips it"}),
            commands.Arg(("--after",), {"action": "append", "default": None, "help": "with --intake: ids this intake waits on; repeat the flag or comma-separate"}),
            commands.Arg(("--draft",), {"action": "store_true", "help": "with --intake: file it as a draft, which the chair skips until it is ready (the default)"}),
            commands.Arg(("--ready",), {"action": "store_true", "help": "with --intake: file it ready for the chair to pick up"}),
            commands.Arg(("--epic",), {"default": None, "help": "with --intake: the epic id this intake belongs to; it must already exist"}),
            commands.Arg(("--priority",), {"default": None, "help": "with --intake: a whole number; higher runs first"}),
            commands.Arg(("--target-release",), {"default": None, "help": "with --intake: the release it ships in, a dotted version such as 2026.11.6"}),
        ),
        "agent_tools.cli:_route_file", False, (),
    ),
    commands.Command(
        "priority", "route", "move an initiative or a queued intake up or down the launch order",
        (
            commands.Arg(("initiative",), {"nargs": "?", "help": "an initiative or a queued intake; required with a flag, omit it to list the open queue"}),
            commands.Arg(("--profile",)),
            commands.Arg(("--set",), {"type": int, "default": None, "metavar": "N", "help": "write N on every item"}),
            commands.Arg(("--up",), {"action": "store_true", "help": "pass the nearest higher open initiative"}),
            commands.Arg(("--down",), {"action": "store_true", "help": "pass the nearest lower open initiative"}),
            commands.Arg(("--top",), {"action": "store_true", "help": "one above every other open initiative"}),
            commands.Arg(("--bottom",), {"action": "store_true", "help": "one below every other open initiative"}),
        ),
        "agent_tools.cli:_route_priority", False, (),
        exclusive=("--set", "--up", "--down", "--top", "--bottom"),
    ),
    commands.Command(
        "schedule", "route", "project the whole queue as one schedule",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--explain",), {"metavar": "INITIATIVE", "default": None, "help": "why this initiative sits where it does"}),
            commands.Arg(("--should-jump",), {"metavar": "INITIATIVE", "default": None, "help": "ask whether this initiative should move up the queue"}),
        ),
        "agent_tools.cli:_route_schedule", False, (),
        exclusive=("--explain", "--should-jump"),
    ),
    commands.Command(
        "pull", "route", "file intake tickets from a source, once per link",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--source",), {"default": None, "help": "one source; default every source in the profile"}),
            commands.Arg(("--dry-run",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_route_pull", False, (),
    ),
    commands.Command(
        "lint", "route", "static ticket lint over a filed initiative, work-shape.md §3",
        (commands.Arg(("initiative_dir",)), commands.Arg(("--repo",), {"default": None})),
        "agent_tools.cli:_route_lint", False, (),
    ),
    commands.Command(
        "groups", "route", "print the newest plans/intake-groups/<date>.md file, work-shape.md §5",
        (commands.Arg(("--profile",)),),
        "agent_tools.cli:_route_groups", False, (),
    ),
    commands.Command(
        "cartridges", "route", "publish this machine's cartridges to the store, or write a published snapshot here",
        (
            commands.Arg(("action",), {"choices": ("publish", "fetch")}),
            commands.Arg(("--sha",), {"help": "the snapshot to fetch"}),
            commands.Arg(("--replace",), {"action": "store_true", "help": "fetch over cartridges that no fetch put there"}),
            commands.Arg(("--into",), {"help": "the directory to publish from or fetch into; default the profile's cartridges_dir"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_cartridges", False, (),
        description=(
            "Cartridges are written and versioned where their owner keeps them. The store only carries a copy of "
            "the exact files a launch used, under the digest of those files, so a lane host can read them.\n"
            "publish prints the sha of the profile's cartridges_dir and stores its files once.\n"
            "fetch makes cartridges_dir hold exactly that snapshot. It refuses to write over files a person put "
            "there or edited, unless --replace is given."
        ),
    ),
    commands.Command(
        "export", "route", "write this machine's work/ and intake/ files from the store; nothing is committed",
        (commands.Arg(("--profile",)),),
        "agent_tools.cli:_route_export", False, (),
        description=(
            "The store holds the board. This writes it out as files under the workspace so they can be read, and "
            "removes files no row names.\nIt first loads any ticket a decompose wrote as a file and the store has "
            "not seen. Under work_state files it does nothing."
        ),
    ),
    commands.Command(
        "drift", "route", "items whose store state and file state differ",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_route_drift", False, (),
    ),
    commands.Command(
        "import", "route", "load the current work item files into the store's work_items table",
        (commands.Arg(("--profile",)), commands.Arg(("--workspace",))),
        "agent_tools.cli:_route_import", False, (),
    ),
    commands.Command(
        "paths", "route", "rewrite recorded repo paths to the tilde form, dry run first",
        (
            commands.Arg(("--profile",)), commands.Arg(("--workspace",)),
            commands.Arg(("--apply",), {"action": "store_true", "help": "write the changes; the default only lists them"}),
            commands.Arg(("--move",), {"nargs": 2, "metavar": ("OLD", "NEW"), "help": "swap repo prefix OLD for NEW in open records only; done and dropped records are left alone"}),
        ),
        "agent_tools.cli:_route_paths", False, (),
    ),
    commands.Command(
        "store-fill", "route", "fill the store's work_items from the workspace files",
        (commands.Arg(("--profile",)), commands.Arg(("--workspace",))),
        "agent_tools.store_fill:main", False, (),
    ),
    commands.Command(
        "approve", "route", "approve a draft initiative's todo tickets: todo becomes ready",
        (
            commands.Arg(("initiative",)), commands.Arg(("--task",), {"help": "approve this one task only"}),
            commands.Arg(("--by",), {"help": "who approves; default the OS user"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_approve", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "decline", "route", "decline a draft initiative: its todo tickets become dropped",
        (
            commands.Arg(("initiative",)), commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who declines; default the OS user"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_decline", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "edit", "route", "change a queued intake, an initiative or a task ticket",
        (
            commands.Arg(("id",), {"help": "a queued intake, an initiative or a task ticket"}), commands.Arg(("--title",)),
            commands.Arg(("--body",)), commands.Arg(("--body-file",)),
            commands.Arg(("--note",), {"default": None, "help": "task ticket only: append this text to the body once, with or without --state; not with --body or --body-file"}),
            commands.Arg(("--repo",)),
            commands.Arg(("--initiative",), {"default": None, "help": "task ticket only: the initiative holding the task, so an id repeated across initiatives picks this one"}),
            commands.Arg(("--surfaces",), {"help": "task ticket only: comma-separated paths that replace the list"}),
            commands.Arg(("--state",), {"help": "task ticket only: ready, approved, done or dropped; never backward"}),
            commands.Arg(("--pr",), {"help": "with --state done: the merged PR url to record on the task, also on a task that is already done; without it the merged PR is looked up with gh"}),
            commands.Arg(("--rebuild",), {"action": "store_true", "help": "with --state ready: send an approved task back to build on current main; needs a recorded land conflict or --force"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "with --rebuild: allow it with no recorded land conflict"}),
            commands.Arg(("--link",), {"default": None, "help": "queued intake only: set the issue url; an empty value clears the link"}),
            commands.Arg(("--after",), {"action": "append", "default": None, "help": "queued intake only: ids it waits on, repeated or comma-separated; an empty value clears the list"}),
            commands.Arg(("--epic",), {"default": None, "help": "initiative only: join the epic with this id, or none to leave it"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the diff and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_edit", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "epic", "route", "create an epic definition file: `route epic new <id>`",
        (
            commands.Arg(("action",), {"help": "only `new`"}), commands.Arg(("id",), {"help": "the epic id, a lowercase slug"}),
            commands.Arg(("--title",)), commands.Arg(("--target-branch",)),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_epic", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "remove", "route", "remove queued intakes or initiatives that have no live run",
        (
            commands.Arg(("id",), {"nargs": "*", "help": "queued intakes or initiatives; repeat for several"}),
            commands.Arg(("--match",), {"metavar": "GLOB", "help": "also remove every queued intake whose file stem or id matches"}),
            commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who removes; default the OS user"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_remove", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "hold", "route", "hold a queued intake or an initiative so the chair skips it until released; for an intake, an alias for draft with a reason",
        (
            commands.Arg(("id",), {"help": "a queued intake or an initiative"}),
            commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who holds; default the OS user"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_hold", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "release", "route", "release a held intake or initiative back to the queue; an intake moves to ready",
        (
            commands.Arg(("id",), {"help": "a held intake or initiative"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_release", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "ready", "route", "move a queued intake to ready so the chair can pick it up",
        (
            commands.Arg(("id",), {"help": "a queued intake"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_stage_ready", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "draft", "route", "move a queued intake to draft so the chair skips it until it is ready",
        (
            commands.Arg(("id",), {"help": "a queued intake"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_stage_draft", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "requeue", "route", "requeue the intake of an initiative that has no live tasks",
        (
            commands.Arg(("id",), {"help": "an intake or an initiative"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_requeue", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "leader", "route", "whether this session is the chair (the SessionStart line)", (), None, False, (),
        subcommands=(
            commands.Command(
                "status", "route", "print one line: held by this session, taken over, or not the chair",
                (commands.Arg(("--profile",)),),
                "agent_tools.cli:_route_leader_status", False, (),
            ),
        ),
        sub_dest="leader_cmd", sub_required=False,
    ),
    commands.Command(
        "chair", "route", "the chair lock for the landing loop (runs/chair.json)", (), None, False, (),
        subcommands=(
            commands.Command(
                "take", "route", "take the chair lock; --steal overrides a live holder and names it",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--steal",), {"action": "store_true", "help": "override a live holder and name it"}),
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "how long the takeover lasts before the loop may steal it (default: policy.leader.takeover_hours)"}),
                    commands.Arg(("--model",), {"choices": ["sonnet", "opus", "haiku"], "default": None, "help": "the model alias this chair runs on, recorded in the chair record (default: not recorded)"}),
                ),
                "agent_tools.cli:_route_chair_take", False, (),
            ),
            commands.Command(
                "extend", "route", "move the end of the chair takeover this session holds",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "hours from now (default: policy.leader.takeover_hours)"}),
                ),
                "agent_tools.cli:_route_chair_extend", False, (),
            ),
            commands.Command(
                "beat", "route", "refresh the chair lock's heartbeat",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--run",)),
                ),
                "agent_tools.cli:_route_chair_beat", False, (),
            ),
            commands.Command(
                "release", "route", "release the chair lock this session holds",
                (commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG),
                "agent_tools.cli:_route_chair_release", False, (),
            ),
            commands.Command(
                "status", "route", "the chair lock's holder and computed state",
                (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
                "agent_tools.cli:_route_chair_status", False, (),
            ),
            commands.Command(
                "clear", "route", "remove the chair lock file, refusing a live holder unless --force",
                (
                    commands.Arg(("--profile",)),
                    commands.Arg(("--force",), {"action": "store_true", "help": "clear the lock even if its recorded pid is live"}),
                ),
                "agent_tools.cli:_route_chair_clear", False, (),
            ),
            commands.Command(
                "chat", "route", "append to or read the leader chat thread (runs/leader.chat.jsonl)",
                (
                    commands.Arg(("text",), {"nargs": "?"}), commands.Arg(("--profile",)),
                    commands.Arg(("--read",), {"action": "store_true"}),
                    commands.Arg(("--since",)), commands.Arg(("--json",), {"action": "store_true"}),
                    commands.Arg(("--as-leader",), {"action": "store_true", "help": "send as the lock's holder; refuses unless this process is the live holder"}),
                ),
                "agent_tools.cli:_route_chair_chat", False, (),
            ),
            commands.Command(
                "converse", "route", "talk to the running chair through the courier, without the lease (`chat` is the leader thread)", (), None, False, (),
                subcommands=(
                    commands.Command(
                        "send", "route", "send the chair a directive; it applies on the chair's next tick",
                        (
                            commands.Arg(("kind",), {"help": "one of: priority, hold, release, scope, feature, cut_release"}),
                            commands.Arg(("args",), {"nargs": "*", "help": "key=value pairs, e.g. target_kind=task target=t1 rank=1"}),
                            commands.Arg(("--profile",)),
                            commands.Arg(("--sender",), {"default": "session"}),
                        ),
                        "agent_tools.cli:_route_chair_converse_send", False, (),
                    ),
                    commands.Command(
                        "ask", "route", "ask the chair a question and wait a bounded time for its answer",
                        (
                            commands.Arg(("kind",), {"help": "one of: left_on_release, why_not_launched, what_failed"}),
                            commands.Arg(("subject",), {"nargs": "?", "default": ""}),
                            commands.Arg(("--profile",)),
                            commands.Arg(("--sender",), {"default": "session"}),
                            commands.Arg(("--wait",), {"type": float, "default": 30.0, "help": "seconds to wait for the answer (default 30)"}),
                        ),
                        "agent_tools.cli:_route_chair_converse_ask", False, (),
                    ),
                    commands.Command(
                        "state", "route", "print the directives in force, the epics the chair last saw, and recent events",
                        (commands.Arg(("--profile",)),),
                        "agent_tools.cli:_route_chair_converse_state", False, (),
                    ),
                ),
                sub_dest="converse_cmd", sub_required=False,
            ),
        ),
        sub_dest="chair_cmd", sub_required=False,
    ),
    commands.Command(
        "sync", "route", "mirror the work store onto the profile's tracker",
        (
            commands.Arg(("--profile",)), commands.Arg(("--item",)), commands.Arg(("--project",)),
            commands.Arg(("--workspace",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--prune-out-of-scope",), {"action": "store_true", "help": "close open task issues outside tracker_scope as not planned and remove their cards; with --dry-run, list them only. A coxswain label added by hand counts as made by sync"}),
        ),
        "agent_tools.cli:_route_sync", False, (),
    ),
    commands.Command(
        "launch", "route", "run one of the harness's graphs directly; `epic` is the run of one initiative", (), None, False, (),
        subcommands=(
            commands.Command(
                "epic", "route", "launch the run of one filed initiative (the epic graph; epic is its historical name)",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--fix-attempts",), {"type": int, "default": None}),
                    commands.Arg(("--base",), {"help": "branch phase branches are cut from, as origin/<branch>; default: the repo's base: setting, then origin/HEAD, then main"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    commands.Arg(("--on",), {"help": "start the lane on this host from the hosts table; `local` is this machine. Without it the table places the lane"}),
                    commands.Arg(("--local",), {"action": "store_true", "help": "start the lane on this machine, not where the hosts table places it"}),
                    commands.Arg(("--include-blocked",), {"action": "store_true", "help": "launch despite a ready task behind a blocked item (--force does not)"}),
                    commands.Arg(("--phase",), {"action": "append", "default": None, "help": "run only the named phases of the initiative; repeat to start one run per phase, each on its own lane"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "epic"},
            ),
            commands.Command(
                "decompose", "route", "launch the decompose graph against an idea",
                (
                    commands.Arg(("--profile",)),
                    commands.Arg(("--idea",), {"required": True, "metavar": "PATH", "help": "the intake/idea markdown file"}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--task-ids",), {"choices": ["ordinal"], "help": "name the graph's own tasks <initiative-id>-t1, -t2, ..."}),
                    commands.Arg(("--local",), {"action": "store_true", "help": "start the lane on this machine, not where the hosts table places it"}),
                    commands.Arg(("--detach",), {"action": "store_true", "help": "start the decompose in the background and return once it has started"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "decompose"},
            ),
            commands.Command(
                "rescue", "route", "launch the rescue graph against a task",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--task",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--base",), {"help": "branch the rescue checks against, as origin/<branch>; default: the repo's base: setting, then origin/HEAD, then main"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "rescue"},
            ),
            commands.Command(
                "cos", "route", "launch the cos graph",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "cos"},
            ),
            commands.Command(
                "sweep", "route", "launch the sweep graph against an idea",
                (
                    commands.Arg(("--idea",), {"required": True}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--label",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                ),
                "agent_tools.cli:_route_launch_sweep", False, (),
            ),
        ),
        sub_dest="graph", sub_required=True,
    ),
]


SETUP_GROUP = commands.Group(
    name="setup", help="does this machine's profile actually work",
    description="Does this machine's profile actually work.",
    epilog="examples:\n  cox setup doctor --profile PATH\n  cox setup install --root PATH --team NAME --workspace PATH",
    args=(commands.Arg(("--profile",)),),
)
# The connection flags and --dry-run, shared by `store` and `store switch`.
_STORE_FLAGS = (
    commands.Arg(("--host",), {"help": "shared: Postgres host"}),
    commands.Arg(("--port",), {"type": int, "help": "shared: Postgres port"}),
    commands.Arg(("--database",), {"help": "shared: Postgres database"}),
    commands.Arg(("--user",), {"help": "shared: Postgres user"}),
    commands.Arg(("--bucket",), {"help": "shared: S3 bucket for traces"}),
    commands.Arg(("--endpoint",), {"help": "shared: S3 endpoint URL"}),
    commands.Arg(("--region",), {"help": "shared: S3 region"}),
    commands.Arg(("--password-env",), {"help": "shared: NAME of the variable holding the Postgres password"}),
    commands.Arg(("--access-key-env",), {"help": "shared: NAME of the variable holding the S3 access key id"}),
    commands.Arg(("--secret-key-env",), {"help": "shared: NAME of the variable holding the S3 secret key"}),
    commands.Arg(("--dry-run",), {"action": "store_true", "help": "validate and print; write and probe nothing"}),
)


def _suppressed(args: tuple) -> tuple:
    """Copies of `args` that leave the namespace alone when absent, so a flag given before the verb is kept."""
    return tuple(commands.Arg(arg.flags, {**arg.kwargs, "default": argparse.SUPPRESS}) for arg in args)


SETUP_COMMANDS = [
    commands.Command(
        "doctor", "setup", "check this machine's profile against what it needs",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--repo",), {"help": "target repo to check for a .agent/cartridge.yaml overlay (default: cwd)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--host",), {"help": "run the doctor on this lane host from lane_hosts; --json is ignored"}),
            commands.Arg(("--fix",), {"action": "store_true", "help": "move damaged log lines to a .damaged file beside the log, start or create the warm smoke Postgres container where docker answers, then check again"}),
        ),
        "agent_tools.cli:_setup_doctor_row", False, (),
    ),
    commands.Command(
        "install", "setup", "sync the checkout's venv, install cox and write a profile for this machine",
        (
            commands.Arg(("--root",)), commands.Arg(("--team",), {"required": True}), commands.Arg(("--workspace",)),
            commands.Arg(("--home",), {"help": "absolute path of the coxswain home; used when --root is omitted"}),
            commands.Arg(("--provider-profile",)), commands.Arg(("--skills-root",)), commands.Arg(("--assume",), {"default": "a", "choices": ("a", "r")}),
            commands.Arg(("--plugins",), {"action": "store_true"}), commands.Arg(("--hook",), {"action": "store_true"}),
            commands.Arg(("--force-profile",), {"action": "store_true"}), commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--window-ceiling-usd",), {"type": float, "default": None, "dest": "window_ceiling_usd", "help": "write spend: window_ceiling_usd into the profile"}),
            commands.Arg(("--weekly-ceiling-usd",), {"type": float, "default": None, "dest": "weekly_ceiling_usd", "help": "write spend: weekly_ceiling_usd into the profile"}),
        ),
        "agent_tools.cli:_setup_install", False, (),
    ),
    commands.Command(
        "extras", "setup", "install the optional extras into this venv, pinned and hash-checked from uv.lock",
        (
            commands.Arg(("--with",), {"nargs": "+", "dest": "with_extras", "metavar": "EXTRA", "help": "extras to install; default traces parquet lake postgres"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print what would be installed; change nothing"}),
        ),
        "agent_tools.cli:_setup_extras", False, (),
    ),
    commands.Command(
        "migrate", "setup", "plan moving this host to the one monorepo checkout; --apply migrates",
        (
            commands.Arg(("--home",), {"help": "absolute path of the coxswain home to migrate to"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "same as the default: print the plans; change nothing"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "execute the plans; without it the command only prints them"}),
            commands.Arg(("--hosts",), {"action": "store_true", "help": "with --apply, also run the host steps; use it on the chair's machine"}),
            commands.Arg(("--finish",), {"action": "store_true", "help": "own mode, after every host migrated: rewrite queue paths, remove old links"}),
            commands.Arg(("--old-root",), {"metavar": "PATH", "help": "with --finish, the old checkout root when no marker names it"}),
            commands.Arg(("--to",), {"metavar": "VERSION", "help": "the release the umbrella checkout becomes; default the newest"}),
        ),
        "agent_tools.cli:_setup_migrate", False, (),
    ),
    commands.Command(
        "store", "setup", "configure the shared Postgres store for run records",
        (
            commands.Arg(("--store",), {"help": "shared; the files store is retired and is refused by name"}),
            *_STORE_FLAGS,
            commands.Arg(("--allow-unreachable",), {"action": "store_true", "help": "exit 0 even when a probe fails"}),
        ),
        "agent_tools.cli:_setup_store", False, (),
        subcommands=(
            commands.Command(
                "switch", "setup", "copy an old local run store and its traces into the shared store, then record store.mode",
                (commands.Arg(("target",), {"choices": ("files", "shared")}), *_suppressed(_STORE_FLAGS)),
                "agent_tools.cli:_setup_store_switch", False, (),
            ),
            commands.Command(
                "worker-role", "setup", "create or reshape one worker's scoped store role; run with the owner's store URL",
                (
                    commands.Arg(("name",), {"help": "the worker's name, as `cox host add` knows it; the role is cox_w_<name>"}),
                    commands.Arg(("--environment-file",), {"help": "the units' env file that gets the role and its password (default: ~/.config/agent-tools/garage.env)"}),
                    commands.Arg(("--revoke-public-create",), {"action": "store_true", "help": "also revoke CREATE on the store's schema from PUBLIC, which Postgres before 15 grants by default"}),
                ),
                "agent_tools.cli:_setup_worker_role", False, (),
            ),
        ),
        sub_dest="store_cmd", sub_required=False,
    ),
    commands.Command(
        "unit-env", "setup", "write the one env file the chair, lander and upgrade units read, with the stored token",
        (
            commands.Arg(("--environment-file",), {"help": "the file to write (default: ~/.config/agent-tools/garage.env, the one the units read)"}),
        ),
        "agent_tools.cli:_setup_unit_env", False, (),
    ),
    commands.Command(
        "token", "setup", "store the long-lived Claude token that coxswain's own claude runs use",
        (
            commands.Arg(("--token",), {"help": "the token printed by `claude setup-token`; prefer --from-file, a flag lands in shell history"}),
            commands.Arg(("--from-file",), {"metavar": "PATH", "help": "read the token from this file; else asked with hidden input in a terminal"}),
        ),
        "agent_tools.cli:_setup_token", False, (),
        subcommands=(
            commands.Command(
                "status", "setup", "report whether the stored token file exists and claude accepts it",
                (commands.Arg(("--probe",), {"action": "store_true", "help": "also send one small claude request; exits 1 when it fails"}),),
                "agent_tools.cli:_setup_token_status", False, (),
            ),
        ),
        sub_dest="token_cmd", sub_required=False,
        description=(
            "The token comes from `claude setup-token`.\n"
            "It is stored mode 600 in coxswain's config folder.\n"
            "Interactive claude sessions are untouched: only coxswain's own runs read the file."
        ),
    ),
]


_PROFILE_ARG = commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"})
LANDER_GROUP = commands.Group(
    name="lander", help="the landing worker: merges built phases and lands whole initiatives",
    description=(
        "The landing worker. It runs beside the chair and owns the land queue: a built phase is merged into its "
        "initiative branch, and a whole initiative goes to its parent as one pull request."
    ),
    epilog="examples:\n  cox lander status\n  cox lander adopt\n  cox lander adopt --apply\n  cox lander once\n  cox lander run --interval 30\n  cox lander service --install",
)
LANDER_COMMANDS = [
    commands.Command(
        "once", "lander", "one pass: queue what is ready, then merge and land within the caps",
        (_PROFILE_ARG,), "agent_tools.cli:_lander_once", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "run", "lander", "pass after pass until interrupted",
        (commands.Arg(("--interval",), {"type": float, "default": 30.0, "help": "seconds between passes (default: 30)"}), _PROFILE_ARG),
        "agent_tools.cli:_lander_run", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "status", "lander", "every initiative in flight with its phases, and the land queue with how long each item has waited",
        (commands.Arg(("--json",), {"action": "store_true", "help": "print the status as JSON"}), _PROFILE_ARG),
        "agent_tools.cli:_lander_status", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "adopt", "lander", "bring initiatives that were in flight before the landing record under it; a dry run unless --apply",
        (
            commands.Arg(("initiative",), {"nargs": "*", "help": "initiative ids (default: every initiative with no landing record)"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "do it; without this flag only the report is printed"}),
            _PROFILE_ARG,
        ),
        "agent_tools.cli:_lander_adopt", False, (), defaults={"profile": None},
        description=(
            "For each initiative with no landing record: work already on the parent is marked done, an approval off "
            "its phase branch is carried onto it, one that cannot be found goes back to ready, the initiative branch "
            "is kept or cut, and every built phase is merged into it.\nNothing is deleted, reset or rebased, and an "
            "initiative that has a record is skipped, so it is safe to run twice."
        ),
    ),
    commands.Command(
        "backlog", "lander", "mark the tasks a hand-folded pull request carried as done, once it has merged; a dry run unless --apply",
        (
            commands.Arg(("--manifest",), {"required": True, "help": "the backlog manifest (prereq-backlog-manifest.json)"}),
            commands.Arg(("--pr",), {"required": True, "help": "the pull request that carried the manifest's tasks (URL)"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "do it; refused until the forge says the pull request merged"}),
            _PROFILE_ARG,
        ),
        "agent_tools.cli:_lander_backlog", False, (), defaults={"profile": None},
        description=(
            "Each task the manifest lists as applied moves from approved to done.\nA task already done, or in any other "
            "state, is skipped, so it is safe to run twice."
        ),
    ),
    commands.Command(
        "service", "lander", "write or show the systemd user unit; never runs systemctl",
        (
            commands.Arg(("--install",), {"action": "store_true", "help": "write the unit and print the systemctl lines to run"}),
            commands.Arg(("--status",), {"action": "store_true", "help": "print the unit as it is on disk"}),
            commands.Arg(("--interval",), {"type": int, "default": 30, "help": "seconds between passes (default: 30)"}),
            commands.Arg(("--environment-file",), {"help": "an EnvironmentFile for the unit (default: the chair's)"}),
            _PROFILE_ARG,
        ),
        "agent_tools.cli:_lander_service", False, (), defaults={"profile": None},
    ),
]

_WORKER_ARGS = (
    commands.Arg(("--name",), {"help": "the worker's name, its hosts row's (default: this machine's hostname)"}),
    commands.Arg(("--capability",), {"action": "append", "metavar": "TOKEN",
                                     "help": "a capability the operator declares, e.g. os:macos or toolchain:go; repeatable"}),
    commands.Arg(("--lanes",), {"type": int, "help": "phases it runs at once (default: its hosts row's capacity, else 1)"}),
    _PROFILE_ARG,
)
WORKER_GROUP = commands.Group(
    name="worker", help="the phase worker: claims ready phases from the phase queue and runs them here",
    description=(
        "The phase worker. The chair queues each ready phase of a phase-model initiative; a worker claims the next one "
        "it qualifies for, runs it through the harness on this machine, and keeps its claim alive while it runs.\n"
        "Nothing reaches a worker over ssh: it pulls."
    ),
    epilog="examples:\n  cox worker status\n  cox worker once\n  cox worker run --capability os:macos",
)
WORKER_COMMANDS = [
    commands.Command(
        "run", "worker", "claim and run phases until interrupted", _WORKER_ARGS,
        "agent_tools.cli:_worker_run", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "once", "worker", "one claim pass, then run what it claimed to its end",
        _WORKER_ARGS, "agent_tools.cli:_worker_once", False, (), defaults={"profile": None},
    ),
    commands.Command(
        "status", "worker", "every queued phase with its claim, and every registered worker",
        (commands.Arg(("--json",), {"action": "store_true", "help": "print the status as JSON"}), _PROFILE_ARG),
        "agent_tools.cli:_worker_status", False, (), defaults={"profile": None},
    ),
]

CHAIR_GROUP = commands.Group(
    name="chair", help="the chair loop that runs the landing and launch ticks itself",
    description="The chair loop that runs the landing and launch ticks itself.",
    epilog='examples:\n  cox chair run --once --dry-run\n  cox chair run --interval 30\n  cox chair service --install\n  cox chair ask "ship it?" --option yes --option no',
)
CHAIR_COMMANDS = [
    commands.Command(
        "run", "chair", "beat, gather, plan, perform and report every tick until interrupted",
        (
            commands.Arg(("--once",), {"action": "store_true", "help": "run one tick, release the lease, exit 1 if the tick errored"}),
            commands.Arg(("--interval",), {"type": float, "default": _CHAIR_RUN_INTERVAL, "help": "seconds between ticks (default: 60)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "plan and report each tick without taking the lease or performing any action"}),
            commands.Arg(("--as-holder",), {"action": "store_true", "help": "with --dry-run, plan as the lease holder would while the loop holds the lease; takes no lease and performs nothing"}),
            commands.Arg(("--max-overruns",), {"type": int, "default": 3, "help": "exit 3 after this many facts-stage overruns in a row (default: 3; 0 disables)"}),
            commands.Arg(("--label",), {"help": "the chair's holder label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_chair_run", False, (),
        defaults={"profile": None},
        description=(
            "Each tick beats the chair lease, gathers facts, plans, performs and reports one status line.\n"
            "It yields to `cox route chair take --steal`: actions planned under a lost lease are fenced.\n"
            "It hard-stops at the profile's weekly_hard_stop_fraction of the weekly ceiling."
        ),
    ),
    commands.Command(
        "service", "chair", "write or show the systemd user unit that keeps `cox chair run` alive",
        (
            commands.Arg(("--install",), {"action": "store_true", "help": "write the unit file and print the systemctl lines to run; runs nothing itself"}),
            commands.Arg(("--status",), {"action": "store_true", "help": "print the unit file, or say there is none"}),
            commands.Arg(("--label",), {"help": "the chair's holder label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--interval",), {"type": int, "default": 60, "help": "seconds between ticks (default: 60)"}),
            commands.Arg(("--environment-file",), {"help": "env file the unit loads (default: ~/.config/agent-tools/garage.env when it exists)"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming workspace_dir, pinned in the unit's ExecStart (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--host",), {"help": "a hosts-table name: install or report on that lane host over ssh"}),
            commands.Arg(("--apply",), {"action": "store_true", "help": "with --install --host, run the steps instead of printing them"}),
        ),
        "agent_tools.cli:_chair_service", False, (),
        description=(
            "--install writes ~/.config/systemd/user/coxswain-chair.service and prints the systemctl lines.\n"
            "It does not run systemctl."
        ),
    ),
    commands.Command(
        "ask", "chair", "put a question with its options to the operator on the courier bus",
        (
            commands.Arg(("question",)),
            commands.Arg(("--option",), {"action": "append", "default": [], "help": "an answer the operator may give; repeat for each, at least two"}),
            commands.Arg(("--context",), {"default": "", "help": "text or a URL that helps the operator decide"}),
            commands.Arg(("--label",), {"help": "the chair's label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_chair_ask", False, (),
    ),
    commands.Command(
        "attach", "chair", "say whether a live chair loop holds the lease; exit 0 if so, 1 if not",
        (
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--lend-routine",), {"action": "store_true", "help": "mark the routine ticks lent in chair.json; the live holder only"}),
            commands.Arg(("--recall-routine",), {"action": "store_true", "help": "remove the lent mark from chair.json; the live holder only"}),
        ),
        "agent_tools.cli:_chair_attach", False, (),
        description=(
            "Reads the chair lease and the loop's last tick.\n"
            "It writes only under --lend-routine and --recall-routine, which set or clear routine_lent in chair.json for the live holder.\n"
            "It never takes, steals or releases the lease."
        ),
        exclusive=("--lend-routine", "--recall-routine"),
    ),
    commands.Command(
        "answer", "chair", "answer an open decision with one of its options",
        (commands.Arg(("id",)), commands.Arg(("option",)), commands.Arg(("--profile",))),
        "agent_tools.cli:_chair_answer", False, (),
    ),
    commands.Command(
        "decisions", "chair", "list decisions asked of the operator and their answers",
        (
            commands.Arg(("--open",), {"action": "store_true", "help": "only decisions without an answer"}),
            commands.Arg(("--answered",), {"action": "store_true", "help": "only decisions with an answer"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_chair_decisions", False, (),
        exclusive=("--open", "--answered"),
    ),
    commands.Command(
        "watchdog", "chair", "one-shot loop check for a five-minute timer; reads only the store",
        (
            commands.Arg(("--idle-minutes",), {"type": int, "default": 30, "help": "minutes of idle status lines before an alert (default: 30)"}),
            commands.Arg(("--stale-minutes",), {"type": int, "default": 10, "help": "minutes without a lease beat before the loop is down (default: 10)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print each alert and push nothing"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--install",), {"action": "store_true", "help": "write the timer files and print the lines that activate them; runs nothing itself"}),
            commands.Arg(("--uninstall",), {"action": "store_true", "help": "remove the timer files this platform's install wrote"}),
            commands.Arg(("--interval-minutes",), {"type": int, "default": 5, "help": "minutes between checks in the installed timer (default: 5)"}),
        ),
        "agent_tools.cli:_chair_watchdog", False, (),
        exclusive=("--install", "--uninstall"),
        description=(
            "A one-shot check meant for a five-minute timer.\n"
            "It reads only the store and exits 0 whether or not it pushed.\n"
            "It exits 1 only when the store cannot be read.\n"
            "--install and --uninstall only write or remove files: no check runs and no launchd or systemd tool does."
        ),
    ),
    commands.Command(
        "freeze-lands", "chair", "hold every land for a release cut, show the freeze, or lift it",
        (
            commands.Arg(("--for",), {"type": int, "dest": "minutes", "metavar": "MINUTES", "help": "minutes the freeze lasts; needs --reason"}),
            commands.Arg(("--reason",), {"metavar": "TEXT", "help": "why lands are frozen; needs --for"}),
            commands.Arg(("--off",), {"action": "store_true", "help": "lift the freeze now; not with --for or --reason"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        "agent_tools.cli:_chair_freeze_lands", False, (),
        defaults={"profile": None},
        description=(
            "With --for and --reason, freeze lands until the minutes pass.\n"
            "With --off, lift the freeze.\n"
            "With neither, show whether lands are frozen.\n"
            "A freeze that outlives its minutes lifts itself."
        ),
    ),
]

ROUTER_GROUP = commands.Group(
    name="router", help="the routing-profile flag and the tier decision it gates",
    description="The routing-profile flag and the tier decision it gates.",
    epilog="examples:\n  cox router select --role build\n  cox router select --role build --json",
)
ROUTER_COMMANDS = [
    commands.Command(
        "select", "router", "the effective tier for a role under the profile's router: off|shadow|on flag",
        (
            commands.Arg(("--role",), {"required": True}),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile and the router flag (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_router_select", False, (),
    ),
]

STEWARD_GROUP = commands.Group(
    name="steward", help="ceiling-change candidates from stats.db, proposed as intake files",
    description="Ceiling-change candidates from stats.db, proposed as intake files.",
    epilog="examples:\n  cox steward propose\n  cox steward propose --json\n  cox steward draft --json",
)
STEWARD_COMMANDS = [
    commands.Command(
        "propose", "steward", "write each candidate clearing the evidence bar as a new intake file; never edits a provider profile",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_steward_propose", False, (),
    ),
    commands.Command(
        "draft", "steward", "write each grounded proposal in intake as a draft initiative under work/, list those that exist or could not be grounded",
        (
            commands.Arg(("--profile",), {"help": "the routing profile naming the workspace (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_steward_draft", False, (),
    ),
]


def _parse_login_ok(text: str) -> bool | None:
    """`true`, `false` or `none`, any case; the one place the login verb's tri-state is read."""
    value = {"true": True, "false": False, "none": None}
    if text.strip().lower() not in value:
        raise argparse.ArgumentTypeError(f"login_ok must be true, false or none, not {text!r}")
    return value[text.strip().lower()]


HOST_GROUP = commands.Group(
    name="host", help="the hosts table: lane hosts, their capacity, versions and login",
    description="The hosts table: lane hosts, their capacity, versions and login.",
    epilog="examples:\n  cox host add host-a --ssh host-a --capacity 8\n  cox host list\n  cox host beat\n  cox host sync host-a",
    args=(commands.Arg(("--profile",)),),
)
HOST_COMMANDS = [
    commands.Command(
        "add", "host", "add or update a host in the table",
        (
            commands.Arg(("name",)), commands.Arg(("--ssh",), {"required": True}),
            commands.Arg(("--capacity",), {"type": int, "required": True, "help": "lanes the host may run at once"}),
            commands.Arg(("--weight",), {"type": int, "default": 1, "help": "share of new placements"}),
            commands.Arg(("--capabilities",), {"default": "", "help": "comma-separated, e.g. go,rust"}),
        ),
        "agent_tools.cli:_host_add", False, (),
    ),
    commands.Command("list", "host", "one line per host: state, capacity, beat age, login", (), "agent_tools.cli:_host_list", False, ()),
    commands.Command("drain", "host", "stop launching on a host; its live lanes finish", (commands.Arg(("name",)),), "agent_tools.cli:_host_drain", False, ()),
    commands.Command("deploy-only", "host", "receive releases but never lanes", (commands.Arg(("name",)),), "agent_tools.cli:_host_deploy_only", False, ()),
    commands.Command(
        "remove", "host", "delete a drained idle host from the hosts table",
        (commands.Arg(("name",)), commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the row that would be removed; write nothing"})),
        "agent_tools.cli:_host_remove", False, (),
    ),
    commands.Command("activate", "host", "make a host a lane host again", (commands.Arg(("name",)),), "agent_tools.cli:_host_activate", False, ()),
    commands.Command(
        "orchestrate", "host", "show, or turn on or off, orchestration on this machine",
        (commands.Arg(("mode",), {"nargs": "?", "choices": ["on", "off"], "help": "omit to show the current mode"}),),
        "agent_tools.cli:_host_orchestrate", False, (),
    ),
    commands.Command(
        "capacity", "host", "change only a host's capacity, leaving ssh, weight and capabilities as they are",
        (
            commands.Arg(("name",)),
            commands.Arg(("n",), {"type": int, "help": "lanes the host may run at once; 0 suspends new placement"}),
            commands.Arg(("--json",), {"action": "store_true", "help": "print the row as JSON"}),
        ),
        "agent_tools.cli:_host_capacity", False, (),
    ),
    commands.Command(
        "beat", "host", "record this machine's versions and claude login in the table",
        (commands.Arg(("name",), {"nargs": "?", "help": "default: this machine's hostname"}),), "agent_tools.cli:_host_beat", False, (),
    ),
    commands.Command(
        "login", "host", "store a host's login state without touching its beat time",
        (
            commands.Arg(("name",)),
            commands.Arg(("login_ok",), {"type": _parse_login_ok, "metavar": "{true,false,none}"}),
            commands.Arg(("--reason",), {"default": None, "help": "why the check failed"}),
            commands.Arg(("--failures",), {"type": int, "default": 0, "help": "consecutive failed checks"}),
        ),
        "agent_tools.cli:_host_login", False, (),
    ),
    commands.Command("doctor", "host", "the table's line for a host, then its doctor over ssh", (commands.Arg(("name",)),), "agent_tools.cli:_host_doctor", False, ()),
    commands.Command(
        "sync", "host", "bring a host's checkouts to the commits this machine runs (fetch, then fast-forward or check out)",
        (commands.Arg(("name",)),), "agent_tools.cli:_host_sync", False, (),
    ),
]


def _dispatch_fn(target: str | None) -> _LazyHandler | None:
    return None if target is None else _LazyHandler(target)


def _dispatch_row(row: commands.Command) -> commands.Command:
    """The row with its `module:function` handler string, and its children's, bound to a `_LazyHandler`."""
    return dataclasses.replace(row, handler=_dispatch_fn(row.handler), subcommands=tuple(_dispatch_row(c) for c in row.subcommands))


def _dispatch_table(
    table: list[tuple[commands.Group, list[commands.Command]]],
) -> list[tuple[commands.Group, list[commands.Command]]]:
    """Each row and leaf group names its handler as `"dotted.module:function"` data; this binds each one
    to a `_LazyHandler`, so `cox --help` builds the parser without importing any handler's module."""
    return [(dataclasses.replace(group, fn=_dispatch_fn(group.fn)), [_dispatch_row(row) for row in rows]) for group, rows in table]


COMMAND_TABLE: list[tuple[commands.Group, list[commands.Command]]] = _dispatch_table([
    (RUNS_GROUP, RUNS_COMMANDS),
    (COURIER_GROUP, COURIER_COMMANDS),
    (MCP_GROUP, []),
    (VERSIONS_GROUP, []),
    (CONSOLE_GROUP, []),
    (DASH_GROUP, []),
    (SETTINGS_GROUP, SETTINGS_COMMANDS),
    (INSTALL_GROUP, []),
    (UPGRADE_GROUP, []),
    (HOME_GROUP, []),
    (SESSION_GROUP, []),
    (STATS_GROUP, STATS_COMMANDS),
    (LAKE_GROUP, LAKE_COMMANDS),
    (USAGE_GROUP, USAGE_COMMANDS),
    (PLAN_GROUP, PLAN_COMMANDS),
    (EPIC_GROUP, EPIC_COMMANDS),
    (PR_GROUP, PR_COMMANDS),
    (ROUTE_GROUP, ROUTE_COMMANDS),
    (CHAIR_GROUP, CHAIR_COMMANDS),
    (LANDER_GROUP, LANDER_COMMANDS),
    (WORKER_GROUP, WORKER_COMMANDS),
    (HOST_GROUP, HOST_COMMANDS),
    (ROUTER_GROUP, ROUTER_COMMANDS),
    (STEWARD_GROUP, STEWARD_COMMANDS),
    (SETUP_GROUP, SETUP_COMMANDS),
])


def _table_entry(name: str) -> tuple[commands.Group, list[commands.Command]]:
    """`COMMAND_TABLE`'s (Group, rows) pair for `name`, so `build_parser` can
    register each migrated group at its own original position -- interleaved
    among the groups still hand-built -- instead of moving every migrated
    group's registration to wherever the table is folded, which would reorder
    `cox --help`'s top-level listing."""
    return next((group, rows) for group, rows in COMMAND_TABLE if group.name == name)


_TOP_OPTIONS_WITH_VALUE = ("--profile",)
_TOP_FLAGS = ("--no-plugin", "--print-argv")


def _bare_launcher_split(args: list[str]):
    """Splits a bare-`cox` invocation's `-- EXTRA...` tail off before the real
    parser sees it: the top-level subparsers action is a PARSER-style
    positional and would otherwise try to consume the first extra token as an
    (invalid) subcommand name. Returns (head, tail) only when `args`, up to
    any `--`, holds nothing but the launcher's own top-level options; returns
    None the moment a subcommand token (or anything else) appears, so `main`
    leaves that invocation, `--` and all, to the ordinary parser untouched —
    an existing subcommand's own `--` semantics (e.g. `route file -- -title`)
    are not this function's to change."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[:i], args[i + 1 :]
        if tok in _TOP_OPTIONS_WITH_VALUE:
            i += 1
            if i >= len(args):
                return None
        elif tok not in _TOP_FLAGS:
            return None
        i += 1
    return args, []


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args and not sys.stdout.isatty():
        return _route_status(argparse.Namespace(profile=None, json=False, all=False))
    split = _bare_launcher_split(args)
    head, tail = split if split is not None else (args, [])
    a = build_parser().parse_args(head)
    if not hasattr(a, "fn"):
        return _launcher(a, tail)
    refused = _store_refusal(a, head)
    if refused is not None:
        print(f"cox: {refused}", file=sys.stderr)
        return 2
    if reads_board(head):
        _refresh_board_files(a)
    return a.fn(a)


module_facade.install(__name__, _PARTS)


if __name__ == "__main__":
    # Under `python -m`, this module is `__main__`, not `agent_tools.cli`; the table's "agent_tools.cli:..."
    # handlers would otherwise import a second copy of this file at dispatch.
    sys.modules.setdefault("agent_tools.cli", sys.modules[__name__])
    sys.exit(main())
