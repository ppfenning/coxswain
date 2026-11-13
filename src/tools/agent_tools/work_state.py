"""The work-state backend. There is one: the store. `files`, in which the ticket files were the board, is retired."""

import contextlib
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_tools import store_dialect

_PROBE_CONNECT_TIMEOUT_S = 3


def _resolve_mode(explicit: object, shared_store: bool, store_answered: bool) -> str:
    """Pure. Always `store`: the store is the only board. The arguments are kept for the status line's callers;
    none of them can select the retired `files` mode, in which the ticket files were the board."""
    return "store"


def _with_connect_timeout(url: str) -> str:
    """The Postgres URL with a short connect timeout added to its query unless it already sets one."""
    parts = urllib.parse.urlsplit(url)
    if "connect_timeout" in urllib.parse.parse_qs(parts.query):
        return url
    extra = f"connect_timeout={_PROBE_CONNECT_TIMEOUT_S}"
    new_query = f"{parts.query}&{extra}" if parts.query else extra
    return urllib.parse.urlunsplit(parts._replace(query=new_query))


def _probe_store(url: str) -> bool:
    """Edge. Opens the Postgres `url` read-only, with a connect timeout, and runs `SELECT 1`;
    any exception is "did not answer". Only ever called with a Postgres URL: appending a
    query string to a SQLite path would corrupt the file path `connect_readonly_url` builds
    from it, so a non-Postgres URL is never routed here. A failure to open, query or close
    all read as "did not answer"."""
    try:
        with contextlib.closing(store_dialect.connect_readonly_url(_with_connect_timeout(url))) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


FILES_RETIRED = "work_state: files is retired: the store is the only board. Remove the key from the provider profile."


def resolve(profile: Mapping[str, Any]) -> tuple[str, str]:
    """Edge. The mode, always `store`, and its status line, from at most one probe of a Postgres store.
    A store that does not answer is still the board: commands that need it fail and say so, and nothing stands in
    for it. A profile that still says `work_state: files` gets a line saying the key is retired."""
    url = profile.get("storage_url") or ""
    try:
        shared_store = store_dialect.is_postgres(url)
    except ValueError:
        shared_store = False
    if profile.get("work_state") == "files":
        return "store", f"work state: store ({FILES_RETIRED})"
    if shared_store and not _probe_store(url):
        return "store", "work state: store (unreachable)"
    return "store", "work state: store"


def work_state_mode(profile: Mapping[str, Any]) -> str:
    """Always `store`. Opens no connection: whether the store answers changes the status line, never the mode."""
    return "store"


def work_state_mode_at(provider_profile: Path | str) -> str:
    """The mode for the provider profile at `provider_profile`: always `store`."""
    return "store"
