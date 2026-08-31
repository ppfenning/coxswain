"""Append-only, one JSON object per line, and picky about what it accepts."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import ledger
from tests.conftest import rows


def test_round_trip_preserves_order(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(rows(("ticket_create", "low", "clean"), ("merge", "high", "reversal")), path)
    read = ledger.read(path)
    assert [r["kind"] for r in read] == ["ticket_create", "merge"]


def test_append_never_rewrites_history(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(rows(("ticket_create", "low", "clean")), path)
    ledger.append(rows(("ticket_create", "low", "reversal")), path)
    assert [r["outcome"] for r in ledger.read(path)] == ["clean", "reversal"]


def test_missing_ledger_reads_empty_rather_than_raising(tmp_path: Path) -> None:
    assert ledger.read(tmp_path / "nothing.jsonl") == ()


def test_refuses_unknown_outcome(tmp_path: Path) -> None:
    bad = rows(("ticket_create", "low", "clean"))
    bad[0]["outcome"] = "went_fine_probably"
    with pytest.raises(ledger.LedgerError, match="unknown outcome"):
        ledger.append(bad, tmp_path / "ledger.jsonl")


def test_refuses_row_missing_scope_fields(tmp_path: Path) -> None:
    bad = rows(("ticket_create", "low", "clean"))
    del bad[0]["cartridge_sha"]
    with pytest.raises(ledger.LedgerError, match="missing required field"):
        ledger.append(bad, tmp_path / "ledger.jsonl")


def test_a_bad_row_writes_nothing_at_all(tmp_path: Path) -> None:
    """All-or-nothing: half a run in the ledger is worse than none."""
    path = tmp_path / "ledger.jsonl"
    batch = rows(("ticket_create", "low", "clean"), ("merge", "high", "clean"))
    batch[1]["outcome"] = "nonsense"
    with pytest.raises(ledger.LedgerError):
        ledger.append(batch, path)
    assert not path.exists()


def test_append_observation_records_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    row = rows(("retry_idempotent", "medium", "clean"))[0]
    ledger.append_observation(row, path)
    assert ledger.read(path)[0]["outcome"] == "failure"


def test_corrupt_line_names_its_line_number(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(rows(("ticket_create", "low", "clean")), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    with pytest.raises(ledger.LedgerError, match=r"ledger.jsonl:2"):
        ledger.read(path)
