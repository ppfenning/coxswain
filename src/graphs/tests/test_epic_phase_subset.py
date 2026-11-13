"""`run_epic(only_phases=...)` runs the named phases and reads every parent outside the set from the work store."""

from __future__ import annotations

import pytest

from harness import epic as epic_module
from harness.epic import outside_parents_complete
from harness.store_migrate import open_store
from harness.store_write import Store
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    cart,
    drive,
    git,
    initiative,
    new_file_patch,
    repo,
)
from tests.test_epic_land_at import prs  # noqa: F401 -- a fake forge, so no run reaches `gh`

P1, P2 = "p1-foundations", "p2-rollout"
P1_BRANCH = f"epic/demo-initiative/{P1}"
CUTOVER = "t3-cutover"


def staged(land_at: str | None = None, *, unmerged: tuple[str, ...] = (), p1_state: str = "approved") -> dict:
    """The default initiative with every p1 task settled in the store, so only p2 has work to do."""
    work = initiative()
    items = [
        {**i, "state": p1_state, "merged": i["id"] not in unmerged} if i["phase"] == P1 else i for i in work["items"]
    ]
    return {**work, "items": items, **({"land_at": land_at} if land_at else {})}


def built(runner: Runner) -> list[str]:
    return [
        t
        for t in ("t1-probe", "t2-bench", CUTOVER)
        if any(t in c["prompt"] for c in runner.calls if c["role"] == "build")
    ]


@pytest.mark.usefixtures("at_task")
def test_a_subset_run_of_b_records_only_b_and_builds_on_the_settled_parent_branch(repo, cart, tmp_path) -> None:  # noqa: F811
    git("branch", P1_BRANCH, cwd=repo)
    result, runner = drive(repo, cart, tmp_path, work=staged(), only_phases=[P2])
    assert [p["phase"] for p in result["phases"]] == [P2]
    assert result["waves"] == [[P2]]
    assert result["phases"][0]["base"] == P1_BRANCH
    assert result["phases"][0]["status"] != "blocked"
    assert built(runner) == [CUTOVER]


def _one_store() -> Store:
    return Store(open_store("sqlite:///:memory:", "2026-11-12T00:00:00+00:00"))


@pytest.mark.parametrize("legacy", [False, True], ids=["init", "legacy-pr-branch"])
def test_a_subset_run_builds_on_the_initiative_branch_once_the_parent_is_merged_into_it(repo, cart, tmp_path, legacy) -> None:  # noqa: F811
    """A `pr/<initiative>` branch an earlier release left is kept as the initiative branch."""
    if legacy:
        git("branch", "pr/demo-initiative", cwd=repo)
    branch = "pr/demo-initiative" if legacy else "init/demo-initiative"
    store = _one_store()
    first, _ = drive(repo, cart, tmp_path, work=initiative(two_phases=False), store=store, run_id="epic-1")
    assert first["phases"][0]["initiative_merge"] == {**first["phases"][0]["initiative_merge"], "status": "merged", "branch": branch}

    result, runner = drive(repo, cart, tmp_path, work=staged(), only_phases=[P2], store=store, run_id="epic-2")

    assert [p["phase"] for p in result["phases"]] == [P2]
    assert result["phases"][0]["base"] == branch and result["phases"][0]["status"] != "blocked"
    assert built(runner) == [CUTOVER]
    assert git("merge-base", "--is-ancestor", P1_BRANCH, f"epic/demo-initiative/{P2}", cwd=repo) == ""


def test_a_subset_run_is_blocked_while_the_parent_is_built_but_not_merged_into_the_initiative_branch(repo, cart, tmp_path) -> None:  # noqa: F811
    git("branch", P1_BRANCH, cwd=repo)
    result, runner = drive(repo, cart, tmp_path, work=staged(), only_phases=[P2])
    (record,) = result["phases"]
    assert record["status"] == "blocked"
    assert record["reason"] == f"parent phase '{P1}' is not merged into init/demo-initiative yet; dependents start once it is"
    assert built(runner) == [] and result["quarantined"] == []


