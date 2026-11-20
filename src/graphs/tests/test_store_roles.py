import base64

import pytest

from harness import store_ddl_0020, store_roles
from harness.store_roles import (
    CLAIM_COLUMNS,
    GRANTS,
    GROUP_ROLE,
    OK,
    PROBES,
    REFUSED,
    WIDENING,
    RoleAttrs,
    RoleState,
    grant_statements,
    owner_refusal,
    probe_verdict,
    residue,
    role_name,
    role_plan,
    scram_verifier,
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


SALT = bytes(range(16))
# Postgres 16 made this verifier for 'pencil' (CREATE ROLE ... PASSWORD 'pencil', then pg_authid.rolpassword).
PG_SALT = base64.b64decode("i7aP06P6QYE2A5L5DR6nGA==")
PG_VERIFIER = ("SCRAM-SHA-256$4096:i7aP06P6QYE2A5L5DR6nGA==$f2C38IbwGuhZanfkiZZeZURI+9mWvJWmCauiUvVwNr4=:"
               "Cv0IWktLG01tomCr9VhJEMputAj3XQyxWxF/yMSsNGE=")


def test_the_verifier_is_the_one_postgres_computes_for_the_same_password_and_salt():
    assert scram_verifier("pencil", PG_SALT) == PG_VERIFIER


@pytest.mark.parametrize("password", ["", "has space", "caf\u00e9"])
def test_a_password_sasl_prep_could_change_is_refused(password):
    with pytest.raises(ValueError):
        scram_verifier(password, SALT)


SAFE_GROUP = RoleAttrs(login=False)
SAFE_LOGIN = RoleAttrs(login=True)
WIDE = RoleAttrs(widening=frozenset(WIDENING), inherit=False, login=False, connection_limit=3, expires=True)
NEW = RoleState(None, None, ("runs",))
SAFE = RoleState(SAFE_GROUP, SAFE_LOGIN, ("runs",))


def _plan(state=None, kept=None, new="new-pass", **kw):
    return role_plan(state or NEW, "cox_w_a", kept, new, SALT, **kw)


def _alters(stmts, role):
    return [s for s in stmts if s.startswith(f"ALTER ROLE {role} ") and " SET " not in s and "RESET" not in s]


def test_no_statement_ever_carries_the_password_itself():
    for state in (NEW, SAFE, RoleState(SAFE_GROUP, None, ()), RoleState(WIDE, WIDE, ())):
        for kept in (None, "kept-pass"):
            plan = _plan(state, kept)
            assert not any(plan.password in s for s in plan.statements)
            setting = [s for s in plan.statements if "PASSWORD" in s]
            assert len(setting) == (1 if plan.sets_password else 0)
            assert all(s.endswith(f"PASSWORD '{scram_verifier(plan.password, SALT)}'") for s in setting)


def test_a_new_store_creates_both_roles_and_sends_no_attribute_it_need_not():
    plan = _plan()
    assert plan.statements[:2] == (f"CREATE ROLE {GROUP_ROLE} NOLOGIN",
                                   f"CREATE ROLE cox_w_a LOGIN PASSWORD '{scram_verifier('new-pass', SALT)}'")
    assert _alters(plan.statements, GROUP_ROLE) == [] and _alters(plan.statements, "cox_w_a") == []
    assert f"GRANT {GROUP_ROLE} TO cox_w_a" in plan.statements
    assert "ALTER ROLE cox_w_a SET idle_in_transaction_session_timeout = '15s'" in plan.statements
    assert plan.statements[-len(grant_statements(("runs",))):] == grant_statements(("runs",))
    assert (plan.password, plan.sets_password) == ("new-pass", True)


def test_roles_already_safe_get_no_alter_at_all_so_an_owner_without_superuser_can_run_it():
    stmts = _plan(SAFE, "kept").statements
    assert _alters(stmts, GROUP_ROLE) == [] and _alters(stmts, "cox_w_a") == []
    assert not any(a in s for s in stmts for a in ("SUPERUSER", "BYPASSRLS", "REPLICATION", "CREATEDB"))


def test_only_what_is_on_is_turned_off():
    state = RoleState(RoleAttrs(widening=frozenset({"BYPASSRLS"})), RoleAttrs(widening=frozenset({"CREATEROLE"}),
                                                                           login=True), ())
    stmts = _plan(state, "kept").statements
    assert _alters(stmts, GROUP_ROLE) == [f"ALTER ROLE {GROUP_ROLE} NOBYPASSRLS"]
    assert _alters(stmts, "cox_w_a") == ["ALTER ROLE cox_w_a NOCREATEROLE"]


def test_every_run_takes_away_what_was_added_to_either_role_by_hand():
    state = RoleState(WIDE, WIDE, ("runs",), database="cox", group_memberships=("pg_read_all_data",),
                      login_memberships=(GROUP_ROLE, "pg_write_all_data", "Odd\"Name"))
    stmts = _plan(state, "kept").statements
    off = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION INHERIT CONNECTION LIMIT -1 VALID UNTIL 'infinity'"
    assert _alters(stmts, GROUP_ROLE) == [f"ALTER ROLE {GROUP_ROLE} {off}"]
    assert _alters(stmts, "cox_w_a") == [f"ALTER ROLE cox_w_a {off} LOGIN"]
    assert 'REVOKE "pg_read_all_data" FROM cox_worker' in stmts
    assert 'REVOKE "pg_write_all_data" FROM cox_w_a' in stmts
    assert 'REVOKE "Odd""Name" FROM cox_w_a' in stmts
    assert not any(s == f'REVOKE "{GROUP_ROLE}" FROM cox_w_a' for s in stmts)
    for role in ("cox_w_a", GROUP_ROLE):
        assert f'REVOKE ALL ON DATABASE "cox" FROM {role}' in stmts
        for what in ("SCHEMA public", "ALL TABLES IN SCHEMA public", "ALL SEQUENCES IN SCHEMA public",
                     "ALL FUNCTIONS IN SCHEMA public"):
            assert f"REVOKE ALL ON {what} FROM {role}" in stmts
    assert "ALTER ROLE cox_w_a RESET ALL" in stmts
    revokes = max(i for i, s in enumerate(stmts) if s.startswith("REVOKE ALL ON ALL TABLES"))
    assert revokes < stmts.index(f"GRANT {GROUP_ROLE} TO cox_w_a") < stmts.index(grant_statements(("runs",))[0])


def test_a_repeat_with_the_password_kept_sets_no_password_and_creates_nothing():
    plan = _plan(SAFE, "kept", "unused")
    assert not any("CREATE ROLE" in s or "PASSWORD" in s for s in plan.statements)
    assert (plan.password, plan.sets_password) == ("kept", False)
    assert _plan(SAFE, "kept", "other") == plan


def test_a_role_whose_password_the_file_lost_gets_the_new_one():
    plan = _plan(SAFE, None, "fresh")
    assert (plan.password, plan.sets_password) == ("fresh", True)
    assert _alters(plan.statements, "cox_w_a") == [f"ALTER ROLE cox_w_a PASSWORD '{scram_verifier('fresh', SALT)}'"]


def test_a_dropped_role_is_recreated_with_the_password_the_file_still_holds():
    plan = _plan(RoleState(SAFE_GROUP, None, ()), "kept", "unused")
    assert f"CREATE ROLE cox_w_a LOGIN PASSWORD '{scram_verifier('kept', SALT)}'" in plan.statements
    assert (plan.password, plan.sets_password) == ("kept", True)


def test_an_owner_that_lacks_an_attribute_that_is_on_is_refused_by_name():
    createrole_only = frozenset({"CREATEROLE"})
    assert owner_refusal(RoleState(WIDE, WIDE, (), owner=frozenset(WIDENING)), "cox_w_a") is None
    assert owner_refusal(RoleState(SAFE_GROUP, SAFE_LOGIN, (), owner=createrole_only), "cox_w_a") is None
    state = RoleState(SAFE_GROUP, RoleAttrs(widening=frozenset({"BYPASSRLS", "CREATEROLE"}), login=True), (),
                      owner=createrole_only)
    reason = owner_refusal(state, "cox_w_a")
    assert reason is not None and reason.startswith("cox_w_a has BYPASSRLS, which only")
    assert "`ALTER ROLE cox_w_a NOBYPASSRLS`" in reason and "CREATEROLE" not in reason


def test_residue_names_what_a_reset_left():
    assert residue(SAFE) == ()
    assert residue(RoleState(SAFE_GROUP, SAFE_LOGIN, (), login_memberships=(GROUP_ROLE,))) == ()
    left = residue(RoleState(RoleAttrs(widening=frozenset({"CREATEDB"})), SAFE_LOGIN, (),
                             login_memberships=(GROUP_ROLE, "pg_write_all_data")))
    assert left == ("cox_worker still has CREATEDB", "the login role is still a member of pg_write_all_data")


def test_public_create_is_revoked_only_when_asked():
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" not in _plan().statements
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in _plan(revoke_public_create=True).statements


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
    create = next(p for p in PROBES if p.label == "create table")
    ok, detail = probe_verdict([(create, OK)], "public")
    assert not ok and "PUBLIC can create in schema public" in detail
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in detail and "--revoke-public-create" in detail


def test_the_env_names_match_the_tools_copy():
    from agent_tools import unit_env

    assert (unit_env.WORKER_ROLE_ENV, unit_env.WORKER_PASSWORD_ENV) == (store_roles.ROLE_ENV, store_roles.PASSWORD_ENV)
