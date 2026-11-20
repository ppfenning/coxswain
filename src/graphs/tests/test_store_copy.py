import json

import pytest

import harness.store_copy as sc
from harness.store_migrate import NotExactVersion, default_modules, exact_refusal, open_store

T0 = "2026-09-25T00:00:00Z"

EXPECTED_ORDER = (
    "graphs",
    "graph_nodes",
    "graph_edges",
    "runs",
    "phases",
    "tasks",
    "task_records",
    "work_items",
    "attempts",
    "node_calls",
    "gate_decisions",
    "ledger",
    "leases",
    "chair_actions",
    "hosts",
    "id_sequence",
    "landing_records",
    "land_queue",
    "courier_log",
    "cartridge_snapshots",
    "lander_notices",
    "phase_queue",
    "step_results",
    "review_rounds",
    "workers",
)

SEED = {
    "graphs": [{"graph_id": "g1", "name": "demo", "version": "1", "content_hash": "g1", "definition_json": '{"x": 1}'}],
    "graph_nodes": [{"graph_id": "g1", "node_id": "n1", "ord": 0, "role": "plan"}],
    "graph_edges": [{"graph_id": "g1", "src": "n1", "dst": "n2"}],
    "runs": [{"run_id": "r1", "principal": "operator", "status": "done", "graph_id": "g1", "record_json": '{"a": 1}'}],
    "phases": [{"run_id": "r1", "phase_id": "p1", "human_minutes": 1.5, "totals_json": "{}"}],
    "tasks": [{"run_id": "r1", "task_id": "t1", "phase_id": "p1", "state": "done"}],
    "task_records": [
        {"run_id": "r1", "phase_id": "p1", "task_id": "t1", "record_json": '{"s": "done"}', "updated_at": T0}
    ],
    "attempts": [{"run_id": "r1", "task_id": "t1", "seq": 1, "kind": "build"}],
    "node_calls": [
        {"call_id": "c1", "run_id": "r1", "seq": 1, "cost_usd": 0.25, "ok": 1, "node_id": "n1"},
        {"call_id": "c2", "run_id": "r1", "seq": 2, "cost_usd": 0.5, "ok": 0, "detail_json": '{"k": "v"}'},
    ],
    "gate_decisions": [{"run_id": "r1", "phase_id": "p1", "seq": 1, "kind": "merge", "applied": 1, "epoch": 3}],
    "ledger": [{"row_hash": "h1", "run_id": "r1", "kind": "merge", "epoch": 3, "row_json": "{}"}],
    "leases": [{"name": "epic", "holder": "me", "epoch": 4}],
    "chair_actions": [
        {"ts": "2026-09-26T00:00:00Z", "epoch": 4, "holder": "me", "kind": "land", "target": "t1", "status": "done",
         "reason": "merged", "action_json": '{"kind": "land"}'},
    ],
    "hosts": [
        {"name": "host-a", "ssh": "host-a", "capacity": 8, "state": "active", "beat_at": "", "versions_json": "{}",
         "updated_at": "2026-09-26T00:00:00Z", "updated_by": "chair"},
    ],
    "work_items": [
        {"initiative": "i1", "task_id": "t1", "phase": "p1", "state": "ready", "needs_json": "[]",
         "updated_at": "2026-09-25T00:00:00Z", "updated_by": "chair"},
    ],
    "id_sequence": [{"scope": "initiative", "next": 1}],
    "landing_records": [
        {"initiative": "i1", "repo": "r", "state": "building", "record_json": '{"initiative": "i1"}',
         "created_at": "2026-09-25T00:00:00Z", "updated_at": "2026-09-25T00:00:00Z"},
    ],
    "land_queue": [
        {"id": "phase_merge:i1:p1", "kind": "phase_merge", "initiative": "i1", "phase": "p1", "repo": "r", "parent": "",
         "state": "queued", "reason": "phase built", "queued_at": "2026-09-25T00:00:00Z", "claimed_by": "",
         "claimed_at": "", "lease_until": "", "attempts": 0, "last_error": "", "updated_at": "2026-09-25T00:00:00Z"},
    ],
    "courier_log": [
        {"line_id": "l1", "at": "2026-09-25T00:00:00.000000Z", "line": '{"id": "m1", "to": "chair", "ack": false}'},
    ],
    "cartridge_snapshots": [
        {"sha": "a" * 64, "at": "2026-09-25T00:00:00Z", "by": "chair-host", "files_json": '{"team/cartridge.yaml": {"text": "k: v"}}'},
    ],
    "lander_notices": [{"key": "unreadable:i2", "reason": "not JSON", "since": "2026-09-25T00:00:00Z"}],
    "phase_queue": [
        {"initiative": "i1", "phase": "p1", "kind": "build", "repo": "r", "base_ref": "init/i1", "branch": "epic/i1/p1",
         "requires_json": "[]", "state": "waiting", "outcome": "", "reason": "", "band": 1, "seq": 1, "budget_usd": 5.0,
         "queued_at": T0, "ready_at": "", "claimed_by": "", "claim_epoch": 0, "run_id": "", "claimed_at": "",
         "heartbeat_at": "", "lease_until": "", "attempts": 0, "updated_at": T0, "updated_by": "chair"},
    ],
    "step_results": [
        {"run_id": "r1", "step": "task/t1/build", "initiative": "i1", "phase": "p1", "task_id": "t1", "kind": "build",
         "round": 1, "ok": 1, "verdict": "", "subject_sha": "", "worker": "", "claim_epoch": 0, "started_at": T0,
         "finished_at": T0, "cost_usd": 0.0, "result_json": "{}", "artifacts_json": "[]"},
    ],
    "review_rounds": [
        {"initiative": "i1", "phase": "p1", "round": 1, "head_sha": "a" * 40, "verdict": "approve", "reason": "",
         "revision_json": "{}", "evidence_step": "phase/smoke/1", "run_id": "r1", "worker": "lane-host",
         "claim_epoch": 1, "cost_usd": 0.5, "decided_at": T0},
    ],
    "workers": [
        {"name": "lane-host", "capabilities_json": "[]", "lanes": 2, "threads": 8, "state": "up", "version": "v",
         "doctor_json": "{}", "doctor_at": T0, "started_at": T0, "beat_at": T0},
    ],
}


