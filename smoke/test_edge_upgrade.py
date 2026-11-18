"""H5, end to end: a real `cox upgrade` on an edge machine rolls back on a failing required doctor row and not on an
advisory one.

The machine is installed as `cox setup install` lays a home out (`conftest.edge_machine`): its checkout is this tree,
cloned from the sandbox forge at the older of two commits on main, its venv synced by uv and `cox` a uv tool editable
from it. Both commits are reported green on the forge, so the upgrade's CI query (`forge_forgejo.green_commits`, picked
for an origin on the forge's host) chooses the newer one. The move runs for real: git, `uv sync`, the tool reinstall
and the moved checkout's own doctor. Nothing is stubbed. No chair or lander unit file exists, so no unit restarts.
"""

import json
import socket
import subprocess

import pytest

from smoke import plan

pytestmark = pytest.mark.forge

UPGRADE_TIMEOUT_S = 900


def _record(machine) -> dict:
    return json.loads(machine.record.read_text(encoding="utf-8"))


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]  # closed once the probe is: nothing listens there


def test_an_advisory_doctor_failure_does_not_roll_the_upgrade_back(edge_machine):
    # The sandbox has no token file, so the doctor's `claude token` row fails; that row is advisory.
    assert not (edge_machine.sandbox.home / ".config" / "agent-tools" / "claude-token").exists()
    base, newer = edge_machine.origin.base, edge_machine.origin.newer
    assert edge_machine.head() == base

    done = edge_machine.cox("upgrade", timeout=UPGRADE_TIMEOUT_S)

    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert f"the newest green commit on main is {newer[:12]}" in output
    assert "warning: doctor advisory rows failed (not rolled back)" in output
    assert "claude token" in output
    assert f"edge: upgraded {base[:12]} -> {newer[:12]}" in output
    assert edge_machine.head() == newer
    assert (edge_machine.checkout / "smoke-edge.txt").is_file()
    record = _record(edge_machine)
    assert (record["previous"], record["chosen"], record["outcome"]) == (base, newer, "upgraded")


def test_a_required_doctor_failure_rolls_the_upgrade_back(edge_machine):
    # The store the provider profile names is on a port nothing listens on: the doctor's store rows fail, and an
    # upgrade requires them.
    provider = edge_machine.sandbox.home / ".config" / "agent-tools" / "provider.yaml"
    provider.write_text(plan.provider_profile_text(plan.closed_port_url(edge_machine.sandbox.store_url, _closed_port())),
                        encoding="utf-8")
    base, newer = edge_machine.origin.base, edge_machine.origin.newer

    done = edge_machine.cox("upgrade", timeout=UPGRADE_TIMEOUT_S)

    output = done.stdout + done.stderr
    assert done.returncode == 1, output
    assert "doctor required rows failed" in output
    assert f"edge: rolled back {newer[:12]} -> {base[:12]}" in output
    assert edge_machine.head() == base
    assert not (edge_machine.checkout / "smoke-edge.txt").exists()
    record = _record(edge_machine)
    assert (record["previous"], record["chosen"], record["outcome"]) == (base, newer, "rolled_back")
    assert "store" in record["detail"]

    # The next run holds the rolled-back commit rather than trying it again every tick.
    again = edge_machine.cox("upgrade", timeout=UPGRADE_TIMEOUT_S)
    assert again.returncode == 0, again.stdout + again.stderr
    assert f"edge: {newer[:12]} was rolled back or failed" in again.stdout
    assert edge_machine.head() == base


def test_a_hand_run_upgrade_reads_the_forge_settings_from_the_units_env_file(edge_machine):
    # The forge's address and token are only in the env file the units read, not in the shell: as over ssh. The upgrade
    # itself needs them (its CI query picks Forgejo by FORGEJO_BASE_URL and reads with FORGEJO_TOKEN), before any doctor.
    names = (plan.FORGEJO_BASE_URL_ENV, plan.FORGEJO_TOKEN_ENV)
    env = edge_machine.sandbox.env
    garage = edge_machine.sandbox.home / ".config" / "agent-tools" / "garage.env"
    garage.write_text("".join(f"{name}={env[name]}\n" for name in names), encoding="utf-8")
    garage.chmod(0o600)
    shell = {key: value for key, value in env.items() if key not in names}
    base, newer = edge_machine.origin.base, edge_machine.origin.newer

    done = subprocess.run([str(edge_machine.sandbox.home / ".local" / "bin" / "cox"), "upgrade"], env=shell,
                          cwd=edge_machine.sandbox.home, capture_output=True, text=True, timeout=UPGRADE_TIMEOUT_S)

    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert f"env: loaded {', '.join(names)} from {garage}, as the units read it" in output
    assert f"edge: upgraded {base[:12]} -> {newer[:12]}" in output
    assert env[plan.FORGEJO_TOKEN_ENV] not in output
    assert edge_machine.head() == newer
