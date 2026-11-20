"""Migration 0020: phase_queue, step_results, review_rounds and workers; hosts gains threads and capabilities_json.

`step_results` holds one row per finished step, keyed by the run and the step (`store_steps.step_key`), and is
insert-only: a repeat of the same (run_id, step) changes nothing. `review_rounds` holds one row per completed review
round of a phase (or of an initiative, phase ''), written once when its verdict exists. `phase_queue` is the phase
worker's queue, with its claim on the row as `land_queue` keeps one; `workers` is one row per worker service, written
only by that worker. `hosts.threads` is the operator's task-build thread budget for a machine (0 derives it from the
cores) and `hosts.capabilities_json` what it can run, nullable like 0009's JSON columns.

Additive (`ADDITIVE = True`): only tables, indexes and columns with a default or NULL are added, so a build one
migration older keeps opening, reading and writing a store at this version (`store_migrate.accepts`).

Every new table's column is NOT NULL, with '' or 0 for "none", and each has a primary key; no foreign-key clause,
matching every migration since 0002. JSON columns are typed per dialect (JSONB on Postgres, TEXT on sqlite).
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 20
DESCRIPTION = "phase_queue, step_results, review_rounds and workers tables; hosts gains threads and capabilities_json"
ADDITIVE = True

_JSON = "JSON"

Column = tuple[str, str]

# Name, columns, key. A `JSON` type is resolved per dialect.
TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "phase_queue",
        (
            ("initiative", "TEXT"),
            ("phase", "TEXT"),
            ("kind", "TEXT"),
            ("repo", "TEXT"),
            ("base_ref", "TEXT"),
            ("branch", "TEXT"),
            ("requires_json", _JSON),
            ("state", "TEXT"),
            ("outcome", "TEXT"),
            ("reason", "TEXT"),
            ("band", "INTEGER"),
            ("seq", "BIGINT"),
            ("budget_usd", "REAL"),
            ("queued_at", "TEXT"),
            ("ready_at", "TEXT"),
            ("claimed_by", "TEXT"),
            ("claim_epoch", "INTEGER"),
            ("run_id", "TEXT"),
            ("claimed_at", "TEXT"),
            ("heartbeat_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("attempts", "INTEGER"),
            ("updated_at", "TEXT"),
            ("updated_by", "TEXT"),
        ),
        ("initiative", "phase"),
    ),
    (
        "step_results",
        (
            ("run_id", "TEXT"),
            ("step", "TEXT"),
            ("initiative", "TEXT"),
            ("phase", "TEXT"),
            ("task_id", "TEXT"),
            ("kind", "TEXT"),
            ("round", "INTEGER"),
            ("ok", "SMALLINT"),
            ("verdict", "TEXT"),
            ("subject_sha", "TEXT"),
            ("worker", "TEXT"),
            ("claim_epoch", "INTEGER"),
            ("started_at", "TEXT"),
            ("finished_at", "TEXT"),
            ("cost_usd", "REAL"),
            ("result_json", _JSON),
            ("artifacts_json", _JSON),
        ),
        ("run_id", "step"),
    ),
    (
        "review_rounds",
        (
            ("initiative", "TEXT"),
            ("phase", "TEXT"),
            ("round", "INTEGER"),
            ("head_sha", "TEXT"),
            ("verdict", "TEXT"),
            ("reason", "TEXT"),
            ("revision_json", _JSON),
            ("evidence_step", "TEXT"),
            ("run_id", "TEXT"),
            ("worker", "TEXT"),
            ("claim_epoch", "INTEGER"),
            ("cost_usd", "REAL"),
            ("decided_at", "TEXT"),
        ),
        ("initiative", "phase", "round"),
    ),
    (
        "workers",
        (
            ("name", "TEXT"),
            ("capabilities_json", _JSON),
            ("lanes", "INTEGER"),
            ("threads", "INTEGER"),
            ("state", "TEXT"),
            ("version", "TEXT"),
            ("doctor_json", _JSON),
            ("doctor_at", "TEXT"),
            ("started_at", "TEXT"),
            ("beat_at", "TEXT"),
        ),
        ("name",),
    ),
)

# Index name, table, columns.
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_phase_queue_order", "phase_queue", ("state", "band", "seq")),
    ("ix_phase_queue_holder", "phase_queue", ("claimed_by", "state")),
    ("ix_step_results_phase", "step_results", ("initiative", "phase", "kind")),
)

# Table, column, type, the column's tail after its type: a default, or nothing for a nullable column.
_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("hosts", "threads", "INTEGER", " NOT NULL DEFAULT 0"),
    ("hosts", "capabilities_json", _JSON, ""),
)

# Table and column that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9.
ADDED: tuple[tuple[str, str], ...] = tuple((t, c) for t, c, _, _ in _COLUMNS)


def _type(dialect: Dialect, kind: str) -> str:
    return dialect.json_type if kind == _JSON else kind


def _table(dialect: Dialect, name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {_type(dialect, t)} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The four CREATE TABLEs, their indexes, then the two hosts ALTERs; the same on both dialects but the JSON type."""
    return (
        *(_table(dialect, n, c, k) for n, c, k in TABLES),
        *(f"CREATE INDEX IF NOT EXISTS {i} ON {t} ({', '.join(cs)})" for i, t, cs in _INDEXES),
        *(f"ALTER TABLE {t} ADD COLUMN {c} {_type(dialect, k)}{tail}" for t, c, k, tail in _COLUMNS),
    )
