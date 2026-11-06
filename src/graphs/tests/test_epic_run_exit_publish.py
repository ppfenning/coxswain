"""The run's `run_exited` publish: the argv it builds, its best-effort failures, and its one call from the epic run.

Driver fixtures repeat the idiom from `test_epic_courier_notify.py`; the tests directory has no package
`__init__.py`, so cross-file test imports are not this suite's convention.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from graphs._spec import GraphSpec
from graphs.delivery import lifecycle_propose, phase_validate
from harness import courier_adapter, epic
from harness.epic import run_epic
from harness.store_migrate import open_store
from harness.store_write import Store
from runner.claude_code_runner import files_touched_from_patch
from runner.protocol import RunnerError

# The autouse stub replaces the publisher; these tests exercise the real one.
adapter = pytest.mark.real_courier

SEND_ARGV = [
    "cox", "courier", "send", "coxswain://run/epic-1", "--to", "chair",
    "--note", "run epic-1 exited ok",
    "--event", "run_exited",
    "--field", "run_id=epic-1", "--field", "initiative=demo-initiative", "--field", "outcome=ok",
]


class Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


@pytest.fixture
def runs(monkeypatch):
    """Every subprocess.run the adapter makes, as (argv, timeout); the tools it looks up are present."""
    calls: list[tuple[list[str], float]] = []
    monkeypatch.delenv("COX_CHAIR_COURIER", raising=False)
    monkeypatch.setattr(courier_adapter.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(argv, timeout, **_kwargs):
        calls.append((argv, timeout))
        return Completed(0)

    monkeypatch.setattr(courier_adapter.subprocess, "run", fake_run)
    return calls


@adapter
def test_local_publish_runs_the_send_argv_on_the_local_bus(runs) -> None:
    assert courier_adapter.publish_run_exited("epic-1", "demo-initiative", "ok") is True
    assert runs == [(SEND_ARGV, 10)]


@adapter
def test_remote_publish_runs_the_send_argv_through_a_bounded_remote_shell(runs, monkeypatch) -> None:
    monkeypatch.setenv("COX_CHAIR_COURIER", "pat@chair-box")

    assert courier_adapter.publish_run_exited("epic-1", "demo-initiative", "ok") is True

    remote = (
        "cox courier send coxswain://run/epic-1 --to chair --note 'run epic-1 exited ok' --event run_exited "
        "--field run_id=epic-1 --field initiative=demo-initiative --field outcome=ok"
    )
    assert runs == [(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "pat@chair-box", remote], 15)]


@adapter
def test_a_nonzero_return_is_false(runs, monkeypatch) -> None:
    monkeypatch.setattr(courier_adapter.subprocess, "run", lambda argv, **_kw: Completed(1))
    assert courier_adapter.publish_run_exited("epic-1", "demo-initiative", "ok") is False


@adapter
@pytest.mark.parametrize(
    "error", [OSError("no route"), subprocess.TimeoutExpired(cmd="cox", timeout=10)], ids=["oserror", "timeout"]
)
def test_a_raised_failure_is_false_and_never_escapes(runs, monkeypatch, error) -> None:
    def boom(argv, **_kw):
        raise error

    monkeypatch.setattr(courier_adapter.subprocess, "run", boom)
    assert courier_adapter.publish_run_exited("epic-1", "demo-initiative", "ok") is False


@adapter
def test_a_missing_cox_is_false_without_running_anything(runs, monkeypatch) -> None:
    monkeypatch.setattr(courier_adapter.shutil, "which", lambda name: None)
    assert courier_adapter.publish_run_exited("epic-1", "demo-initiative", "ok") is False
    assert runs == []


# --- the epic run -------------------------------------------------------------------------------------------

APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
CHUNK_OK = {"satisfied": True, "gaps": [], "reasoning": "the description is satisfied"}
GOAL_MET = {
    "goal_met": True, "partial": False, "missing": [],
    "quarantine_blocks_dependents": False, "reasoning": "the pieces add up",
}
CHECK_SCRIPT = """\
import pathlib
import sys

