"""Which tasks an active land is landing, read from the live per-task land leases.

A land that knows its initiative takes its lease under `agent_tools.land_lease.task_lease_name`. A bare
`land:<task>` lease, a phase land and the repo lease name no task here and claim nothing.

agent_tools is imported inside the functions: the harness stays importable without it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from harness import store_read
from harness.store_dialect import Connection


def claimed_tasks(lease_rows: Iterable[Mapping[str, Any]], initiative: str, now: str) -> frozenset[str]:
    """Task ids named by `initiative`'s unexpired task-land leases. Timestamps are the store's `YYYY-MM-DDTHH:MM:SSZ`."""
    from agent_tools.land_lease import parse_task_lease_name

    named = [parse_task_lease_name(row["name"]) for row in lease_rows if row["expires_at"] > now]
    return frozenset(parsed[1] for parsed in named if parsed is not None and parsed[0] == initiative)


def read_claimed_tasks(conn: Connection, initiative: str, now: str) -> frozenset[str]:
    from agent_tools.land_lease import TASK_LEASE_PREFIX

    return claimed_tasks(store_read.leases_with_prefix(conn, TASK_LEASE_PREFIX), initiative, now)
