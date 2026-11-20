"""F4, end to end: `cox setup store worker-role` creates a worker's scoped role on a real Postgres, the role connects
through `store_scoped.open_scoped` and does what the grant set allows, and Postgres refuses what it does not.

The store owner is the sandbox's provider profile URL (the session server's superuser), as on the machine the
operator runs the command from. The worker's password reaches the units' env file under the sandbox home and is never
printed; the test reads it from that file, as the doctor does. Roles are cluster objects, so each test names its own
login role and drops it at the end; the `cox_worker` group is shared, as it is on a real server. Nothing is stubbed.
"""

import json
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

from smoke import plan

ROLE_ENV, PASSWORD_ENV = "COX_WORKER_STORE_ROLE", "COX_WORKER_STORE_PASSWORD"
CHECK = "worker store role"
DOCTOR_TIMEOUT_S = 300

# Run as the owner: SQL from argv[2:] against argv[1], one statement each, printing each result's rows.
OWNER_SQL = """
import json, sys, psycopg
with psycopg.connect(sys.argv[1], autocommit=True) as conn:
    out = []
    for sql in sys.argv[2:]:
        cur = conn.execute(sql)
        out.append([list(r) for r in cur.fetchall()] if cur.description else cur.rowcount)
print(json.dumps(out, default=str))
"""

# Run as the worker, whose URL is in the environment: each statement on its own, reported as ok or its SQLSTATE.
WORKER_SQL = """
import json, os, sys
from harness.store_scoped import open_scoped
conn = open_scoped(os.environ["SMOKE_WORKER_URL"])
out = {}
for label, sql in json.loads(sys.argv[1]):
    try:
        conn.execute(sql)
        out[label] = "ok"
    except Exception as exc:
        out[label] = getattr(exc, "sqlstate", None) or type(exc).__name__
conn.close()
print(json.dumps(out))
"""

# Open a store through open_scoped and report what happened and the version after.
SCOPED_OPEN = """
import json, sys
from harness.store_dialect import connect
from harness.store_migrate import check_version
from harness.store_read import StoreVersionError
from harness.store_scoped import open_scoped
try:
    open_scoped(sys.argv[1]).close()
    outcome = "opened"
except StoreVersionError as exc:
    outcome = "refused: " + str(exc)
conn = connect(sys.argv[1])
print(json.dumps({"outcome": outcome, "version": check_version(conn)[0]}))
conn.close()
"""

# Make an empty database argv[2] on server argv[1] and migrate it one migration short of this tree.
OLDER_STORE = """
import sys, psycopg
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate
with psycopg.connect(sys.argv[1], autocommit=True) as admin:
    admin.execute(f'DROP DATABASE IF EXISTS "{sys.argv[2]}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{sys.argv[2]}"')
conn = connect(sys.argv[3])
migrate(conn, "2026-11-20T00:00:00Z", default_modules()[:-1])
conn.close()
"""

CLAIM_ROW = (
    "INSERT INTO phase_queue (initiative, phase, kind, repo, base_ref, branch, requires_json, state, outcome, reason, "
    "band, seq, budget_usd, queued_at, ready_at, claimed_by, claim_epoch, run_id, claimed_at, heartbeat_at, "
    "lease_until, attempts, updated_at, updated_by) VALUES ('ship', 'p1', 'build', 'repo', 'init/ship', "
    "'epic/ship/p1', '[]', 'ready', '', '', 1, 1, 0, '', '', '', 0, '', '', '', '', 0, '', 'chair')"
)
STEP_ROW = (
    "INSERT INTO step_results (run_id, step, initiative, phase, task_id, kind, round, ok, verdict, subject_sha, worker, "
    "claim_epoch, started_at, finished_at, cost_usd, result_json, artifacts_json) VALUES ('run-1', 'task/t1/build', "
    "'ship', 'p1', 't1', 'build', 0, 1, '', '', 'smoke-w1', 1, '', '', 0.5, '{}', '[]')"
)
GRANTS_SQL = ("SELECT table_name, privilege_type FROM information_schema.role_table_grants WHERE grantee = 'cox_worker' "
              "AND table_catalog = current_database() ORDER BY 1, 2")


