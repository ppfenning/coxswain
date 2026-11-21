import pytest

from harness import store_ddl_0022
from harness.store_ddl_0022 import ADDITIVE, INDEXES, statements
from harness.store_dialect import POSTGRES, SQLITE, connect
from harness.store_migrate import ADDITIVE_PREFIX, additive_violations, check_version, default_modules, migrate

NOW1 = "2026-11-20T00:00:00Z"
NOW2 = "2026-11-21T00:00:00Z"
LATEST = len(default_modules())

# The hot reads the indexes are for, as their callers write them: `run_store.cost_since` and
# `run_store.latest_chair_meter` (the chair's budget gate and meter read on every tick).
COST_SINCE = "SELECT SUM(cost_usd) AS total FROM node_calls WHERE ts >= %s"
LATEST_OF_KIND = "SELECT action_json FROM chair_actions WHERE kind = %s ORDER BY ts DESC LIMIT 1"


def test_the_migration_is_two_non_unique_indexes_and_additive_on_both_dialects():
    assert ADDITIVE is True
    assert statements(SQLITE) == statements(POSTGRES) == (
        "CREATE INDEX IF NOT EXISTS ix_node_calls_ts ON node_calls (ts)",
        "CREATE INDEX IF NOT EXISTS ix_chair_actions_kind_ts ON chair_actions (kind, ts)",
    )
    for dialect in (SQLITE, POSTGRES):
        assert additive_violations(statements(dialect), {}) == ()


def test_a_store_at_version_twenty_one_gains_both_indexes_recorded_as_additive(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:21]) == 21
        assert migrate(c, NOW2, default_modules()[:22]) == 22
        if c.dialect.name == "sqlite":
            found = c.query_all("SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'ix_%'")
        else:
            found = c.query_all("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
        assert {name for name, _, _ in INDEXES} <= {r[0] for r in found}
        recorded = c.query_one("SELECT description FROM schema_version WHERE version = 22")
        assert recorded == (ADDITIVE_PREFIX + store_ddl_0022.DESCRIPTION,)
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
    finally:
        c.close()


def test_the_postgres_planner_answers_cost_since_and_the_newest_of_a_kind_from_the_new_indexes(store_url):
    if not store_url.startswith("postgres"):
        pytest.skip("EXPLAIN reads the Postgres planner")
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()) == LATEST
        with c.transaction():
            # A table this small is cheaper to scan than to probe; with scans priced out, the plan shows whether an
            # index fits the predicate at all. Without these indexes both reads stay a scan or the primary key.
            c.execute("SET LOCAL enable_seqscan = off")
            cost = "\n".join(r[0] for r in c.query_all("EXPLAIN " + COST_SINCE, ("2026-11-19T00:00:00+00:00",)))
            newest = "\n".join(r[0] for r in c.query_all("EXPLAIN " + LATEST_OF_KIND, ("meter",)))
        assert "ix_node_calls_ts" in cost, cost
        assert "ix_chair_actions_kind_ts" in newest, newest
    finally:
        c.close()
