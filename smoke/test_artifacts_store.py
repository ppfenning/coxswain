"""Step artifacts in object storage (F3): with `artifacts_url` in the provider profile, the scripted one-task epic,
started by `cox route launch epic` as a person starts it, writes each step's patch and check logs under
`v1/runs/<run>/<step>/<name>` before the step's row, and the row lists each key with its size and sha256.

Once against a `file://` folder, which is the single-machine case with no Garage, and once against `s3://` on the
session's Garage container with the `object_store` block naming the key variables. `cox runs artifacts` lists what the
rows name. Without the key, nothing is uploaded and the rows list nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess

import pytest

from smoke import plan
from smoke.test_harness_launch import INITIATIVE, SUMMARY, _filed, _launched, _responses, _steps

psycopg = pytest.importorskip("psycopg")

RUN = "ship-1"
PROVIDER = ".config/agent-tools/provider.yaml"


def _with_profile_lines(sandbox, text: str) -> None:
    provider = sandbox.home / PROVIDER
    provider.write_text(provider.read_text(encoding="utf-8") + text, encoding="utf-8")


def _ran(launchable) -> list[dict]:
    sandbox = launchable.sandbox
    _filed(sandbox)
    _responses(launchable)
    log = _launched(sandbox, RUN)
    assert SUMMARY.format(run=RUN) in log, log[-3000:]
    return _steps(sandbox, RUN)


def _keys(steps: list[dict]) -> dict[str, list[str]]:
    return {s["step"]: [a["key"] for a in s["artifacts_json"]] for s in steps}


def _expected(sandbox) -> dict[str, list[str]]:
    """The patch on the build step; on the checks step, one log per check that printed anything, in the cartridge's
    order, as the local run folder holds them (an empty log is not uploaded)."""
    base, local = f"v1/runs/{RUN}", sandbox.workspace / "runs" / RUN / "checks" / "p1" / "t1"
    logs = [f"{base}/task__t1__checks/{n:02d}-{c['name']}.log" for n, c in enumerate(plan.smoke_checks("x"), 1)
            if (local / f"{n}.log").stat().st_size]
    assert logs, "no check printed anything, so the checks step would carry no log"
    return {"task/t1/build": [f"{base}/task__t1__build/patch.diff"], "task/t1/checks": logs}


def _listed(sandbox) -> list[dict]:
    done = sandbox.cox("runs", "artifacts", RUN, "--runs-dir", str(sandbox.workspace / "runs"), "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)["artifacts"]


def _check_rows(sandbox, steps: list[dict], read) -> None:
    keys = _keys(steps)
    for step, wanted in _expected(sandbox).items():
        assert keys[step] == wanted, steps
    assert all(not v for k, v in keys.items() if k not in ("task/t1/build", "task/t1/checks")), keys
    for step in steps:
        for item in step["artifacts_json"]:
            data = read(item["key"])
            assert (item["bytes"], item["sha256"]) == (len(data), hashlib.sha256(data).hexdigest()), item
    build = next(s for s in steps if s["step"] == "task/t1/build")
    assert plan.SMOKE_NOTE in read(build["artifacts_json"][0]["key"]).decode("utf-8")
    assert [e["key"] for e in _listed(sandbox)] == [k for s in steps for k in keys[s["step"]]]


def test_a_file_artifacts_url_holds_each_steps_patch_and_logs_under_its_key(launchable, repo, tmp_path):
    root = tmp_path / "artifacts"
    _with_profile_lines(launchable.sandbox, f"artifacts_url: file://{root}\n")
    steps = _ran(launchable)
    _check_rows(launchable.sandbox, steps, lambda key: (root / key).read_bytes())
    # the local run folder is unchanged: the checks still log there too
    assert (launchable.sandbox.workspace / "runs" / RUN / "checks" / "p1" / "t1" / "1.log").is_file()
    row = launchable.sandbox.store_row(INITIATIVE, "t1")
    assert row["state"] == "approved", row


def test_without_artifacts_url_no_step_row_lists_an_artifact(launchable, repo):
    steps = _ran(launchable)
    assert steps and all(s["artifacts_json"] == [] for s in steps), steps
    assert _listed(launchable.sandbox) == []


# Read through the install's interpreter, which carries pyarrow; the test process may not.
_S3_READ = """import base64, json, os, sys
from pyarrow.fs import S3FileSystem
key_env, secret_env, endpoint, region, path = sys.argv[1:6]
fs = S3FileSystem(access_key=os.environ[key_env], secret_key=os.environ[secret_env], scheme="http", endpoint_override=endpoint, region=region)
with fs.open_input_stream(path) as stream:
    print(json.dumps(base64.b64encode(stream.read()).decode()))
"""


def test_an_s3_artifacts_url_on_garage_holds_each_steps_patch_and_logs(launchable, repo, object_store, tmp_path):
    sandbox = launchable.sandbox
    prefix = f"artifacts-{tmp_path.name}"
    _with_profile_lines(sandbox, "".join([
        f"artifacts_url: s3://{object_store.bucket}/{prefix}\n",
        "object_store:\n",
        f"  endpoint: {object_store.endpoint}\n",
        f"  region: {plan.GARAGE_REGION}\n",
        f"  access_key_env: {plan.GARAGE_ACCESS_KEY_ENV}\n",
        f"  secret_key_env: {plan.GARAGE_SECRET_KEY_ENV}\n",
        "  path_style: true\n",
    ]))
    sandbox.env[plan.GARAGE_ACCESS_KEY_ENV] = object_store.access_key
    sandbox.env[plan.GARAGE_SECRET_KEY_ENV] = object_store.secret_key
    steps = _ran(launchable)
    endpoint = object_store.endpoint.removeprefix("http://")

    def read(key: str) -> bytes:
        done = subprocess.run(
            [str(sandbox.install.python), "-c", _S3_READ, plan.GARAGE_ACCESS_KEY_ENV, plan.GARAGE_SECRET_KEY_ENV, endpoint,
             plan.GARAGE_REGION, f"{object_store.bucket}/{prefix}/{key}"],
            env=sandbox.env, capture_output=True, text=True, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr
        return base64.b64decode(json.loads(done.stdout))

    _check_rows(sandbox, steps, read)
