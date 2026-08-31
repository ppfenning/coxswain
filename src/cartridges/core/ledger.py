"""Append-only run ledger — the only persistent state, and the I/O edge.

CONTRACT (implement against this; see docs/CLEAN-ROOM.md — write it fresh)

    append(rows, path) -> None
    read(path) -> tuple[dict, ...]
    append_observation(row, path) -> None   # post-hoc detector hits

One JSON object per line. Append only; never rewrite history. A row records
what a graph proposed, what the human decided, and what actually happened:

    {run_id, ts, principal, kind, risk, outcome, cartridge_sha, provider_profile}

`outcome` is one of:
    clean     applied exactly as proposed
    reversal  the human edited or refused it
    skipped   approved but never executed
    failure   applied, then a detector found it was wrong

`principal` is the GRAPH name, never a person.

This module is deliberately tiny. Everything interesting is in policy.py, which
is pure and takes these rows as input.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = ["append", "read", "append_observation", "LedgerError", "OUTCOMES", "REQUIRED_FIELDS"]

OUTCOMES = frozenset({"clean", "reversal", "skipped", "failure"})
REQUIRED_FIELDS = ("run_id", "ts", "principal", "kind", "risk", "outcome", "cartridge_sha", "provider_profile")


class LedgerError(Exception):
    """A row was refused, or the ledger on disk could not be read."""


def _validate(row: Mapping[str, Any]) -> dict[str, Any]:
    missing = [f for f in REQUIRED_FIELDS if f not in row]
    if missing:
        raise LedgerError(f"ledger row missing required field(s): {', '.join(missing)}")
    if row["outcome"] not in OUTCOMES:
        raise LedgerError(f"unknown outcome {row['outcome']!r}; expected one of {sorted(OUTCOMES)}")
    return dict(row)


def append(rows: Iterable[Mapping[str, Any]], path: Path | str) -> None:
    """Append rows as JSON lines. Validates every row BEFORE opening the file.

    All-or-nothing on purpose: a partial append would leave the ledger holding
    half a run, and the ledger is the one thing downstream policy trusts.
    """
    validated = [_validate(row) for row in rows]
    if not validated:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in validated:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def read(path: Path | str) -> tuple[dict[str, Any], ...]:
    """Read the ledger oldest-first. A missing ledger is empty, not an error."""
    path = Path(path)
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path}:{number}: not valid JSON: {exc}") from exc
    return tuple(rows)


def append_observation(row: Mapping[str, Any], path: Path | str) -> None:
    """Record a post-hoc detector hit — the `failure` outcome.

    Separate from `append` because it arrives LATER, from something that went
    looking after the fact. A run cannot report its own failure here; that is
    the entire point of measuring after the gate rather than at it.
    """
    append([{**row, "outcome": "failure"}], path)
