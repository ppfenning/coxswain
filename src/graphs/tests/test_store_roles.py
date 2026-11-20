import pytest

from harness import store_ddl_0020, store_roles
from harness.store_roles import (
    CLAIM_COLUMNS,
    GRANTS,
    GROUP_ROLE,
    OK,
    PROBES,
    REFUSED,
    RoleState,
    grant_statements,
    probe_verdict,
    role_name,
    role_plan,
    worker_url,
)

# Every table a store at 0020 has, as a migrated sqlite double lists them.
STORE_TABLES = (
    "attempts", "cartridge_snapshots", "chair_actions", "courier_log", "gate_decisions", "graph_edges", "graph_nodes",
    "graphs", "hosts", "id_sequence", "land_queue", "lander_notices", "landing_records", "leases", "ledger",
    "node_calls", "phase_queue", "phases", "review_rounds", "runs", "schema_version", "step_results", "task_records",
    "tasks", "work_items", "workers",
)
NEVER_WRITTEN = ("work_items", "hosts", "land_queue", "lander_notices", "chair_actions", "courier_log", "leases",
                 "id_sequence", "landing_records", "cartridge_snapshots", "schema_version")
UNTOUCHED = ("land_queue", "lander_notices", "chair_actions", "courier_log", "leases", "id_sequence")


def _grants(statements):
    return [s for s in statements if s.startswith("GRANT")]


def _on(statements, table):
    return [s for s in statements if s.split(" ON TABLE ")[-1].split(" ")[0] == table and " ON TABLE " in s]


def test_the_grant_table_names_each_table_once_and_only_the_tables_the_plan_lists():
    tables = [g.table for g in GRANTS]
    assert len(tables) == len(set(tables))
    assert set(tables) - set(STORE_TABLES) == {"store_cursor"}  # named by the plan; no migration creates it yet
    assert set(UNTOUCHED).isdisjoint(tables)