def test_at_task_an_approved_unmerged_parent_task_leaves_the_named_phase_blocked(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    result, runner = drive(repo, cart, tmp_path, work=staged("task", unmerged=("t2-bench",)), only_phases=[P2])
    (record,) = result["phases"]
    assert record["phase"] == P2 and record["status"] == "blocked"
    assert f"parent phase '{P1}'" in record["reason"]
    assert built(runner) == []


def test_at_task_a_merged_parent_lets_the_named_phase_run(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    git("branch", P1_BRANCH, cwd=repo)
    result, runner = drive(repo, cart, tmp_path, work=staged("task"), only_phases=[P2])
    assert [p["phase"] for p in result["phases"]] == [P2] and result["phases"][0]["status"] != "blocked"
    assert built(runner) == [CUTOVER]


def test_the_retired_level_reads_as_initiative_and_an_unmerged_parent_blocks_the_named_phase(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    git("branch", P1_BRANCH, cwd=repo)
    result, runner = drive(repo, cart, tmp_path, work=staged("level", unmerged=("t2-bench",)), only_phases=[P2])
    assert result["land_at"] == "initiative"
    assert result["phases"][0]["status"] == "blocked" and built(runner) == []
    assert prs == []


def test_a_parent_still_ready_blocks_the_named_phase_and_nothing_runs(repo, cart, tmp_path) -> None:  # noqa: F811
    result, runner = drive(repo, cart, tmp_path, work=staged(p1_state="ready"), only_phases=[P2])
    assert [(p["phase"], p["status"]) for p in result["phases"]] == [(P2, "blocked")]
    assert built(runner) == []


def test_an_unknown_phase_refuses_the_run_before_any_worktree_or_store_write(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(epic_module, "_record_run_exit", lambda *a, **k: pytest.fail("the refused run wrote an exit"))
    with pytest.raises(ValueError, match=f"unknown phase.*p9-nope.*{P1}, {P2}"):
        drive(repo, cart, tmp_path, work=staged(), only_phases=[P2, "p9-nope"])
    assert not (tmp_path / "worktrees").exists()


def test_two_independent_named_phases_run_in_separate_lanes_of_one_wave(repo, cart, tmp_path) -> None:  # noqa: F811
    work = initiative()
    items = [{**i, "needs": []} for i in work["items"]]
    runner = Runner({t: new_file_patch(f"{t}.txt") for t in ("t1-probe", "t2-bench", CUTOVER)})
    result, runner = drive(repo, cart, tmp_path, runner=runner, work={**work, "items": items}, only_phases=[P1, P2])
    assert result["waves"] == [[P1, P2]]
    assert sorted(p["lane"] for p in result["phases"]) == [1, 2]
    assert sorted(built(runner)) == ["t1-probe", "t2-bench", CUTOVER]


def test_no_subset_runs_every_phase_as_before(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, only_phases=None)
    assert [p["phase"] for p in result["phases"]] == [P1, P2]
    assert result["waves"] == [[P1], [P2]]


ITEMS = [
    {"id": "a1", "phase": "a", "state": "approved", "merged": True},
    {"id": "a2", "phase": "a", "state": "approved", "merged": False},
    {"id": "c1", "phase": "c", "state": "done", "merged": False},
    {"id": "d1", "phase": "d", "state": "ready", "merged": False},
]
PARENTS = {"b": {"a", "c", "d", "e"}, "x": {"b"}}


@pytest.mark.parametrize(
    ("land_at", "rows", "expected"),
    [
        ("ticket", None, {"a", "c"}),
        ("level", None, {"a", "c"}),
        ("task", None, {"c"}),
        ("task", [{"task_id": "a2", "state": "approved", "merged": True}], {"a", "c"}),
        ("ticket", [{"task_id": "a1", "state": "ready", "merged": False}], {"c"}),
    ],
)
def test_an_outside_parent_is_complete_when_every_task_meets_need_met(land_at, rows, expected) -> None:
    # d has a ready task, e has no tasks, and the named phase b is not outside itself.
    assert outside_parents_complete(ITEMS, rows, PARENTS, {"b"}, land_at) == expected