bad = sorted(p.name for p in pathlib.Path(".").glob("*.txt") if p.read_text().strip() != "ok")
print(f"{len(bad)} failed" if bad else "1 passed")
sys.exit(1 if bad else 0)
"""
PATCH = (
    "diff --git a/t1-probe.txt b/t1-probe.txt\nnew file mode 100644\n--- /dev/null\n+++ b/t1-probe.txt\n"
    "@@ -0,0 +1 @@\n+ok\n"
)


class Runner:
    capabilities: ClassVar[dict[str, bool]] = {"tool_use": True}

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def run(self, *, role, schema, prompt, **_kwargs):
        if role == "plan":
            return {"steps": ["do it"], "files_expected": ["x.txt"], "out_of_scope": []}
        if role == "build":
            return {
                "patch": PATCH, "summary": "built t1-probe",
                "files_touched": files_touched_from_patch(PATCH), "commands_run": [],
            }
        scripted = {
            "review_charter": APPROVE, "validate_chunk": CHUNK_OK, "validate_phase": GOAL_MET,
            "work_state_arm": {"applied": True, "detail": "state moved"}, "style_pass": {"patch": ""},
        }
        if role in scripted:
            return dict(scripted[role])
        raise RunnerError(f"no scripted response for role '{role}'")


def git(*args: str, cwd: Path) -> None:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@invalid", "-c", "user.name=t", *args], cwd=cwd, capture_output=True, text=True
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"


def test_the_epic_run_publishes_once_with_the_recorded_status_even_when_the_publish_fails(
    tmp_path, monkeypatch, caplog
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "check.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base", cwd=repo)
    cart = {
        "team": "acme", "cartridge_sha": "sha-fixture", "context": [],
        "skills": {
            "plan": "acme-skills:plan", "build": "acme-skills:build", "review_charter": "acme-skills:review",
            "validate_chunk": "acme-skills:validate-chunk", "validate_phase": "acme-skills:validate-phase",
            "work_state_arm": "acme-skills:work-state",
        },
        "write_kinds": {
            "draft_pr_create": {"risk": "low", "ramp": "eligible", "apply_arm": "shell"},
            "merge_stack": {"risk": "high", "ramp": "eligible", "apply_arm": "shell"},
            "stack_rebase": {"risk": "high", "ramp": "eligible", "apply_arm": "shell"},
            "state_move": {"risk": "low", "ramp": "deferred", "apply_arm": "work_state_arm"},
            "self_modification": {"risk": "high", "ramp": "never", "apply_arm": "pr"},
            "consolidate": {"risk": "low", "ramp": "deferred"},
        },
        "policy": {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {}},
        "landing_areas": {
            "worktree_root": str(tmp_path / "worktrees"),
            "checks": [{"name": "state", "cmd": f"{sys.executable} check.py"}],
        },
    }
    work = {
        "id": "demo-initiative", "title": "demo", "body": "make the vendor join measurable end to end",
        "phases": ["p1-foundations"],
        "items": [{"id": "t1-probe", "phase": "p1-foundations", "state": "ready", "needs": [], "surfaces": [],
                   "title": "schema probe", "body": "read the vendor schema"}],
    }
    published: list[tuple[str, str, str]] = []

    def failing_publish(run_id: str, initiative: str, outcome: str) -> bool:
        published.append((run_id, initiative, outcome))
        return False

    monkeypatch.setattr(epic, "publish_run_exited", failing_publish)

    with caplog.at_level("WARNING"):
        result = run_epic(
            store=Store(open_store("sqlite:///:memory:", datetime.now(UTC).isoformat())),
            initiative=work, repo=repo, cartridge=cart, runner=Runner(),
            specs={
                "lifecycle": GraphSpec(name="lifecycle", graph_name="lifecycle-propose", run=lifecycle_propose.run),
                "validate": phase_validate.SPEC,
            },
            run_id="epic-1", date="2026-09-17", max_parallel=3, ledger_path=tmp_path / "ledger.jsonl",
            provider_profile="anthropic-default", runs_dir=tmp_path / "runs",
            worktree_root=cart["landing_areas"]["worktree_root"], assume="a",
        )

    assert result["run_id"] == "epic-1"
    assert published == [("epic-1", "demo-initiative", "ok")]
    assert "run_exited for epic-1 not published" in caplog.text
