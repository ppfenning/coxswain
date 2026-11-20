"""Step artifacts in object storage (F3): with `artifacts_url` in the provider profile, the scripted one-task epic,
started by `cox route launch epic` as a person starts it, writes each step's patch and check logs under
`v1/runs/<run>/<step>/<name>@<sha12>` before the step's row, and the row lists each key with its size and sha256.

Once against a `file://` folder, which is the single-machine case with no Garage, and once against `s3://` on the
session's Garage container with the `object_store` block naming the key variables. `cox runs artifacts` lists what the
rows name. Without the key, nothing is uploaded and the rows list nothing.

The read side (F3b), on both roots: the run's folder and its trace folder are deleted, and `cox runs log` and
`cox runs show` still print its check logs and its build transcript, read from the objects the step rows name, and
`cox setup doctor`'s `artifacts root` row writes, reads back and removes a probe object there. The scripted runner makes
no model call, so it writes no transcript; the test records the one node call a build makes (`SEED_CALL`), with its
trace file, before the launch, and the run's own save uploads it as a real build would.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

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
    """Each step's keys without their `@<sha12>` suffix; each suffix is first held to the sha256 its entry records."""
    for s in steps:
        assert "artifacts_error" not in s["result_json"], s
        for a in s["artifacts_json"]:
            assert {"key", "bytes", "sha256", "media"} <= set(a) <= {"key", "bytes", "sha256", "media", "scrubbed"}, a
            assert a["media"] != "text/x-diff" or "scrubbed" not in a, a  # the patch is stored as written
            assert a["key"].rpartition("@")[2] == a["sha256"][:12], a
    return {s["step"]: [a["key"].rpartition("@")[0] for a in s["artifacts_json"]] for s in steps}


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
    assert [e["key"] for e in _listed(sandbox)] == [a["key"] for s in steps for a in s["artifacts_json"]]


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


def _with_garage_profile(sandbox, object_store, prefix: str) -> None:
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


def test_an_s3_artifacts_url_on_garage_holds_each_steps_patch_and_logs(launchable, repo, object_store, tmp_path):
    sandbox = launchable.sandbox
    prefix = f"artifacts-{tmp_path.name}"
    _with_garage_profile(sandbox, object_store, prefix)
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


ALARMS = """import json, psycopg
with psycopg.connect({url!r}) as conn:
    rows = conn.execute("SELECT kind, status, action_json->>'stall', action_json->>'subject' FROM chair_actions "
                        "WHERE kind IN ('stall_alarm', 'stall_clear') ORDER BY ts, kind").fetchall()
print(json.dumps([list(r) for r in rows]))
"""


def test_a_broken_artifacts_root_finishes_the_run_marks_its_rows_and_the_chair_alarms_once(launchable, repo, tmp_path):
    sandbox = launchable.sandbox
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the root's folder should be", encoding="utf-8")
    _with_profile_lines(sandbox, f"artifacts_url: file://{blocker}/root\n")
    steps = _ran(launchable)
    by_step = {s["step"]: s for s in steps}
    for step in ("task/t1/build", "task/t1/checks"):
        assert by_step[step]["artifacts_json"] == [] and by_step[step]["result_json"]["artifacts_error"], by_step
    for step in set(by_step) - {"task/t1/build", "task/t1/checks"}:
        assert "artifacts_error" not in by_step[step]["result_json"], by_step
    assert sandbox.store_row(INITIATIVE, "t1")["state"] == "approved"
    assert {e["step"] for e in _listed(sandbox) if e.get("missing")} == {"task/t1/build", "task/t1/checks"}

    # The worker only marked the rows; a chair tick over the store raises the one alarm, and a second raises none.
    for _ in range(2):
        tick = sandbox.cox("chair", "run", "--once", timeout=180)
        assert tick.returncode == 0, tick.stdout + tick.stderr
    alarms = [r for r in sandbox.python_json(ALARMS.format(url=sandbox.store_url)) if r[2].startswith("artifacts_missing")]
    assert alarms == [["stall_alarm", "done", f"artifacts_missing:{RUN}", INITIATIVE]], alarms


# --- the read side (F3b): delete the run folder, and the run still shows its transcript and its check log ---------

