"""Per-lane test database names: pure and clock-free, plain data in, plain data out."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

MAX_DB_NAME = 63
_HASH_LEN = 8


def database_name(url: str) -> str:
    """The last path segment of a database URL."""
    return url.rsplit("/", 1)[-1]


def _slug(task: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", task.lower()).strip("_")


def _lane_hash(run_id: str, phase: str, task: str) -> str:
    return hashlib.sha256(f"{run_id}/{phase}/{task}".encode()).hexdigest()[:_HASH_LEN]


def lane_database_url(template: str, run_id: str, phase: str, task: str) -> str:
    """Fill the one `{lane}` in the template; the literal text is never cut, the task part of the slug is.

    When the literal text leaves less room than the hash, the hash is kept whole and the name runs over the cap.
    """
    stable = _lane_hash(run_id, phase, task)
    room = MAX_DB_NAME - len(database_name(template).replace("{lane}", "")) - len(stable) - 1
    head = _slug(task)[: max(room, 0)].rstrip("_")
    slug = f"{head}_{stable}" if head else stable
    return template.replace("{lane}", slug, 1)


def lane_env(setting: Mapping[str, str] | None, run_id: str, phase: str, task: str) -> dict[str, str]:
    """`setting` carries `env` (the variable name) and `template` (the URL with `{lane}`)."""
    if setting is None:
        return {}
    return {setting["env"]: lane_database_url(setting["template"], run_id, phase, task)}
