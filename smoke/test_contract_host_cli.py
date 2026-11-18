"""Contract: `cox host` -> `harness.store_cli host`, in process through `agent_tools.store_cli.runner`.

The unit tests of `cox host` replace the store CLI with doubles (`FakeStore` in src/tools/tests/test_host_login.py,
a fake refusal in test_host_deploy_only_cli.py), so an argv the store does not take passed there. Here every write goes
to the store, and the row is read back through the store CLI's own `host list`.
"""

import json

HOST = "lane-a"


def _rows(sandbox) -> dict[str, dict]:
    done = sandbox.harness("harness.store_cli", "--store-url", sandbox.store_url, "host", "list")
    assert done.returncode == 0, done.stderr
    return {row["name"]: row for row in json.loads(done.stdout)}


def _ok(sandbox, *args):
    done = sandbox.cox("host", *args)
    assert done.returncode == 0, f"cox host {' '.join(args)}: {done.stdout}{done.stderr}"
    return done


def test_a_host_is_added_resized_drained_reactivated_and_removed_through_the_store(sandbox):
    _ok(sandbox, "add", HOST, "--ssh", "lane-a.invalid", "--capacity", "2")
    assert "lane-a  active  cap 2  beat never" in _ok(sandbox, "list").stdout
    _ok(sandbox, "capacity", HOST, "3")
    assert (_rows(sandbox)[HOST]["capacity"], _rows(sandbox)[HOST]["state"]) == (3, "active")
    _ok(sandbox, "drain", HOST)
    assert _rows(sandbox)[HOST]["state"] == "draining"
    _ok(sandbox, "activate", HOST)
    assert _rows(sandbox)[HOST]["state"] == "active"
    _ok(sandbox, "drain", HOST)
    # cox sends `host delete`, which the store CLI did not have: every remove failed with "invalid choice: 'delete'"
    removed = _ok(sandbox, "remove", HOST).stdout
    assert any(line.startswith(f"removed {HOST}: ") for line in removed.splitlines()), removed
    assert f"restore: cox host add {HOST} --ssh lane-a.invalid --capacity 3" in removed
    assert HOST not in _rows(sandbox)
    assert HOST not in _ok(sandbox, "list").stdout


def test_a_login_is_merged_into_the_hosts_versions_and_leaves_its_beat(sandbox):
    _ok(sandbox, "add", HOST, "--ssh", "lane-a.invalid", "--capacity", "2")
    # cox sent `--login-ok/--reason/--failures/--by`; the store CLI takes `--login-json`, so every login exited 2
    _ok(sandbox, "login", HOST, "false", "--reason", "expired", "--failures", "2")
    row = _rows(sandbox)[HOST]
    assert row["versions_json"] == {"login_ok": False, "reason": "expired", "login_failures": 2}
    assert row["beat_at"] == ""
    _ok(sandbox, "login", HOST, "true")
    assert _rows(sandbox)[HOST]["versions_json"] == {"login_ok": True, "reason": None, "login_failures": 0}


def test_remove_refuses_an_active_host_and_the_store_keeps_it(sandbox):
    _ok(sandbox, "add", HOST, "--ssh", "lane-a.invalid", "--capacity", "2")
    refused = sandbox.cox("host", "remove", HOST)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert _rows(sandbox)[HOST]["state"] == "active"
