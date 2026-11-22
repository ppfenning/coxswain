"""Which tasks an active land is landing, read from the live per-task land leases.

A land that knows its initiative takes its lease under `core.task_lease.task_lease_name`. A bare
`land:<task>` lease, a phase land and the repo lease name no task here and claim nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.task_lease import TASK_LEASE_PREFIX, parse_task_lease_name
from harness import store_read
from harness.store_dialect import Connection


def claimed_tasks(lease_rows: Iterable[Mapping[str, Any]], initiative: str, now: str) -> frozenset[str]:
    """Task ids named by `initiative`'s unexpired task-land leases. Timestamps are the store's `YYYY-MM-DDTHH:MM:SSZ`."""
    named = [parse_task_lease_name(row["name"]) for row in lease_rows if row["expires_at"] > now]
    return frozenset(parsed[1] for parsed in named if parsed is not None and parsed[0] == initiative)


def read_claimed_tasks(conn: Connection, initiative: str, now: str) -> frozenset[str]:
    return claimed_tasks(store_read.leases_with_prefix(conn, TASK_LEASE_PREFIX), initiative, now)
