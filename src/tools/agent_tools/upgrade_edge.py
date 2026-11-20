"""The edge upgrade channel, pure: which commit an edge machine moves to, whether it moves, whether a failed move may
be rolled back, the record `cox versions` shows, and the timer that runs it. No file, process, network or clock
access; cli.py reads git, gh and systemd, and moves the checkout by the path `cox upgrade --to` uses.

A machine is on `stable` unless its profile says `upgrade_channel: edge`. Stable upgrades to the newest release tag,
as before. Edge upgrades to the newest commit of main whose CI passed, never to the tip unless the tip passed.

Store migrations are forward only: `harness.store_migrate.migrate` refuses a store newer than the code that opens it,
unless every newer migration it records is additive (`harness.store_migrate.accepts`). So once a restarted chair or
lander has opened the store at a commit that adds a migration, the previous commit can no longer open it, and moving
back to it would break the machine rather than restore it. `may_roll_back` says no then, except when every added
migration declares `ADDITIVE = True` and the previous commit carries that rule itself."""

from __future__ import annotations

import ast
import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import PurePath
from xml.sax.saxutils import escape

from agent_tools import chair_service, unit_env

CHANNELS = ("stable", "edge")
DEFAULT_CHANNEL = "stable"
PROFILE_KEY = "upgrade_channel"

# CI passing means this workflow's push run on main concluded success: .github/workflows/ci.yml runs every suite,
# ruff and the docs build. docs.yml also runs on main, but it publishes the site and tests nothing.
WORKFLOW = "ci.yml"
BRANCH = "main"
HISTORY = 200  # first-parent commits of origin/main searched for a green one, newest first

RECORD_FILE = "edge-upgrade.json"  # in the installs folder, beside the checkout, so `cox versions` finds it
MIGRATIONS_DIR = "src/graphs/harness/"
MIGRATE_MODULE = f"{MIGRATIONS_DIR}store_migrate.py"  # where the rule that tolerates an additive newer store lives
_MIGRATION = re.compile(r"(?:^|/)store_ddl_(\d{4})\.py$")

SERVICE_NAME = "coxswain-upgrade.service"
TIMER_NAME = "coxswain-upgrade.timer"
PLIST_LABEL = "coxswain-upgrade"
PLIST_NAME = "coxswain-upgrade.plist"
# systemd kills a scheduled upgrade still running after this long; each git and gh call inside has its own bound too.
SERVICE_TIMEOUT = "30min"

MOVE, UNCHANGED, AHEAD, HELD = "move", "unchanged", "ahead", "held"
UPGRADED, ROLLED_BACK, ROLLBACK_FAILED, NOT_ROLLED_BACK = "upgraded", "rolled_back", "rollback_failed", "not_rolled_back"
_FAILED = frozenset({ROLLED_BACK, ROLLBACK_FAILED, NOT_ROLLED_BACK})
_SHORT = 12


def channel(profile: Mapping) -> str:
    """The profile's `upgrade_channel`; stable when it names none."""
    return profile.get(PROFILE_KEY) or DEFAULT_CHANNEL


def newest_green(first_parent: Sequence[str], green: Collection[str]) -> str | None:
    """The first commit of `first_parent` (main's history, newest first) whose CI run passed, else None. The tip is
    chosen only when it passed itself."""
    passed = frozenset(green)
    return next((sha for sha in first_parent if sha in passed), None)


def decide(head: str, target: str, first_parent: Sequence[str], record: Mapping | None) -> str:
    """UNCHANGED at the target; AHEAD when `head` is a newer commit of main than the target, since an edge upgrade
    never moves back; HELD when the last edge upgrade failed at this same target, so a broken commit is not retried
    every tick; else MOVE. `head` off main's first parents (a release commit, say) moves."""
    if head == target:
        return UNCHANGED
    if head in first_parent and target in first_parent and first_parent.index(head) < first_parent.index(target):
        return AHEAD
    if record and record.get("chosen") == target and record.get("outcome") in _FAILED:
        return HELD
    return MOVE


