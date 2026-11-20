"""`store_scoped` on the sqlite double: the version rule of `open_scoped`, that it never migrates, and that every probe
rolls back. Roles and grants are Postgres-only; smoke/test_worker_role.py proves them against a real server."""

import pytest

from harness import store_roles
from harness.store_dialect import connect
from harness.store_migrate import check_version, default_modules, migrate
from harness.store_read import StoreVersionError
from harness.store_scoped import apply_role, open_scoped, probe

NOW = "2026-11-20T00:00:00Z"
LATEST = len(default_modules())


def _store(tmp_path, modules=None) -> str:
    url = f"sqlite:///{tmp_path / 'store.db'}"
    conn = connect(url)
    try:
        migrate(conn, NOW, modules)
    finally:
        conn.close()
    return url


def _version(url: str) -> int:
    conn = connect(url)
    try:
        return check_version(conn)[0]
    finally:
        conn.close()


def test_a_store_at_this_codes_version_opens_and_takes_a_write(tmp_path):
    url = _store(tmp_path)
    conn = open_scoped(url)
    try:
        conn.execute(store_roles.PROBES[0].sql)
        assert conn.query_one("SELECT run_id FROM step_results") == (store_roles.PROBE_RUN,)
    finally:
        conn.close()


def test_an_older_store_is_refused_and_nothing_is_migrated(tmp_path):
    url = _store(tmp_path, default_modules()[:-1])
    with pytest.raises(StoreVersionError, match="older .* never migrates"):
        open_scoped(url)
    assert _version(url) == LATEST - 1


def test_an_empty_database_is_refused_and_gets_no_schema_version_table(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    connect(url).close()
    with pytest.raises(StoreVersionError, match="schema version 0"):
        open_scoped(url)
    conn = connect(url)
    try:
        assert conn.query_all("SELECT name FROM sqlite_master WHERE type = 'table'") == []
    finally:
        conn.close()


def test_every_probe_is_rolled_back_and_sqlite_refuses_nothing(tmp_path):
    url = _store(tmp_path)
    conn = open_scoped(url)
    try:
        outcomes = probe(conn)
        assert conn.query_all("SELECT * FROM step_results") == []
        assert conn.query_all("SELECT * FROM review_rounds") == []
        assert conn.query_one("SELECT name FROM sqlite_master WHERE name = 'cox_worker_probe'") is None
    finally:
        conn.close()
    by_label = {p.label: o for p, o in outcomes}
    assert by_label["insert step_results"] == store_roles.OK
    assert by_label["create table"] == store_roles.OK
    ok, detail = store_roles.probe_verdict(outcomes)
    assert not ok and "update work_items: allowed" in detail


def test_a_failing_probe_reports_its_error_and_leaves_the_connection_usable(tmp_path):
    url = _store(tmp_path)
    conn = open_scoped(url)
    try:
        broken = store_roles.Probe("insert nowhere", "INSERT INTO no_such_table VALUES (1)", True)
        ((_, outcome),) = probe(conn, (broken,))
        assert outcome.startswith("OperationalError: no such table")
        assert conn.query_one("SELECT COUNT(*) FROM step_results") == (0,)
    finally:
        conn.close()


def test_setting_up_a_role_on_sqlite_is_refused(tmp_path):
    url = _store(tmp_path)
    with pytest.raises(ValueError, match="needs the Postgres store"):
        apply_role(url, "cox_w_a", None, "pw", NOW)
