"""H5.3, end to end: a hand-run `cox setup doctor` (an ssh or interactive shell, no unit env) judges the environment
the units see. The object_store keys live only in the env file the upgrade unit names; the doctor loads every variable
that file defines and its own environment lacks, so its required rows pass where they once failed and rolled a good
`cox upgrade` back. `cox upgrade` runs this same doctor as a child and loads the same file first.

Real processes throughout: the installed `cox`, a Garage container holding the traces bucket, the session's Postgres.
The upgrade unit is written by `cox upgrade --install-timer --environment-file`, as on a machine whose units read a
file other than the default.
"""

import json
import subprocess
from pathlib import Path

import pytest

# The doctor's own tier rule, from this tree (conftest puts src/tools on the path, as for the forge's branch reader),
# so the test and the upgrade agree on which rows are required: one reader of the rule, not two.
from agent_tools.doctor import failing_required
from smoke import plan

DOCTOR_TIMEOUT_S = 300
OBJECT_STORE_ROW = "shared store object_store"


@pytest.fixture
def shared_machine(sandbox, object_store):
    """The sandbox on a shared store: its Postgres and the Garage bucket for traces, the credentials named by the
    provider profile and present in no process environment."""
    config = sandbox.home / ".config" / "agent-tools"
    (config / "provider.yaml").write_text(
        plan.object_store_provider_text(sandbox.store_url, object_store.endpoint, object_store.bucket), encoding="utf-8")
    profile = config / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + "store:\n  mode: shared\n", encoding="utf-8")
    (sandbox.workspace / "intake").mkdir(exist_ok=True)  # the doctor's workspace row, which an upgrade requires, wants it
    assert plan.GARAGE_ACCESS_KEY_ENV not in sandbox.env and plan.GARAGE_SECRET_KEY_ENV not in sandbox.env
    return sandbox


def _env_file(machine, values: dict[str, str]) -> Path:
    """The keys in a NAME=value file the default does not name, as jarvis keeps `garage.systemd.env`."""
    path = machine.home / ".config" / "agent-tools" / "garage.systemd.env"
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    path.chmod(0o600)
    return path


def _keys(object_store, secret: str | None = None) -> dict[str, str]:
    return {plan.GARAGE_ACCESS_KEY_ENV: object_store.access_key, plan.GARAGE_SECRET_KEY_ENV: secret or object_store.secret_key}


def _doctor(machine, extra_env: dict[str, str] | None = None) -> tuple[dict[str, dict], list[dict], str]:
    """`cox setup doctor --json` as a person runs it in an ssh shell: the sandbox env, plus `extra_env` if given."""
    done = subprocess.run([str(machine.install.cox), "setup", "doctor", "--json"], env={**machine.env, **(extra_env or {})},
                          cwd=machine.home, capture_output=True, text=True, timeout=DOCTOR_TIMEOUT_S)
    rows = json.loads(done.stdout)["rows"]
    return {row["check"]: row for row in rows}, rows, done.stdout + done.stderr


def _install_upgrade_unit(machine, env_file: Path) -> None:
    profile = str(machine.home / ".config" / "agent-tools" / "profile.yaml")
    done = machine.cox("upgrade", "--install-timer", "--profile", profile, "--environment-file", str(env_file))
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_hand_run_doctor_loads_the_keys_from_the_file_the_upgrade_unit_names(shared_machine, object_store):
    # Before: no file holds the keys, and the object_store row fails as it did on bp-macbook.
    by_check, _rows, _out = _doctor(shared_machine)
    assert by_check[OBJECT_STORE_ROW]["ok"] is False
    assert "incomplete" in by_check[OBJECT_STORE_ROW]["detail"]

    env_file = _env_file(shared_machine, _keys(object_store))
    _install_upgrade_unit(shared_machine, env_file)

    by_check, rows, output = _doctor(shared_machine)
    assert failing_required(rows) == [], output
    assert by_check["shared store"]["ok"] is True, by_check["shared store"]
    assert object_store.secret_key not in output and object_store.access_key not in output


def test_the_process_environment_wins_over_the_units_file(shared_machine, object_store):
    env_file = _env_file(shared_machine, _keys(object_store))
    _install_upgrade_unit(shared_machine, env_file)

    # A wrong secret in the process environment is used, not the right one in the file: the bucket refuses it.
    wrong = "0" * len(object_store.secret_key)
    by_check, _rows, output = _doctor(shared_machine, {plan.GARAGE_SECRET_KEY_ENV: wrong})
    assert by_check[OBJECT_STORE_ROW]["ok"] is False, output
    assert "incomplete" not in by_check[OBJECT_STORE_ROW]["detail"]
