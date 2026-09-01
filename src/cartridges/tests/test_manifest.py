"""Outcomes are DERIVED from what the human did. A run cannot grade itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import ledger
from core.manifest import ManifestError, agreement_rate, build_manifest, gate_diff, record_run

CARTRIDGE = {"team": "acme", "cartridge_sha": "sha-1"}
PROPOSAL = {"kind": "ticket_create", "risk": "low", "target": "TICKET-1"}


def manifest(*diffs, **overrides):
    return build_manifest(
        run_id="run-1",
        ts="2026-08-30T12:00:00Z",
        principal="lifecycle-propose",
        cartridge=CARTRIDGE,
        provider_profile="anthropic-default",
        proposals=[PROPOSAL],
        gate_diffs=list(diffs),
        **overrides,
    )


# ── gate_diff derives the outcome ──────────────────────────────────────────


def test_approved_applied_unedited_is_clean() -> None:
    assert gate_diff(PROPOSAL, "approved", applied=True, edited=False)["outcome"] == "clean"


def test_edited_is_a_reversal_even_when_applied() -> None:
    """The human had to fix it. That the fix shipped does not make it a win."""
    assert gate_diff(PROPOSAL, "approved", applied=True, edited=True)["outcome"] == "reversal"


def test_refused_is_a_reversal() -> None:
    assert gate_diff(PROPOSAL, "refused", applied=False, edited=False)["outcome"] == "reversal"


def test_approved_but_never_executed_is_skipped() -> None:
    assert gate_diff(PROPOSAL, "approved", applied=False, edited=False)["outcome"] == "skipped"


def test_unknown_decision_is_refused() -> None:
    with pytest.raises(ManifestError, match="unknown gate decision"):
        gate_diff(PROPOSAL, "sort_of_approved", applied=True, edited=False)


# ── gate_diff carries the grain a streak is measured at ────────────────────

ENTRY_PROPOSAL = {**PROPOSAL, "subject": "rb-04", "subject_new": False, "attempts": 3}


def test_subject_and_attempts_ride_through_the_gate() -> None:
    diff = gate_diff(ENTRY_PROPOSAL, "approved", applied=True, edited=False)
    assert diff["subject"] == "rb-04"
    assert diff["subject_new"] is False
    assert diff["attempts"] == 3


def test_absent_grain_stays_absent_rather_than_being_defaulted() -> None:
    """A default here would be an invented track record. Absent means absent."""
    diff = gate_diff(PROPOSAL, "approved", applied=True, edited=False)
    assert "subject" not in diff and "subject_new" not in diff and "attempts" not in diff


# ── agreement_rate ─────────────────────────────────────────────────────────


def test_agreement_rate_counts_only_decided_proposals() -> None:
    m = manifest(
        gate_diff(PROPOSAL, "approved", applied=True, edited=False),
        gate_diff(PROPOSAL, "approved", applied=True, edited=True),
        gate_diff(PROPOSAL, "approved", applied=False, edited=False),  # skipped: no signal
    )
    assert agreement_rate(m) == 0.5


def test_agreement_rate_of_a_run_nobody_ruled_on_is_zero() -> None:
    assert agreement_rate(manifest()) == 0.0


# ── build_manifest ─────────────────────────────────────────────────────────


def test_manifest_is_attributable_to_the_rules_it_ran_under() -> None:
    assert manifest()["cartridge_sha"] == "sha-1"


def test_refuses_an_unresolved_cartridge() -> None:
    with pytest.raises(ManifestError, match="no 'cartridge_sha'"):
        build_manifest(
            run_id="r",
            ts="t",
            principal="p",
            cartridge={"team": "acme"},
            provider_profile="anthropic-default",
            proposals=[],
        )


def test_human_minutes_is_carried_run_level() -> None:
    assert manifest(human_minutes=12.5)["human_minutes"] == 12.5


# ── record_run: the I/O edge ───────────────────────────────────────────────


def test_record_run_writes_manifest_and_derives_ledger_rows(tmp_path: Path) -> None:
    m = manifest(
        gate_diff(PROPOSAL, "approved", applied=True, edited=False),
        gate_diff(PROPOSAL, "refused", applied=False, edited=False),
    )
    record_run(m, runs_dir=tmp_path / "runs", ledger_path=tmp_path / "ledger.jsonl")

    written = json.loads((tmp_path / "runs" / "run-1.json").read_text())
    assert written["run_id"] == "run-1"

    recorded = ledger.read(tmp_path / "ledger.jsonl")
    assert [r["outcome"] for r in recorded] == ["clean", "reversal"]
    assert {r["principal"] for r in recorded} == {"lifecycle-propose"}, "principal is the graph, never a person"
    assert {r["cartridge_sha"] for r in recorded} == {"sha-1"}


def test_record_run_writes_subject_and_attempts_onto_the_row(tmp_path: Path) -> None:
    m = manifest(gate_diff(ENTRY_PROPOSAL, "approved", applied=True, edited=False))
    record_run(m, runs_dir=tmp_path / "runs", ledger_path=tmp_path / "ledger.jsonl")
    row = ledger.read(tmp_path / "ledger.jsonl")[0]
    assert row["subject"] == "rb-04"
    assert row["attempts"] == 3
    assert "subject_new" not in row, "a fact about one moment, not about the row"


def test_record_run_leaves_a_subjectless_run_subjectless(tmp_path: Path) -> None:
    m = manifest(gate_diff(PROPOSAL, "approved", applied=True, edited=False))
    record_run(m, runs_dir=tmp_path / "runs", ledger_path=tmp_path / "ledger.jsonl")
    row = ledger.read(tmp_path / "ledger.jsonl")[0]
    assert "subject" not in row and "attempts" not in row


def test_caller_cannot_assert_a_run_was_clean(tmp_path: Path) -> None:
    """Self-reported success is exactly what the ledger exists to not believe."""
    m = manifest(gate_diff(PROPOSAL, "approved", applied=True, edited=True))
    m["proposals"][0]["self_reported"] = "all good!"
    record_run(m, runs_dir=tmp_path / "runs", ledger_path=tmp_path / "ledger.jsonl")
    assert [r["outcome"] for r in ledger.read(tmp_path / "ledger.jsonl")] == ["reversal"]