# The node call a build makes, recorded before the launch: the scripted runner calls no model, so it records none.
SEED_CALL = """import json
from harness.store_migrate import open_store
from harness.store_write import Store
conn = open_store({url!r}, "2026-11-20T00:00:00Z")
Store(conn).record_call({{"id": "smoke-build-1", "task_id": "t1", "role": "build", "ok": True, "trace": {trace!r}}},
                        run_id={run!r}, seq=0)
conn.close()
print(json.dumps(True))
"""
TRANSCRIPT = '{"type":"assistant","message":{"content":[{"type":"text","text":"smoke build transcript"}]}}\n'


def _seeded_transcript(sandbox, tmp_path: Path) -> Path:
    trace = tmp_path / "model-trace" / "build-1.jsonl"
    trace.parent.mkdir()
    trace.write_text(TRANSCRIPT, encoding="utf-8")
    assert sandbox.python_json(SEED_CALL.format(url=sandbox.store_url, trace=str(trace), run=RUN)) is True
    return trace


def _local_logs(sandbox) -> dict[str, str]:
    """Each check's local log, by the number the bucket's name starts with, before the run folder goes."""
    folder = sandbox.workspace / "runs" / RUN / "checks" / "p1" / "t1"
    return {f"{int(p.stem):02d}": p.read_text(encoding="utf-8") for p in folder.glob("*.log") if p.stat().st_size}


def _without_the_run_folder_it_still_reads(sandbox, steps: list[dict], trace: Path, shown: str) -> None:
    runs = sandbox.workspace / "runs"
    by_step = {s["step"]: s for s in steps}
    assert [a["key"].rpartition("/")[2].rpartition("@")[0] for a in by_step["task/t1/build"]["artifacts_json"]] == [
        "patch.diff", "transcript-build-1.jsonl"], by_step["task/t1/build"]
    logs = _local_logs(sandbox)
    shutil.rmtree(runs / RUN)
    shutil.rmtree(runs / f"{RUN}-trace", ignore_errors=True)
    trace.unlink(missing_ok=True)  # the run's end may already have compacted it away

    log = sandbox.cox("runs", "log", RUN, "--runs-dir", str(runs))
    assert log.returncode == 0, log.stdout + log.stderr
    for item in by_step["task/t1/checks"]["artifacts_json"]:
        name = item["key"].rpartition("/")[2].rpartition("@")[0]
        assert f"== task/t1/checks {name} (from {shown})" in log.stdout, log.stdout
        if not item.get("scrubbed"):
            assert logs[name[:2]] in log.stdout, (name, log.stdout)

    show = sandbox.cox("runs", "show", RUN, "--runs-dir", str(runs))
    assert show.returncode == 0, show.stdout + show.stderr
    assert show.stdout == f"== task/t1/build transcript-build-1.jsonl (from {shown})\n{TRANSCRIPT}", show.stdout


def _doctor_row(sandbox) -> dict:
    done = sandbox.cox("setup", "doctor", "--json", timeout=300)
    rows = json.loads(done.stdout)["rows"]
    [row] = [r for r in rows if r["check"] == "artifacts root"] or [{"missing": rows}]
    return row


def test_a_file_root_still_shows_the_transcript_and_the_check_log_once_the_run_folder_is_gone(launchable, repo, tmp_path):
    sandbox = launchable.sandbox
    root = tmp_path / "artifacts"
    _with_profile_lines(sandbox, f"artifacts_url: file://{root}\n")
    trace = _seeded_transcript(sandbox, tmp_path)
    steps = _ran(launchable)
    _without_the_run_folder_it_still_reads(sandbox, steps, trace, f"file://{root}")
    before = sorted(p for p in root.rglob("*") if p.is_file())
    row = _doctor_row(sandbox)
    assert row.get("ok") is True and row["detail"].startswith("local folder: a probe object was written"), row
    assert sorted(p for p in root.rglob("*") if p.is_file()) == before  # the probe removed its object


def test_an_s3_root_on_garage_still_shows_the_transcript_and_the_check_log_once_the_run_folder_is_gone(
        launchable, repo, object_store, tmp_path):
    sandbox = launchable.sandbox
    prefix = f"artifacts-{tmp_path.name}"
    _with_garage_profile(sandbox, object_store, prefix)
    trace = _seeded_transcript(sandbox, tmp_path)
    steps = _ran(launchable)
    _without_the_run_folder_it_still_reads(sandbox, steps, trace, f"s3://{object_store.bucket}/{prefix}")
    row = _doctor_row(sandbox)
    assert row.get("ok") is True, row
    assert row["detail"] == f"s3 bucket {object_store.bucket}: a probe object was written, read back and removed", row