def put(conn, table, row):
    marks = ", ".join(conn.dialect.placeholder for _ in row)
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({marks})", list(row.values()))


def seed(conn, extra=None):
    for name, rows in {**SEED, **(extra or {})}.items():
        for row in rows:
            put(conn, name, row)


def counts(url):
    conn = open_store(url, T0)
    try:
        return {n: conn.query_one(f"SELECT COUNT(*) FROM {n}")[0] for n, _, _ in sc.tables()}
    finally:
        conn.close()


@pytest.fixture
def src_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'src.db'}"
    conn = open_store(url, T0)
    try:
        seed(conn)
    finally:
        conn.close()
    return url


@pytest.fixture
def dst_url(store_url, tmp_path):
    """The destination per backend. An in-memory sqlite store vanishes on close, so it becomes a file."""
    return f"sqlite:///{tmp_path / 'dst.db'}" if store_url == "sqlite:///:memory:" else store_url


def test_the_plan_puts_parents_first_and_names_every_table():
    assert tuple(n for n, _, _ in sc.plan(sc.tables())) == EXPECTED_ORDER


def test_a_table_the_order_does_not_know_is_refused():
    with pytest.raises(ValueError, match="mystery"):
        sc.plan((*sc.tables(), ("mystery", ("a",), ("a",))))


def test_the_columns_come_from_the_ddl_including_the_alter_columns():
    by_name = {n: cols for n, cols, _ in sc.tables()}
    assert "graph_id" in by_name["runs"]
    assert "node_id" in by_name["node_calls"]
    assert "schema_version" not in by_name


