"""A reused level branch that predates the default ref is brought up to it before any task in the level builds."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from harness.epic import _run_phase
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    cart,
    drive,
    git,
    initiative,
    is_ancestor,
    new_file_patch,
    repo,
)
from tests.test_epic_stale_phase_worktree import _ctx

P1, P2 = "epic/demo-initiative/p1-foundations", "epic/demo-initiative/p2-rollout"
EDIT_SHARED = "diff --git a/shared.md b/shared.md\n--- a/shared.md\n+++ b/shared.md\n@@ -1 +1 @@\n-one\n+two\n"


def commit_on_main(repo: Path, name: str, text: str) -> None:  # noqa: F811
    (repo / name).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", f"main adds {name}", cwd=repo)


def p1_with_only_t1(*, t2: str | None) -> dict[str, Any]:
    """p1 holding t1, approved, and t2 ready beside it when `t2` is given."""
    work = initiative(two_phases=False)
    t1, second = work["items"]
    if t2 is None:
        return {**work, "items": [t1]}
    return {**work, "items": [{**t1, "state": "approved", "merged": True}, {**second, "state": t2}]}


def builds(runner: Runner) -> list[str]:
    return [c["prompt"] for c in runner.calls if c["role"] == "build"]


def test_a_level_cut_before_a_main_commit_is_refreshed_and_builds_on_the_file(repo, cart, tmp_path) -> None:  # noqa: F811
    drive(repo, cart, tmp_path, work=p1_with_only_t1(t2=None), run_id="epic-1")
    commit_on_main(repo, "shared.md", "one\n")
    assert not is_ancestor(repo, "main", P1)

    result, runner = drive(
        repo,
        cart,
        tmp_path,
        work=p1_with_only_t1(t2="ready"),
        run_id="epic-2",
        patches={"t2-bench": EDIT_SHARED},
    )

    record = result["phases"][0]
    assert record["refreshed"].startswith(f"refreshed {P1} onto main: head ")
    assert len(builds(runner)) == 1
    assert next(t for t in result["tasks"] if t["id"] == "t2-bench")["state"] == "approved"
    assert is_ancestor(repo, "main", P1)
    assert git("show", f"{P1}:shared.md", cwd=repo) == "two"


def two_levels(*, t2: str | None, t3_patch: str) -> tuple[dict, dict[str, str]]:
    """p1 (t1 approved at `t1_commit`, optional t2) and p2 (t3 ready, needing t1), with the patches their tasks build."""
    items = {i["id"]: i for i in initiative()["items"]}
    rows = [{**items["t1-probe"], "state": "approved", "merged": True}]
    rows += [{**items["t2-bench"], "state": t2, "merged": t2 == "approved"}] if t2 else []
    rows.append(items["t3-cutover"])
    return {**initiative(), "items": rows}, {"t2-bench": new_file_patch("t2-bench.txt"), "t3-cutover": t3_patch}


def first_run_of_two_levels(repo, cart, tmp_path, t3_patch: str) -> str:  # noqa: F811
    """Run t1 and t3 once, so both level branches exist; the commit p1 held for t1."""
    first = initiative()
    first["items"] = [i for i in first["items"] if i["id"] != "t2-bench"]
    drive(
        repo,
        cart,
        tmp_path,
        work=first,
        run_id="epic-1",
        patches={"t1-probe": new_file_patch("t1-probe.txt"), "t3-cutover": t3_patch},
    )
    return git("rev-parse", P1, cwd=repo)


def pin_t1(work: dict, t1_commit: str) -> dict:
    return {**work, "items": [{**i, "merged_commit": t1_commit} if i["id"] == "t1-probe" else i for i in work["items"]]}


def test_a_child_whose_parent_is_not_yet_current_takes_the_default_ref_itself(repo, cart, tmp_path) -> None:  # noqa: F811
    """Levels can start side by side, so a parent may still be stale when its child opens."""
    first = _ctx(repo, tmp_path, "r1")
    old_main = git("rev-parse", "main", cwd=repo)
    parent, child = first.phase_branch("pa"), first.phase_branch("pc")
    git("branch", parent, "main", cwd=repo)
    git("branch", child, "main", cwd=repo)
    commit_on_main(repo, "shared.md", "one\n")
    items = [
        {"id": "ta", "phase": "pa", "state": "approved", "merged": True, "merged_commit": old_main, "needs": []},
        {"id": "tc", "phase": "pc", "state": "approved", "merged": True, "needs": ["ta"]},
    ]

    record = _run_phase(
        replace(_ctx(repo, tmp_path, "r2"), cartridge=cart),
        initiative={"id": "x"},
        phase="pc",
        parents=("pa",),
        items=items,
    )

    assert record["refreshed"].startswith(f"refreshed {child} onto main: head ")
    assert is_ancestor(repo, "main", child)
    assert not is_ancestor(repo, "main", parent), "the parent is another level's to refresh"


def refresh_p1_alone(repo, cart, tmp_path) -> None:  # noqa: F811
    """A run of p1 only: it refreshes p1 onto main and builds t2, and leaves p2 as it was."""
    result, _ = drive(
        repo,
        cart,
        tmp_path,
        work=p1_with_only_t1(t2="ready"),
        run_id="epic-2",
        patches={"t2-bench": new_file_patch("t2-bench.txt")},
    )
    assert "refreshed" in result["phases"][0] and is_ancestor(repo, "main", P1) and not is_ancestor(repo, "main", P2)


def test_a_child_level_is_restacked_onto_its_refreshed_parent(repo, cart, tmp_path) -> None:  # noqa: F811
    t1_commit = first_run_of_two_levels(repo, cart, tmp_path, new_file_patch("t3-cutover.txt"))
    commit_on_main(repo, "shared.md", "one\n")
    refresh_p1_alone(repo, cart, tmp_path)
    # Epic-1's draft branch for t3 is still there; left in place it stops this run's draft, and so its merge.
    git("branch", "-D", f"{P2}--t3-cutover", cwd=repo)
    work, patches = two_levels(t2="approved", t3_patch=EDIT_SHARED)

    result, runner = drive(repo, cart, tmp_path, work=pin_t1(work, t1_commit), run_id="epic-3", patches=patches)

    p2 = next(p for p in result["phases"] if p["phase"] == "p2-rollout")
    assert p2["refreshed"].startswith(f"refreshed {P2} onto {P1}: head ")
    assert len(builds(runner)) == 1
    assert next(t for t in result["tasks"] if t["id"] == "t3-cutover")["state"] == "approved"
    assert is_ancestor(repo, P1, P2)
    assert git("show", f"{P2}:shared.md", cwd=repo) == "two"


def test_a_child_restack_that_conflicts_blocks_the_level_and_names_the_path(repo, cart, tmp_path) -> None:  # noqa: F811
    t1_commit = first_run_of_two_levels(repo, cart, tmp_path, new_file_patch("clash.md", "branch"))
    commit_on_main(repo, "clash.md", "main\n")
    refresh_p1_alone(repo, cart, tmp_path)
    before = git("rev-parse", P2, cwd=repo)
    work, patches = two_levels(t2="approved", t3_patch=EDIT_SHARED)

    result, runner = drive(repo, cart, tmp_path, work=pin_t1(work, t1_commit), run_id="epic-3", patches=patches)

    p2 = next(p for p in result["phases"] if p["phase"] == "p2-rollout")
    assert p2["status"] == "blocked" and "refreshed" not in p2
    assert "clash.md" in p2["reason"] and "was not built on the stale tree" in p2["reason"]
    assert {"id": "p2-rollout", "phase": "p2-rollout", "grain": "phase", "reason": p2["reason"]} in result[
        "quarantined"
    ]
    assert builds(runner) == []
    assert git("rev-parse", P2, cwd=repo) == before
    assert git("status", "--porcelain", cwd=repo) == ""


def test_a_child_with_no_commits_of_its_own_is_recreated_from_its_current_parent(repo, cart, tmp_path) -> None:  # noqa: F811
    old_main = git("rev-parse", "main", cwd=repo)
    git("branch", P1, "main", cwd=repo)
    git("branch", P2, "main", cwd=repo)
    commit_on_main(repo, "shared.md", "one\n")
    git("branch", "-f", P1, "main", cwd=repo)
    assert is_ancestor(repo, "main", P1) and not is_ancestor(repo, "main", P2)
    work, patches = two_levels(t2="ready", t3_patch=EDIT_SHARED)

    result, _ = drive(repo, cart, tmp_path, work=pin_t1(work, old_main), run_id="epic-1", patches=patches)

    p2 = next(p for p in result["phases"] if p["phase"] == "p2-rollout")
    assert p2["refreshed"].startswith(f"refreshed {P2} onto {P1}: head ")
    assert next(t for t in result["tasks"] if t["id"] == "t3-cutover")["state"] == "approved"
    assert is_ancestor(repo, "main", P2)


def test_a_refresh_that_conflicts_blocks_the_level_and_builds_nothing(repo, cart, tmp_path) -> None:  # noqa: F811
    drive(
        repo,
        cart,
        tmp_path,
        work=p1_with_only_t1(t2=None),
        run_id="epic-1",
        patches={"t1-probe": new_file_patch("shared.md", "branch")},
    )
    commit_on_main(repo, "shared.md", "main\n")
    before = git("rev-parse", P1, cwd=repo)

    result, runner = drive(repo, cart, tmp_path, work=p1_with_only_t1(t2="ready"), run_id="epic-2")

    record = result["phases"][0]
    assert record["status"] == "blocked" and "refreshed" not in record
    assert "shared.md" in record["reason"] and "was not built on the stale tree" in record["reason"]
    assert [q for q in result["quarantined"] if q["grain"] == "phase"] == [
        {"id": "p1-foundations", "phase": "p1-foundations", "grain": "phase", "reason": record["reason"]}
    ]
    assert builds(runner) == []
    assert git("rev-parse", P1, cwd=repo) == before
    assert git("status", "--porcelain", cwd=repo) == ""


def test_a_current_stack_records_no_refreshed_entry(repo, cart, tmp_path) -> None:  # noqa: F811
    drive(repo, cart, tmp_path, work=p1_with_only_t1(t2=None), run_id="epic-1")
    before = git("rev-parse", P1, cwd=repo)

    result, runner = drive(
        repo,
        cart,
        tmp_path,
        work=p1_with_only_t1(t2="ready"),
        run_id="epic-2",
        patches={"t2-bench": new_file_patch("t2-bench.txt")},
    )

    record = result["phases"][0]
    assert record["reused_branch"] is True and "refreshed" not in record
    assert len(builds(runner)) == 1
    assert is_ancestor(repo, before, P1)