def _owner(sandbox, url: str, *sql: str) -> list:
    done = subprocess.run([str(sandbox.install.python), "-c", OWNER_SQL, url, *sql], env=sandbox.env, cwd=sandbox.home,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _env_file(sandbox) -> Path:
    return sandbox.home / ".config" / "agent-tools" / "garage.env"


def _env_values(sandbox) -> dict[str, str]:
    lines = _env_file(sandbox).read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _worker_url(owner_url: str, login: str, password: str) -> str:
    parts = urlsplit(owner_url)
    return urlunsplit(parts._replace(netloc=f"{login}:{quote(password, safe='')}@{parts.netloc.rpartition('@')[2]}"))


@pytest.fixture
def worker_machine(sandbox) -> Iterator:
    """The sandbox, with this test's login role dropped afterwards (its grants live on the group, which stays)."""
    try:
        yield sandbox
    finally:
        login = _login(sandbox)
        _owner(sandbox, sandbox.store_url, f"DO $$BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login}') "
                                           f"THEN EXECUTE 'DROP OWNED BY {login}'; EXECUTE 'DROP ROLE {login}'; END IF; END$$")


def _worker(machine) -> str:
    """A worker name of this test's own: the hash that ends its database's name. Roles are server-wide, so two tests
    on one server must not share one."""
    return f"smoke-{urlsplit(machine.store_url).path.rsplit('_', 1)[-1]}"


def _login(machine) -> str:
    return "cox_w_" + _worker(machine).replace("-", "_")


def _setup(machine) -> subprocess.CompletedProcess[str]:
    done = machine.cox("setup", "store", "worker-role", _worker(machine))
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def test_the_role_is_created_once_and_a_repeat_changes_nothing(worker_machine):
    first = _setup(worker_machine)
    values = _env_values(worker_machine)
    assert values[ROLE_ENV] == _login(worker_machine) and len(values[PASSWORD_ENV]) >= 32
    assert stat.S_IMODE(_env_file(worker_machine).stat().st_mode) == 0o600
    assert values[PASSWORD_ENV] not in first.stdout + first.stderr
    assert "password: set and written to" in first.stdout
    grants = _owner(worker_machine, worker_machine.store_url, GRANTS_SQL)[0]
    assert ["step_results", "INSERT"] in grants and ["work_items", "SELECT"] in grants
    assert not any(t in {"work_items", "hosts"} and p != "SELECT" for t, p in grants)
    assert not any(t in {"land_queue", "leases", "chair_actions"} for t, _ in grants)

    text = _env_file(worker_machine).read_text(encoding="utf-8")
    again = _setup(worker_machine)
    assert "password: unchanged in" in again.stdout
    assert _env_file(worker_machine).read_text(encoding="utf-8") == text
    assert _owner(worker_machine, worker_machine.store_url, GRANTS_SQL)[0] == grants

    # A grant that drifted in by hand is taken away by the next run, which leaves exactly the grant set.
    _owner(worker_machine, worker_machine.store_url, "GRANT DELETE ON work_items TO cox_worker")
    assert ["work_items", "DELETE"] in _owner(worker_machine, worker_machine.store_url, GRANTS_SQL)[0]
    _setup(worker_machine)
    assert _owner(worker_machine, worker_machine.store_url, GRANTS_SQL)[0] == grants


def test_the_role_writes_its_results_and_claims_and_is_refused_everything_else(worker_machine):
    _setup(worker_machine)
    url = worker_machine.store_url
    _owner(worker_machine, url, CLAIM_ROW)
    values = _env_values(worker_machine)
    statements = [
        ("insert step", STEP_ROW),
        ("claim", "UPDATE phase_queue SET claimed_by = 'smoke-w1', claim_epoch = claim_epoch + 1, state = 'claimed' "
                  "WHERE initiative = 'ship' AND phase = 'p1'"),
        ("insert round", "INSERT INTO review_rounds (initiative, phase, round, head_sha, verdict, reason, revision_json, "
                         "evidence_step, run_id, worker, claim_epoch, cost_usd, decided_at) VALUES ('ship', 'p1', 1, "
                         "'abc', 'approve', '', '{}', 'phase/smoke/1', 'run-1', 'smoke-w1', 1, 0.1, '')"),
        ("change band", "UPDATE phase_queue SET band = 0 WHERE initiative = 'ship'"),
        ("delete step", "DELETE FROM step_results"),
        ("approve task", "UPDATE work_items SET state = 'approved'"),
        ("move host", "UPDATE hosts SET name = name"),
        ("enqueue land", "DELETE FROM land_queue"),
        ("create table", "CREATE TABLE smoke_worker_table (x INTEGER)"),
    ]
    done = subprocess.run([str(worker_machine.install.python), "-c", WORKER_SQL, json.dumps(statements)],
                          env={**worker_machine.env, "SMOKE_WORKER_URL": _worker_url(url, _login(worker_machine), values[PASSWORD_ENV])},
                          cwd=worker_machine.home, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    outcomes = json.loads(done.stdout)
    assert outcomes == {
        "insert step": "ok", "claim": "ok", "insert round": "ok",
        "change band": "42501", "delete step": "42501", "approve task": "42501", "move host": "42501",
        "enqueue land": "42501", "create table": "42501",
    }
    rows = _owner(worker_machine, url, "SELECT COUNT(*) FROM step_results",
                  "SELECT claimed_by, claim_epoch, band FROM phase_queue")
    assert rows == [[[1]], [["smoke-w1", 1, 1]]]


def test_a_role_widened_by_hand_is_put_back_by_the_next_run(worker_machine):
    _setup(worker_machine)
    url, login = worker_machine.store_url, _login(worker_machine)
    _owner(worker_machine, url, f"ALTER ROLE {login} CREATEROLE BYPASSRLS", f"GRANT DELETE ON work_items TO {login}",
           f"GRANT pg_write_all_data TO {login}", "GRANT pg_read_all_data TO cox_worker",
           f"ALTER ROLE {login} SET statement_timeout = 0")
    state = (f"SELECT rolcreaterole, rolbypassrls, rolsuper, rolinherit, has_table_privilege('{login}', 'work_items', "
             f"'DELETE'), pg_has_role('{login}', 'pg_write_all_data', 'MEMBER'), "
             f"pg_has_role('{login}', 'pg_read_all_data', 'MEMBER'), pg_has_role('{login}', 'cox_worker', 'MEMBER') "
             f"FROM pg_roles WHERE rolname = '{login}'")
    settings = (f"SELECT setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
                f"WHERE r.rolname = '{login}'")
    assert _owner(worker_machine, url, state)[0] == [[True, True, False, True, True, True, True, True]]
    _setup(worker_machine)
    assert _owner(worker_machine, url, state, settings) == [
        [[False, False, False, True, False, False, False, True]],
        [[["idle_in_transaction_session_timeout=15s"]]],
    ]
    values = _env_values(worker_machine)
    done = subprocess.run([str(worker_machine.install.python), "-c", WORKER_SQL,
                           json.dumps([["delete work_items", "DELETE FROM work_items"]])],
                          env={**worker_machine.env, "SMOKE_WORKER_URL": _worker_url(url, login, values[PASSWORD_ENV])},
                          cwd=worker_machine.home, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"delete work_items": "42501"}


def test_the_password_reaches_the_server_only_as_a_scram_verifier(worker_machine):
    _setup(worker_machine)
    url, login = worker_machine.store_url, _login(worker_machine)
    password = _env_values(worker_machine)[PASSWORD_ENV]
    ((stored,),) = _owner(worker_machine, url, f"SELECT rolpassword FROM pg_authid WHERE rolname = '{login}'")[0]
    assert stored.startswith("SCRAM-SHA-256$4096:") and password not in stored
    # Postgres's own verifier for the same password and salt is the one stored: the worker logs in with it.
    salt = stored.split("$")[1].split(":")[1]
    check = ("import base64, json, sys; from harness.store_roles import scram_verifier; "
             "print(json.dumps(scram_verifier(sys.argv[1], base64.b64decode(sys.argv[2]))))")
    done = subprocess.run([str(worker_machine.install.python), "-c", check, password, salt], env=worker_machine.env,
                          cwd=worker_machine.home, capture_output=True, text=True, timeout=60)
    assert json.loads(done.stdout) == stored


def _doctor_row(machine) -> dict:
    done = subprocess.run([str(machine.install.cox), "setup", "doctor", "--json"], env=machine.env, cwd=machine.home,
                          capture_output=True, text=True, timeout=DOCTOR_TIMEOUT_S)
    return next(r for r in json.loads(done.stdout)["rows"] if r["check"] == CHECK)


def test_public_create_as_before_postgres_15_fails_the_row_with_the_fix_and_setup_can_apply_it(worker_machine):
    _setup(worker_machine)
    _owner(worker_machine, worker_machine.store_url, "GRANT CREATE ON SCHEMA public TO PUBLIC")
    row = _doctor_row(worker_machine)
    assert row["ok"] is False and "create table: allowed" in row["detail"]
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in row["detail"]
    done = worker_machine.cox("setup", "store", "worker-role", _worker(worker_machine), "--revoke-public-create")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _doctor_row(worker_machine)["ok"] is True


def test_the_doctor_row_passes_for_the_role_and_rolls_its_probes_back(worker_machine):
    _setup(worker_machine)
    done = subprocess.run([str(worker_machine.install.cox), "setup", "doctor", "--json"], env=worker_machine.env,
                          cwd=worker_machine.home, capture_output=True, text=True, timeout=DOCTOR_TIMEOUT_S)
    rows = {row["check"]: row for row in json.loads(done.stdout)["rows"]}
    assert rows[CHECK]["ok"] is True, rows[CHECK]
    assert "forbidden ones are refused" in rows[CHECK]["detail"]
    assert _env_values(worker_machine)[PASSWORD_ENV] not in done.stdout + done.stderr
    assert _owner(worker_machine, worker_machine.store_url, "SELECT COUNT(*) FROM step_results",
                  "SELECT COUNT(*) FROM review_rounds") == [[[0]], [[0]]]


def test_the_doctor_row_fails_when_the_password_is_wrong(worker_machine):
    _setup(worker_machine)
    path = _env_file(worker_machine)
    values = _env_values(worker_machine)
    path.write_text(f"{ROLE_ENV}={values[ROLE_ENV]}\n{PASSWORD_ENV}=not-the-password\n", encoding="utf-8")
    done = subprocess.run([str(worker_machine.install.cox), "setup", "doctor", "--json"], env=worker_machine.env,
                          cwd=worker_machine.home, capture_output=True, text=True, timeout=DOCTOR_TIMEOUT_S)
    row = next(r for r in json.loads(done.stdout)["rows"] if r["check"] == CHECK)
    assert row["ok"] is False and row["detail"].startswith("cannot use the role")


def test_open_scoped_refuses_an_older_store_and_migrates_nothing(sandbox, postgres):
    name = plan.database_name("worker-role-older-store")
    url = plan.with_database(postgres, name)
    done = subprocess.run([str(sandbox.install.python), "-c", OLDER_STORE, postgres, name, url], env=sandbox.env,
                          cwd=sandbox.home, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    try:
        older = sandbox.python_json(f"import sys; sys.argv = ['', {url!r}]\n{SCOPED_OPEN}")
        current = sandbox.python_json(f"import sys; sys.argv = ['', {sandbox.store_url!r}]\n{SCOPED_OPEN}")
    finally:
        _owner(sandbox, postgres, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    newest = current["version"]
    assert current == {"outcome": "opened", "version": newest}
    assert older["version"] == newest - 1
    assert older["outcome"].startswith("refused: ") and "never migrates" in older["outcome"]