def test_every_listed_table_carries_every_column_the_migrated_store_has():
    conn = open_store("sqlite:///:memory:", T0)
    try:
        listed = {n: set(cols) for n, cols, _ in sc.tables()}
        live = {n: {r[1] for r in conn.query_all(f"PRAGMA table_info({n})")} for n in listed}
        assert listed == live
    finally:
        conn.close()


def test_the_attempts_cause_columns_are_copied_including_null(src_url, dst_url):
    src = open_store(src_url, T0)
    try:
        put(src, "attempts", {"run_id": "r1", "task_id": "t1", "seq": 2, "cause": "flake", "cause_why": "retry passed"})
        put(src, "attempts", {"run_id": "r1", "task_id": "t1", "seq": 3, "cause": None, "cause_why": None})
    finally:
        src.close()
    sc.copy(src_url, dst_url, T0)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_all("SELECT seq, cause, cause_why FROM attempts WHERE seq IN (2, 3) ORDER BY seq") == [
            (2, "flake", "retry passed"),
            (3, None, None),
        ]
    finally:
        dst.close()


def test_the_runs_host_column_is_copied_including_null(src_url, dst_url):
    src = open_store(src_url, T0)
    try:
        put(src, "runs", {"run_id": "rh1", "host": "build-host-1"})
        put(src, "runs", {"run_id": "rh2", "host": None})
    finally:
        src.close()
    sc.copy(src_url, dst_url, T0)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_all("SELECT run_id, host FROM runs WHERE run_id IN ('rh1', 'rh2') ORDER BY run_id") == [
            ("rh1", "build-host-1"),
            ("rh2", None),
        ]
    finally:
        dst.close()


def test_the_leases_progress_column_is_copied_including_null(src_url, dst_url):
    src = open_store(src_url, T0)
    try:
        put(src, "leases", {"name": "lp1", "holder": "me", "epoch": 1, "progress": "3/7 tasks"})
        put(src, "leases", {"name": "lp2", "holder": "me", "epoch": 1, "progress": None})
    finally:
        src.close()
    sc.copy(src_url, dst_url, T0)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_all("SELECT name, progress FROM leases WHERE name IN ('lp1', 'lp2') ORDER BY name") == [
            ("lp1", "3/7 tasks"),
            ("lp2", None),
        ]
    finally:
        dst.close()


def test_every_source_row_lands_in_the_destination(src_url, dst_url):
    report = sc.copy(src_url, dst_url, T0)
    assert report["node_calls"] == {"source": 2, "copied": 2, "refreshed": 0, "present": 0}
    assert {n: r["copied"] for n, r in report.items()} == {n: len(SEED[n]) for n in EXPECTED_ORDER}
    assert counts(dst_url) == {n: len(SEED[n]) for n in EXPECTED_ORDER}
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_one("SELECT graph_id FROM runs") == ("g1",)
        assert dst.query_all("SELECT call_id, cost_usd, ok, node_id FROM node_calls ORDER BY call_id") == [
            ("c1", 0.25, 1, "n1"),
            ("c2", 0.5, 0, None),
        ]
        assert json.loads(dst.query_one("SELECT definition_json FROM graphs")[0]) == {"x": 1}
    finally:
        dst.close()


