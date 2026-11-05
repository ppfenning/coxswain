"""`_run_phase(only=...)` builds a named subset of a phase and judges the phase once, when it is finished."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness import epic as epic_module
from harness.epic import _buildable, _Ctx, _earlier_merged, _run_phase
from harness.store_migrate import open_store
from harness.store_write import Store
from tests.test_epic_driver import (  # noqa: F401
    PROFILE,
    SPECS,
    Runner,
    cart,
    git,
    initiative,
    new_file_patch,
    repo,
)

PHASE = "p1-foundations"
A, B = "t1-probe", "t2-bench"
PATCHES = {A: new_file_patch(f"{A}.txt"), B: new_file_patch(f"{B}.txt")}


def _ctx(root: Path, cartridge: dict, tmp_path: Path, runner: Runner) -> _Ctx:
    return _Ctx(
        repo=root,
        cartridge=cartridge,
        runner=runner,
        specs=SPECS,
        run_id="epic-1",
        date="2026-09-01",
        max_parallel=3,
        ledger_path=tmp_path / "ledger.jsonl",
        provider_profile=PROFILE,
        runs_dir=tmp_path / "runs",
        worktree_root=Path(cartridge["landing_areas"]["worktree_root"]),
        assume="a",
        fix_attempts=None,
        initiative_id="demo-initiative",
        default_ref="main",
        # A merge's handoff is stamped on the task row only when the store owns task state.
        work_state="store",
        store=Store(open_store("sqlite:///:memory:", datetime.now(UTC).isoformat())),
    )


def _call(ctx: _Ctx, work: dict, only: frozenset[str] | None) -> dict:
    return _run_phase(ctx, initiative=work, phase=PHASE, parents=(), items=work["items"], only=only)


@pytest.fixture
def validator_inputs(monkeypatch) -> list[dict]:
    """The `phase_state` each validate_phase invocation was handed."""
    seen: list[dict] = []
    real = epic_module.invoke_graphs

    def spy(invocations, **kwargs):
        seen.extend(i.args["phase_state"] for i in invocations if "phase_state" in i.args)
        return real(invocations, **kwargs)

    monkeypatch.setattr(epic_module, "invoke_graphs", spy)
    return seen


def _builds(runner: Runner) -> list[str]:
    return [c["prompt"] for c in runner.calls if c["role"] == "build"]


def _built_ids(runner: Runner) -> set[str]:
    return {t for t in (A, B) if any(t in prompt for prompt in _builds(runner))}


def test_only_narrows_the_buildable_set() -> None:
    items = initiative(two_phases=False)["items"]
    assert [i["id"] for i in _buildable(items, PHASE, {})] == [A, B]
    assert [i["id"] for i in _buildable(items, PHASE, {}, frozenset({B}))] == [B]


def test_earlier_merged_takes_approved_items_outside_the_run_with_or_without_a_handoff() -> None:
    items = [
        {"id": "a", "phase": "p", "state": "approved", "handoff": {"files_changed": ["a.py"]}},
        {"id": "b", "phase": "p", "state": "approved", "handoff": None},
        {"id": "c", "phase": "p", "state": "ready", "handoff": {"files_changed": ["c.py"]}},
        {"id": "d", "phase": "q", "state": "approved", "handoff": {"files_changed": ["d.py"]}},
        {"id": "e", "phase": "p", "state": "approved", "handoff": {"files_changed": ["e.py"]}},
    ]
    assert [i["id"] for i in _earlier_merged(items, "p", {"e"})] == ["a", "b"]


def test_two_calls_build_one_task_each_on_one_branch_and_the_second_validates_the_whole_phase(
    repo, cart, tmp_path, validator_inputs  # noqa: F811
) -> None:
    runner = Runner(PATCHES)
    ctx = _ctx(repo, cart, tmp_path, runner)
    work = initiative(two_phases=False)
    epic_module._mirror_read(ctx, work["items"])

    first = _call(ctx, work, frozenset({A}))
    assert _built_ids(runner) == {A}
    assert [t["id"] for t in first["task_records"]] == [A]
    assert first["phase_verdict"]["skipped"].startswith("phase not finished: 1 open task(s)")
    assert validator_inputs == []

    # `run_epic` removes the run's worktrees when it ends; the branch stays for the next call to reuse.
    epic_module.remove_worktree(repo, ctx.worktree_root / ctx.run_id)

    second = _call(ctx, work, frozenset({B}))
    assert _built_ids(runner) == {A, B}
    assert [t["id"] for t in second["task_records"]] == [B]
    assert second["reused_branch"] is True
    assert second["branch"] == first["branch"]
    assert second["phase_verdict"]["goal_met"] is True

    assert len(validator_inputs) == 1
    entries = {t["id"]: t for t in validator_inputs[0]["tasks"]}
    assert set(entries) == {A, B}
    assert set(entries[A]) == set(entries[B])
    assert entries[A]["patch"] == ""
    assert entries[A]["evidence"] == []
    assert entries[A]["change_facts"]["files_changed"] == [f"{A}.txt"]
    assert entries[B]["patch"] != ""
    git("cat-file", "-e", f"{second['branch']}:{A}.txt", cwd=repo)
    git("cat-file", "-e", f"{second['branch']}:{B}.txt", cwd=repo)


def test_no_subset_builds_every_ready_task_and_validates_exactly_those(
    repo, cart, tmp_path, validator_inputs  # noqa: F811
) -> None:
    runner = Runner(PATCHES)
    ctx = _ctx(repo, cart, tmp_path, runner)
    work = initiative(two_phases=False)
    epic_module._mirror_read(ctx, work["items"])

    record = _call(ctx, work, None)

    assert _built_ids(runner) == {A, B}
    assert {t["id"] for t in record["task_records"]} == {A, B}
    assert len(validator_inputs) == 1
    assert {t["id"] for t in validator_inputs[0]["tasks"]} == {A, B}
    assert record["phase_verdict"]["goal_met"] is True
