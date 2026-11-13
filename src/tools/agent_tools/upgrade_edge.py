"""The edge upgrade channel, pure: which commit an edge machine moves to, whether it moves, whether a failed move may
be rolled back, the record `cox versions` shows, and the timer that runs it. No file, process, network or clock
access; cli.py reads git, gh and systemd, and moves the checkout by the path `cox upgrade --to` uses.

A machine is on `stable` unless its profile says `upgrade_channel: edge`. Stable upgrades to the newest release tag,
as before. Edge upgrades to the newest commit of main whose CI passed, never to the tip unless the tip passed.

Store migrations are forward only: `harness.store_migrate.migrate` refuses a store newer than the code that opens it.
So once a restarted chair or lander has opened the store at a commit that adds a migration, the previous commit can no
longer open it, and moving back to it would break the machine rather than restore it. `may_roll_back` says no then."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence

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
_MIGRATION = re.compile(r"(?:^|/)store_ddl_(\d{4})\.py$")

SERVICE_NAME = "coxswain-upgrade.service"
TIMER_NAME = "coxswain-upgrade.timer"

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


def may_roll_back(added: Sequence[str], restarted: bool) -> bool:
    """False when the commit moved to adds migrations and a unit was restarted on it: the restarted chair or lander
    applies them when it opens the store, and the previous commit then refuses that store. Without a restart nothing
    on this machine opened the store at the new commit (the doctor reads it read-only), so moving back is safe."""
    return not (added and restarted)


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
    env_lines = [f"EnvironmentFile=-{environment_file}"] if environment_file is not None else []
    lines = [
        "[Unit]",
        "Description=Coxswain edge upgrade: move to the newest green commit on main",
        "",
        "[Service]",
        "Type=oneshot",
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