def test_a_chair_actions_row_is_in_the_destination_after_a_copy(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_all("SELECT ts, epoch, holder, kind, target, status FROM chair_actions") == [
            ("2026-09-26T00:00:00Z", 4, "me", "land", "t1", "done")
        ]
    finally:
        dst.close()


def test_a_changed_host_state_is_refreshed_in_the_destination(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    _update_source(src_url, "UPDATE hosts SET state = 'draining' WHERE name = 'host-a'")
    again = sc.copy(src_url, dst_url, T0)
    assert (again["hosts"]["copied"], again["hosts"]["refreshed"]) == (0, 1)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_one("SELECT state FROM hosts WHERE name = 'host-a'") == ("draining",)
    finally:
        dst.close()


def test_a_bumped_id_sequence_counter_is_refreshed_in_the_destination(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    _update_source(src_url, "UPDATE id_sequence SET next = 7 WHERE scope = 'initiative'")
    again = sc.copy(src_url, dst_url, T0)
    assert (again["id_sequence"]["copied"], again["id_sequence"]["refreshed"]) == (0, 1)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_one("SELECT next FROM id_sequence WHERE scope = 'initiative'") == (7,)
    finally:
        dst.close()


def test_a_second_copy_inserts_nothing_and_reports_every_row_present(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    again = sc.copy(src_url, dst_url, T0)
    assert {n: (r["copied"], r["present"]) for n, r in again.items()} == {n: (0, len(SEED[n])) for n in EXPECTED_ORDER}
    assert counts(dst_url) == {n: len(SEED[n]) for n in EXPECTED_ORDER}


def test_the_hosts_threads_and_capabilities_columns_are_copied_including_null(src_url, dst_url):
    src = open_store(src_url, T0)
    try:
        put(src, "hosts", {"name": "lane-host", "ssh": "lane-host", "capacity": 2, "state": "active", "beat_at": "",
                           "versions_json": "{}", "updated_at": T0, "updated_by": "chair", "threads": 6,
                           "capabilities_json": '["gpu"]'})
    finally:
        src.close()
    sc.copy(src_url, dst_url, T0)
    dst = open_store(dst_url, T0)
    try:
        rows = dst.query_all("SELECT name, threads, capabilities_json FROM hosts ORDER BY name")
        assert [(n, t, None if c is None else json.loads(c)) for n, t, c in rows] == [
            ("host-a", 0, None),
            ("lane-host", 6, ["gpu"]),
        ]
    finally:
        dst.close()


def test_a_changed_step_row_in_the_source_is_not_copied_over_the_first(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    _update_source(src_url, "UPDATE step_results SET ok = 0")
    again = sc.copy(src_url, dst_url, T0)
    assert (again["step_results"]["copied"], again["step_results"]["refreshed"]) == (0, 0)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_one("SELECT ok FROM step_results") == (1,)
    finally:
        dst.close()


def _update_source(src_url, sql):
    src = open_store(src_url, T0)
    try:
        src.execute(sql)
    finally:
        src.close()


def test_a_changed_lease_is_refreshed_in_the_destination(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    _update_source(src_url, "UPDATE leases SET epoch = 5, expires_at = '2026-09-26T00:00:00Z' WHERE name = 'epic'")
    again = sc.copy(src_url, dst_url, T0)
    assert (again["leases"]["copied"], again["leases"]["refreshed"]) == (0, 1)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_one("SELECT epoch, expires_at FROM leases WHERE name = 'epic'") == (5, "2026-09-26T00:00:00Z")
    finally:
        dst.close()


def test_a_changed_node_call_is_left_as_it_was_in_the_destination(src_url, dst_url):
    sc.copy(src_url, dst_url, T0)
    _update_source(src_url, "UPDATE node_calls SET cost_usd = 9.0 WHERE call_id = 'c1'")
    again = sc.copy(src_url, dst_url, T0)
    assert (again["node_calls"]["copied"], again["node_calls"]["refreshed"]) == (0, 0)
    dst = open_store(dst_url, T0)
    try:
        assert dst.query_one("SELECT cost_usd FROM node_calls WHERE call_id = 'c1'") == (0.25,)
    finally:
        dst.close()


def test_a_batch_smaller_than_the_table_still_copies_every_row(src_url, dst_url):
    assert sc.copy(src_url, dst_url, T0, batch=1)["node_calls"]["copied"] == 2


def test_a_destination_with_extra_rows_still_passes_the_check(src_url, dst_url):
    dst = open_store(dst_url, T0)
    try:
        put(dst, "runs", {"run_id": "extra"})
        put(dst, "leases", {"name": "other"})
    finally:
        dst.close()
    report = sc.copy(src_url, dst_url, T0)
    assert report["runs"] == {"source": 1, "copied": 1, "refreshed": 0, "present": 0}
    assert (counts(dst_url)["runs"], counts(dst_url)["leases"]) == (2, 2)


def test_a_shortfall_raises_with_the_report_and_main_exits_one(src_url, dst_url, monkeypatch, capsys):
    monkeypatch.setattr(sc, "_count", lambda conn, table: 0)
    with pytest.raises(sc.CopyCheckFailed) as failed:
        sc.copy(src_url, dst_url, T0)
    assert failed.value.report["runs"]["source"] == 1
    assert sc.main([src_url, dst_url]) == 1
    assert "FAIL runs: destination has 0, source has 1" in capsys.readouterr().out


def test_main_prints_a_line_per_table_and_exits_zero(src_url, dst_url, capsys):
    assert sc.main([src_url, dst_url]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "node_calls source=2 copied=2 refreshed=0 present=0" in lines
    assert len(lines) == 1 + len(EXPECTED_ORDER)


def test_main_json_is_one_object(src_url, dst_url, capsys):
    assert sc.main([src_url, dst_url, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["ok"], out["tables"]["leases"]) == (True, {"source": 1, "copied": 1, "refreshed": 0, "present": 0})


def test_safe_url_keeps_only_scheme_and_path():
    assert sc._safe_url("postgresql://user:hunter2@db.example:5432/store?sslmode=require") == "postgresql:///store"
    assert sc._safe_url("sqlite:////tmp/x.db") == "sqlite:////tmp/x.db"


def test_a_password_never_reaches_output_even_when_the_copy_fails(src_url, capsys):
    assert sc.main([src_url, "postgresql://user:hunter2@127.0.0.1:1/store"]) == 2
    seen = capsys.readouterr()
    assert "hunter2" not in seen.out + seen.err


NEWEST = len(default_modules())


def _one_additive_migration_newer(url):
    """The store at `url` as a build one additive migration newer leaves it: a new table holding a row, a new column
    on runs, and the schema_version row that says additive."""
    conn = open_store(url, T0)
    try:
        conn.execute("CREATE TABLE newer_notes (key TEXT NOT NULL, PRIMARY KEY (key))")
        conn.execute("INSERT INTO newer_notes (key) VALUES ('kept')")
        conn.execute("ALTER TABLE runs ADD COLUMN newer_note TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                     (NEWEST + 1, T0, "additive: newer notes"))
    finally:
        conn.close()


def test_exact_refusal_takes_only_this_codes_version():
    assert exact_refusal("source", NEWEST, NEWEST) is None
    assert "older than" in exact_refusal("source", NEWEST - 1, NEWEST)
    newer = exact_refusal("source", NEWEST + 2, NEWEST)
    assert newer.startswith(f"the source store is at schema version {NEWEST + 2}, newer than the {NEWEST}")
    assert f"migration {NEWEST + 1:04d}-{NEWEST + 2:04d}" in newer


def test_a_copy_from_a_store_one_additive_migration_newer_is_refused_not_truncated(src_url, tmp_path, capsys):
    _one_additive_migration_newer(src_url)
    dst = f"sqlite:///{tmp_path / 'fresh-dst.db'}"
    with pytest.raises(NotExactVersion, match=f"the source store is at schema version {NEWEST + 1}, newer"):
        sc.copy(src_url, dst, T0)
    assert not (tmp_path / "fresh-dst.db").exists()  # refused before the destination was opened
    assert sc.main([src_url, dst]) == 2
    err = capsys.readouterr().err
    assert f"would drop what migration {NEWEST + 1:04d} adds" in err


def test_a_copy_into_a_store_one_additive_migration_newer_is_refused_and_writes_no_row(src_url, tmp_path):
    dst = f"sqlite:///{tmp_path / 'newer-dst.db'}"
    _one_additive_migration_newer(dst)
    with pytest.raises(NotExactVersion, match="the destination store is at schema version"):
        sc.copy(src_url, dst, T0)
    assert counts(dst)["runs"] == 0
