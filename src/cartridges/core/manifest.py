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

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core import ledger

__all__ = ["build_manifest", "gate_diff", "agreement_rate", "record_run", "ManifestError"]

APPROVED = "approved"
REFUSED = "refused"
DECISIONS = frozenset({APPROVED, REFUSED})


class ManifestError(Exception):
    """A manifest could not be built, or was asked to record something incoherent."""


def build_manifest(
    *,
    run_id: str,
    ts: str,
    principal: str,
    cartridge: Mapping[str, Any],
    provider_profile: str,
    proposals: Sequence[Mapping[str, Any]],
    gate_diffs: Sequence[Mapping[str, Any]] = (),
    human_minutes: float | None = None,
    totals: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the run record. Pure — every input is passed in, nothing read.

    `ts` and `run_id` are arguments for the same reason a graph's date is: a run
    that reads the clock cannot be replayed, and a run that cannot be replayed
    cannot be debugged after the fact.
    """
    sha = cartridge.get("cartridge_sha")
    if not sha:
        raise ManifestError("resolved cartridge has no 'cartridge_sha'; build_manifest needs a resolved cartridge")
    return {
        "run_id": run_id,
        "ts": ts,
        "principal": principal,
        "cartridge_sha": sha,
        "cartridge_team": cartridge.get("team"),
        "provider_profile": provider_profile,
        "proposals": [dict(p) for p in proposals],
        "gate_diffs": [dict(d) for d in gate_diffs],
        "human_minutes": human_minutes,
        "totals": dict(totals or {}),
    }


def gate_diff(
    proposal: Mapping[str, Any],
    decision: str,
    applied: bool,
    edited: bool,
) -> dict[str, Any]:
    """One human decision at the gate, reduced to the fields policy reads.

    The outcome is DERIVED here rather than supplied, so no caller can assert a
    run was clean. `skipped` — approved but never executed — is deliberately
    neither a win nor a reversal: it proves nothing about whether the proposal
    was right, so it must not build or break a streak.
    """
    if decision not in DECISIONS:
        raise ManifestError(f"unknown gate decision {decision!r}; expected one of {sorted(DECISIONS)}")
    if decision == REFUSED or edited:
        outcome = "reversal"
    elif applied:
        outcome = "clean"
    else:
        outcome = "skipped"
    return {
        "kind": proposal.get("kind"),
        "risk": proposal.get("risk"),
        "target": proposal.get("target"),
        "decision": decision,
        "applied": bool(applied),
        "edited": bool(edited),
        "outcome": outcome,
    }


def agreement_rate(manifest: Mapping[str, Any]) -> float:
    """How often the gate accepted what was proposed, unedited.

    Denominator is decisions actually made — a proposal nobody ruled on says
    nothing about agreement, and padding the denominator with it would quietly
    depress a number this whole system is judged by.
    """
    diffs = manifest.get("gate_diffs") or []
    decided = [d for d in diffs if d.get("outcome") in {"clean", "reversal"}]
    if not decided:
        return 0.0
    return sum(1 for d in decided if d["outcome"] == "clean") / len(decided)


def record_run(
    manifest: Mapping[str, Any],
    *,
    runs_dir: Path | str,
    ledger_path: Path | str,
) -> None:
    """Write the manifest to runs/ and derive ledger rows from its gate diffs.

    THE I/O EDGE. Note what is not a parameter: the outcomes. They come from
    `gate_diff`, which computed them from what the human actually did. A caller
    cannot tell this function the run went well.
    """
    if not manifest.get("run_id"):
        raise ManifestError("manifest has no run_id")

    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{manifest['run_id']}.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )

    rows = [
        {
            "run_id": manifest["run_id"],
            "ts": manifest["ts"],
            "principal": manifest["principal"],
            "kind": diff.get("kind"),
            "risk": diff.get("risk"),
            "outcome": diff["outcome"],
            "cartridge_sha": manifest["cartridge_sha"],
            "provider_profile": manifest["provider_profile"],
        }
        for diff in manifest.get("gate_diffs") or []
    ]
    ledger.append(rows, ledger_path)
