"""Run manifests: what a run proposed, what the gate decided, what it cost.

CONTRACT (implement against this; see docs/CLEAN-ROOM.md — write it fresh)

    build_manifest(...) -> dict          # pure
    gate_diff(proposal, decision, applied, edited) -> dict   # pure
    agreement_rate(manifest) -> float    # pure
    record_run(manifest, ...) -> None    # THE I/O EDGE: writes runs/ + ledger

Design rules:

1.  Everything except `record_run` is pure and unit-testable with plain dicts.
2.  `record_run` DERIVES ledger outcomes from the gate diffs. The caller does
    not get to assert "this was clean" — clean is computed from whether the
    human edited it. Self-reported success is not evidence.
3.  `build_manifest` hashes the RESOLVED cartridge, so a run is permanently
    attributable to the exact rules it ran under.
4.  `human_minutes` is run-level and entered AT THE GATE, not reconstructed
    later. A time saving recalled a month afterwards convinces nobody, least
    of all the person who has to fund the next quarter of this.
5.  `agreement_rate` answers "how often was it right" from gate diffs alone.

Shell duty after a run is two calls — build_manifest, then record_run — not a
convention someone has to remember.
"""

from __future__ import annotations

__all__ = ["build_manifest", "gate_diff", "agreement_rate", "record_run"]


def build_manifest(*args, **kwargs):
    raise NotImplementedError("See docstring contract; write fresh.")


def gate_diff(*args, **kwargs):
    raise NotImplementedError("See docstring contract; write fresh.")


def agreement_rate(*args, **kwargs):
    raise NotImplementedError("See docstring contract; write fresh.")


def record_run(*args, **kwargs):
    raise NotImplementedError("See docstring contract; write fresh.")