def needs_resync(verdict: str, venv_check_code: int) -> bool:
    """Whether a run that moves nothing must still sync the venv: `uv sync --check` said the venv does not match the
    checkout (exit 1). A check that timed out (124) or could not run (127) is not evidence either way."""
    return verdict != MOVE and venv_check_code == 1


def verdict_line(verdict: str, head: str, target: str, record: Mapping | None = None) -> str:
    """What a run that does not move prints."""
    if verdict == UNCHANGED:
        return f"edge: already at {target[:_SHORT]}, the newest green commit; nothing to do"
    if verdict == AHEAD:
        return (f"edge: {head[:_SHORT]} is newer on main than the newest green commit {target[:_SHORT]}; "
                "an edge upgrade never moves back")
    at = (record or {}).get("at", "?")
    return (f"edge: {target[:_SHORT]} was rolled back or failed at {at}; waiting for a newer green commit "
            f"(cox upgrade --to {target} tries it again)")


def migrations(listing: Sequence[str]) -> frozenset[str]:
    """The `store_ddl_NNNN` numbers in a `git ls-tree --name-only <sha> src/graphs/harness/` listing."""
    return frozenset(m.group(1) for line in listing if (m := _MIGRATION.search(line.strip())))


def added_migrations(before: Sequence[str], after: Sequence[str]) -> tuple[str, ...]:
    """The migration numbers `after` carries that `before` lacks, in order."""
    return tuple(sorted(migrations(after) - migrations(before)))


def migration_path(number: str) -> str:
    """The path, in a commit, of the `store_ddl_<number>` module."""
    return f"{MIGRATIONS_DIR}store_ddl_{number}.py"


def _top_level_assigns(source: str) -> dict[str, ast.expr]:
    """The module-level `NAME = value` assignments in `source`, by name; none when it does not parse."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return {}
    plain = {t.id: n.value for n in body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    annotated = {n.target.id: n.value for n in body
                 if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None}
    return {**plain, **annotated}


def declares_additive(source: str) -> bool:
    """Whether a `store_ddl_NNNN` module's source sets `ADDITIVE = True` at module level, as
    `harness.store_migrate` reads it: anything other than the literal True is not additive."""
    value = _top_level_assigns(source).get("ADDITIVE")
    return isinstance(value, ast.Constant) and value.value is True


def tolerates_additive(source: str) -> bool:
    """Whether a `store_migrate.py` source carries the rule that accepts a store newer only by additive migrations:
    a module-level `accepts` function. Code without it refuses every newer store."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return False
    return any(isinstance(n, ast.FunctionDef) and n.name == "accepts" for n in body)


def blocking_migrations(added: Sequence[str], additive: Collection[str], tolerant: bool) -> tuple[str, ...]:
    """The added migrations the previous commit cannot open a store past: every one when it does not carry the
    additive rule (`tolerant` False), else those not declared additive. In `added` order."""
    return tuple(added) if not tolerant else tuple(n for n in added if n not in additive)


def may_roll_back(added: Sequence[str], restarted: bool, additive: Collection[str], tolerant: bool) -> bool:
    """False when the commit moved to adds a migration the previous commit cannot get past (`blocking_migrations`)
    and a unit was restarted on it: the restarted chair or lander applies it when it opens the store, and the previous
    commit then refuses that store. Without a restart nothing on this machine opened the store at the new commit (the
    doctor reads it read-only), so moving back is safe. Past additive migrations alone, moving back is safe too: the
    previous commit, carrying the rule, opens the store they leave."""
    return not (blocking_migrations(added, additive, tolerant) and restarted)


def record(at: str, previous: str, chosen: str, outcome: str, detail: str = "") -> dict:
    """The last edge upgrade that moved this machine, as written to `RECORD_FILE`."""
    return {"at": at, "previous": previous, "chosen": chosen, "outcome": outcome, "detail": detail}


