"""Verdict on a checkout's environment from two lock hashes. Pure: the beat edge reads and hashes."""

from collections.abc import Mapping
from typing import TypedDict

from core.jsontypes import is_mapping

CHECKOUT_ENV_KEY = "checkout_env"

# The lockfile the beat edge hashes. A repo without it, a Rust repo say, records lock_hash None,
# reads fresh, and is never shown SYNC_COMMAND.
LOCKFILE_NAME = "uv.lock"

# Not `uv sync --locked`: it errors whenever uv.lock lags pyproject, as on a version bump.
# dev, postgres and lake are [project.optional-dependencies] extras, not a dependency group, and
# uv sync is exact, so a sync without --all-extras uninstalls pytest and psycopg.
# --frozen installs exactly what the hashed lockfile pins, without re-locking.
SYNC_COMMAND = "uv sync --frozen --all-extras"

_BLOCKING = frozenset({"stale", "never_synced"})


class CheckoutEnvEntry(TypedDict):
    """One repo's record under versions_json[CHECKOUT_ENV_KEY]; None lock_hash means no lockfile."""

    lock_hash: str | None
    synced_hash: str | None


def env_verdict(synced_hash: str | None, current_hash: str | None) -> str:
    if current_hash is None:
        return "fresh"
    if synced_hash is None:
        return "never_synced"
    if synced_hash == current_hash:
        return "fresh"
    return "stale"


def host_env_verdict(versions: Mapping[str, object], repo: str) -> str:
    entries = versions.get(CHECKOUT_ENV_KEY)
    if not is_mapping(entries):
        return "unknown"
    entry = entries.get(repo)
    if not is_mapping(entry):
        return "unknown"
    synced, lock = entry.get("synced_hash"), entry.get("lock_hash")
    return env_verdict(synced if isinstance(synced, str) else None, lock if isinstance(lock, str) else None)


def blocks_launch(verdict: str) -> bool:
    return verdict in _BLOCKING


def sync_instruction(host: str, repo: str) -> str:
    return f"on {host}, in {repo}, run: {SYNC_COMMAND}"