def test_phase_queue_updates_only_its_claim_columns_and_results_are_insert_only():
    stmts = grant_statements(STORE_TABLES)
    assert _grants(_on(stmts, "phase_queue")) == [
        f"GRANT SELECT ON TABLE phase_queue TO {GROUP_ROLE}",
        f"GRANT UPDATE ({', '.join(CLAIM_COLUMNS)}) ON TABLE phase_queue TO {GROUP_ROLE}",
    ]
    for chair_column in ("band", "seq", "requires_json", "kind", "budget_usd"):
        assert chair_column not in CLAIM_COLUMNS
    assert _grants(_on(stmts, "step_results")) == [f"GRANT SELECT, INSERT ON TABLE step_results TO {GROUP_ROLE}"]
    assert _grants(_on(stmts, "review_rounds")) == [f"GRANT SELECT, INSERT ON TABLE review_rounds TO {GROUP_ROLE}"]
    for table in ("runs", "node_calls", "task_records", "attempts", "ledger"):
        assert _grants(_on(stmts, table)) == [f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO {GROUP_ROLE}"]


def test_no_statement_writes_a_forbidden_table_grants_ddl_or_deletes():
    stmts = grant_statements(STORE_TABLES)
    for table in NEVER_WRITTEN:
        assert not any(w in s for s in _grants(_on(stmts, table)) for w in ("INSERT", "UPDATE", "DELETE"))
    for table in UNTOUCHED:
        assert _grants(_on(stmts, table)) == []
        assert f"REVOKE ALL ON TABLE {table} FROM {GROUP_ROLE}" in stmts
    joined = "\n".join(_grants(stmts))
    for word in ("DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "REFERENCES", "TRIGGER"):
        assert word not in joined
    assert f"REVOKE CREATE ON SCHEMA public FROM {GROUP_ROLE}" in stmts


def test_every_present_table_is_revoked_before_it_is_granted_so_a_repeat_changes_nothing():
    stmts = grant_statements(STORE_TABLES)
    assert grant_statements(reversed(STORE_TABLES)) == stmts
    for table in STORE_TABLES:
        on = _on(stmts, table)
        assert on[0] == f"REVOKE ALL ON TABLE {table} FROM {GROUP_ROLE}"
        assert all(s.startswith("GRANT") for s in on[1:])


def test_a_table_the_store_lacks_is_skipped_and_one_the_grant_table_lacks_gets_only_a_revoke():
    stmts = grant_statements(("step_results", "a_later_table"))
    assert _on(stmts, "phase_queue") == []
    assert _on(stmts, "a_later_table") == [f"REVOKE ALL ON TABLE a_later_table FROM {GROUP_ROLE}"]


def test_a_name_that_is_not_plain_is_refused_rather_than_put_in_a_statement():
    with pytest.raises(ValueError):
        grant_statements(("runs; DROP TABLE runs",))
    with pytest.raises(ValueError):
        grant_statements(("runs",), schema="Public")


@pytest.mark.parametrize(("worker", "role"), [
    ("lane-host-1", "cox_w_lane_host_1"), ("Build.Box", "cox_w_build_box"), ("  w2  ", "cox_w_w2"),
])
def test_role_name_is_the_prefix_and_a_plain_lower_case_name(worker, role):
    assert role_name(worker) == role


@pytest.mark.parametrize("worker", ["", "---", "x" * 60])
def test_role_name_refuses_an_empty_or_too_long_name(worker):
    with pytest.raises(ValueError):
        role_name(worker)


def test_a_new_store_gets_both_roles_and_the_new_password():
    plan = role_plan(RoleState(False, False, ("runs",)), "cox_w_a", None, "new-pass")
    assert plan.statements[:4] == (
        f"CREATE ROLE {GROUP_ROLE} NOLOGIN",
        "CREATE ROLE cox_w_a LOGIN PASSWORD 'new-pass'",
        f"GRANT {GROUP_ROLE} TO cox_w_a",
        "ALTER ROLE cox_w_a SET idle_in_transaction_session_timeout = '15s'",
    )
    assert plan.statements[4:] == grant_statements(("runs",))
    assert (plan.password, plan.sets_password) == ("new-pass", True)


def test_a_repeat_with_the_password_kept_sets_no_password_and_creates_nothing():
    plan = role_plan(RoleState(True, True, ("runs",)), "cox_w_a", "kept", "unused")
    assert not any("CREATE ROLE" in s or "PASSWORD" in s for s in plan.statements)
    assert plan.statements[0] == "ALTER ROLE cox_w_a LOGIN"
    assert (plan.password, plan.sets_password) == ("kept", False)
    assert role_plan(RoleState(True, True, ("runs",)), "cox_w_a", "kept", "other") == plan


def test_a_role_whose_password_the_file_lost_gets_the_new_one():
    plan = role_plan(RoleState(True, True, ()), "cox_w_a", None, "fresh")
    assert plan.statements[0] == "ALTER ROLE cox_w_a LOGIN PASSWORD 'fresh'"
    assert (plan.password, plan.sets_password) == ("fresh", True)


def test_a_dropped_role_is_recreated_with_the_password_the_file_still_holds():
    plan = role_plan(RoleState(True, False, ()), "cox_w_a", "kept", "unused")
    assert plan.statements[0] == "CREATE ROLE cox_w_a LOGIN PASSWORD 'kept'"
    assert plan.password == "kept"


def test_a_quote_in_a_password_is_doubled():
    assert "PASSWORD 'it''s'" in role_plan(RoleState(True, False, ()), "cox_w_a", None, "it's").statements[0]


def test_worker_url_swaps_only_the_user_and_password():
    owner = "postgresql://owner:p%40ss@db.example:5433/coxswain?sslmode=require"
    assert worker_url(owner, "cox_w_a", "s/e:c") == "postgresql://cox_w_a:s%2Fe%3Ac@db.example:5433/coxswain?sslmode=require"
    assert worker_url("postgresql://db.example/coxswain", "cox_w_a", "x") == "postgresql://cox_w_a:x@db.example/coxswain"


def test_the_probe_inserts_fill_every_column_of_the_tables_they_write():
    columns = {n: [c for c, _ in cs] for n, cs, _ in store_ddl_0020.TABLES}
    for probe in PROBES:
        if probe.sql.startswith("INSERT INTO step_results") or probe.sql.startswith("INSERT INTO review_rounds"):
            table = probe.sql.split()[2]
            assert probe.sql.count(",") >= len(columns[table]) - 1
            assert f"({', '.join(columns[table])})" in probe.sql
    assert {p.allowed for p in PROBES} == {True, False}
    assert any("work_items" in p.sql and not p.allowed for p in PROBES)


def test_the_verdict_passes_only_when_allowed_writes_work_and_forbidden_ones_are_refused():
    good = [(p, OK if p.allowed else REFUSED) for p in PROBES]
    ok, detail = probe_verdict(good)
    assert ok and "refused" in detail
    allowed = next(p for p in PROBES if p.allowed)
    forbidden = next(p for p in PROBES if not p.allowed)
    assert probe_verdict([(allowed, REFUSED)]) == (False, f"{allowed.label}: refused")
    assert probe_verdict([(forbidden, OK)]) == (False, f"{forbidden.label}: allowed")
    assert probe_verdict([(forbidden, "UndefinedTable: x")]) == (False, f"{forbidden.label}: UndefinedTable: x")
    assert probe_verdict([(allowed, "UndefinedTable: x")]) == (False, f"{allowed.label}: UndefinedTable: x")
    assert probe_verdict([]) == (False, "no probe ran")


def test_the_env_names_match_the_tools_copy():
    from agent_tools import unit_env

    assert (unit_env.WORKER_ROLE_ENV, unit_env.WORKER_PASSWORD_ENV) == (store_roles.ROLE_ENV, store_roles.PASSWORD_ENV)
