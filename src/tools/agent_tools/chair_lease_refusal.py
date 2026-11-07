from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

STATUS = "lease_held"
LEASE_TTL_S = 120  # mirrors _LEASE_TTL in src/graphs/harness/cli.py

_REFUSAL = re.compile(r"run: (\S+) holds (runs:[^\s;]+); refusing to start (\S+)")


@dataclass(frozen=True)
class LeaseRefusal:
    holder_run: str
    lease: str
    refused_run: str


def parse_lease_refusal(output: str) -> LeaseRefusal | None:
    match = _REFUSAL.search(output)
    if match is None:
        return None
    return LeaseRefusal(*match.groups())


def stuck_due(first_refused_at: datetime, now: datetime, holder_alive: bool, ttl_s: int = LEASE_TTL_S) -> bool:
    """A dead holder is never stuck; the TTL boundary is inclusive."""
    return holder_alive and (now - first_refused_at).total_seconds() >= ttl_s