def record_line(rec: Mapping) -> str:
    """The one line `cox versions` prints for the record."""
    previous, chosen = str(rec.get("previous") or "?"), str(rec.get("chosen") or "?")
    detail = f": {rec['detail']}" if rec.get("detail") else ""
    return (f"edge upgrade {rec.get('at', '?')}: {rec.get('outcome', '?')} "
            f"(previous {previous[:_SHORT]}, chosen {chosen[:_SHORT]}){detail}")


def service_text(cox: str, profile: str, path: str, environment_file: str | None) -> str:
    """The oneshot the timer fires. It runs outside the chair, so restarting the chair does not kill it mid-upgrade."""
    env_lines = [unit_env.environment_file_line(environment_file)] if environment_file is not None else []
    lines = [
        "[Unit]",
        "Description=Coxswain edge upgrade: move to the newest green commit on main",
        "",
        "[Service]",
        "Type=oneshot",
        f"TimeoutStartSec={SERVICE_TIMEOUT}",
        f"ExecStart={cox} upgrade --scheduled --profile {profile}",
        f"Environment=PATH={path}",
        *env_lines,
    ]
    return "\n".join(lines) + "\n"


def timer_text(minutes: int) -> str:
    lines = [
        "[Unit]",
        "Description=Run the Coxswain edge upgrade on a schedule",
        "",
        "[Timer]",
        f"OnBootSec={minutes}min",
        f"OnUnitActiveSec={minutes}min",
        f"Unit={SERVICE_NAME}",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]
    return "\n".join(lines) + "\n"


def plist_text(cox: str, profile: str, path: str, minutes: int, environment_file: str) -> str:
    """The launchd agent a macOS host upgrades on: the same command as the systemd service, every `minutes`, run
    through `unit_env.launchd_arguments` so it sources the one env file at launch. Every value is XML-escaped."""
    arguments = chair_service.program_arguments(environment_file, [cox, "upgrade", "--scheduled", "--profile", profile])
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key>",
        f"  <string>{escape(PLIST_LABEL)}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
        *(f"    <string>{escape(argument)}</string>" for argument in arguments),
        "  </array>",
        "  <key>StartInterval</key>",
        f"  <integer>{minutes * 60}</integer>",
        *chair_service.environment_variable_lines(path),
        "</dict>",
        "</plist>",
    ]
    return "\n".join(lines) + "\n"


def schedule_paths[Home: PurePath](platform: str, home: Home) -> list[Home]:
    """Where the upgrade schedule lives on `platform`: the launchd agent on darwin, the service and timer elsewhere."""
    if platform == "darwin":
        return [home / "Library" / "LaunchAgents" / PLIST_NAME]
    unit_dir = home / ".config" / "systemd" / "user"
    return [unit_dir / SERVICE_NAME, unit_dir / TIMER_NAME]


def schedule_files[Home: PurePath](
    platform: str, home: Home, cox: str, profile: str, path: str, minutes: int, environment_file: str | None,
) -> list[tuple[Home, str]]:
    """Each file `cox upgrade --install-timer` writes, in `schedule_paths` order. Both platforms read the one env
    file: systemd by its EnvironmentFile= line, launchd by sourcing it at launch (the default file when none is given,
    so the agent picks it up once `cox setup unit-env` writes it)."""
    if platform == "darwin":
        texts = [plist_text(cox, profile, path, minutes, environment_file or unit_env.env_file_path(str(home)))]
    else:
        texts = [service_text(cox, profile, path, environment_file), timer_text(minutes)]
    return list(zip(schedule_paths(platform, home), texts, strict=True))


def activation_lines(platform: str, first: PurePath) -> list[str]:
    """What the operator runs to start the schedule; the install itself runs no launchd or systemd tool."""
    if platform == "darwin":
        return [f"launchctl bootstrap gui/$(id -u) {first}"]
    return ["systemctl --user daemon-reload", f"systemctl --user enable --now {TIMER_NAME}"]
