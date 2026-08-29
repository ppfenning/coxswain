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

__all__ = ["append", "read", "append_observation"]


def append(*args, **kwargs):
    raise NotImplementedError("See docstring contract; write fresh.")


def read(*args, **kwargs):
    raise NotImplementedError("See docstring contract; write fresh.")


def append_observation(*args, **kwargs):
    raise NotImplementedError("See docstring contract; write fresh.")
