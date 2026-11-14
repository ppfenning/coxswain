"""A reused phase branch is brought up to the initiative branch before any of its tasks builds.

The initiative branch is a phase's only parent, and it does not float: it moves when a sibling phase merges into it.
At `task` there is no initiative branch and a phase is never refreshed."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import landing_store
from harness.store_migrate import open_store
from harness.store_write import Store
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
from tests.test_epic_waves import Gated

INIT = "init/demo-initiative"
P1 = "epic/demo-initiative/p1-foundations"
PA, PB = "epic/demo-initiative/pa", "epic/demo-initiative/pb"
EDIT_SHARED = "diff --git a/shared.md b/shared.md\n--- a/shared.md\n+++ b/shared.md\n@@ -1 +1 @@\n-one\n+two\n"


def commit_on_main(repo: Path, name: str, text: str) -> None:  # noqa: F811
    (repo / name).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", f"main adds {name}", cwd=repo)


def one_store() -> Store:
    """One store across the runs of a test, as a deployment has: the landing record outlives a run."""
    return Store(open_store("sqlite:///:memory:", "2026-11-12T00:00:00+00:00"))


def task(ident: str, phase: str, state: str = "ready") -> dict:
    return {"id": ident, "phase": phase, "state": state, "needs": [], "surfaces": [], "title": ident, "body": ident,
            **({"merged": True} if state == "approved" else {})}


def siblings(*tasks: dict) -> dict:
    """Two phases with no edge between them: they build side by side and each merges into the initiative branch."""
    return {**initiative(), "phases": ["pa", "pb"], "items": list(tasks)}


def scripted(**patches: str) -> Gated:
    """A runner that knows the sibling tasks' ids; `patches` replace the one-new-file patch each task builds."""
    runner = Gated()
    runner.patches.update({name.replace("_", "-"): patch for name, patch in patches.items()})
    return runner


def builds(runner: Runner) -> list[str]:
    return [c["prompt"] for c in runner.calls if c["role"] == "build"]


def by_phase(result: dict) -> dict[str, dict]:
    return {p["phase"]: p for p in result["phases"]}


def test_a_reused_phase_takes_what_a_sibling_merged_and_builds_on_it_then_merges_again(repo, cart, tmp_path) -> None:  # noqa: F811
    store = one_store()
    first, _ = drive(
        repo, cart, tmp_path, store=store, run_id="epic-1", work=siblings(task("ta-one", "pa"), task("tb-one", "pb")),
        runner=scripted(ta_one=new_file_patch("shared.md", "one")),
    )
    assert {p["initiative_merge"]["status"] for p in first["phases"]} == {"merged"}
    assert not is_ancestor(repo, PA, PB), "pb was cut before pa merged"

    work = siblings(task("ta-one", "pa", "approved"), task("tb-one", "pb", "approved"), task("tb-two", "pb"))
    result, runner = drive(repo, cart, tmp_path, store=store, run_id="epic-2", work=work, runner=scripted(tb_two=EDIT_SHARED))

    pb = by_phase(result)["pb"]
    assert pb["refreshed"].startswith(f"refreshed {PB} onto {INIT}: head ")
    assert len(builds(runner)) == 1
    assert next(t for t in result["tasks"] if t["id"] == "tb-two")["state"] == "approved"
    assert git("show", f"{PB}:shared.md", cwd=repo) == "two"
    # The phase had merged before; the work it gained since is merged too, and the record lists what each merge carried.
    assert pb["status"] == "complete" and pb["initiative_merge"]["status"] == "merged"
    assert git("show", f"{INIT}:shared.md", cwd=repo) == "two"
    assert landing_store.get_record(store.conn, "demo-initiative")["phases"]["pb"]["tasks"] == ["tb-one", "tb-two"]


def test_a_moved_default_ref_moves_neither_the_initiative_branch_nor_a_phase(repo, cart, tmp_path) -> None:  # noqa: F811
    """The parent is recorded when the branch is cut. Taking it forward is the lander's step, before the pull request."""
    store = one_store()
    one = initiative(two_phases=False)
    drive(repo, cart, tmp_path, store=store, run_id="epic-1", work={**one, "items": one["items"][:1]})
    cut_at = git("merge-base", INIT, "main", cwd=repo)
    commit_on_main(repo, "moved.md", "moved\n")

    t1, t2 = one["items"]
    work = {**one, "items": [{**t1, "state": "approved", "merged": True}, t2]}
    result, runner = drive(repo, cart, tmp_path, store=store, run_id="epic-2", work=work)

    record = result["phases"][0]
    assert record["status"] == "complete" and len(builds(runner)) == 1
    assert not is_ancestor(repo, "main", INIT) and not is_ancestor(repo, "main", P1)
    assert git("merge-base", INIT, "main", cwd=repo) == cut_at
    assert landing_store.get_record(store.conn, "demo-initiative")["base_sha"] == cut_at
    assert [p for p in result["proposals"] if p["kind"] == "stack_rebase"] == []


def test_a_refresh_that_conflicts_blocks_the_phase_names_the_path_and_builds_nothing(repo, cart, tmp_path) -> None:  # noqa: F811
    """Both siblings wrote one file. One merged; the other cannot take the initiative tip, and its work stays where it is."""
    store = one_store()
    first, _ = drive(
        repo, cart, tmp_path, store=store, run_id="epic-1", work=siblings(task("ta-one", "pa"), task("tb-one", "pb")),
        runner=scripted(ta_one=new_file_patch("shared.md", "from pa"), tb_one=new_file_patch("shared.md", "from pb")),
    )
    statuses = {p["phase"]: p["initiative_merge"]["status"] for p in first["phases"]}
    assert sorted(statuses.values()) == ["conflicted", "merged"]
    loser = next(p for p, s in statuses.items() if s == "conflicted")
    branch = f"epic/demo-initiative/{loser}"
    before = git("rev-parse", branch, cwd=repo)

    states = [task("ta-one", "pa", "approved"), task("tb-one", "pb", "approved"), task("ta-two" if loser == "pa" else "tb-two", loser)]
    result, runner = drive(repo, cart, tmp_path, store=store, run_id="epic-2", work=siblings(*states), runner=scripted())

    record = by_phase(result)[loser]
    assert record["status"] == "blocked" and "refreshed" not in record
    assert "shared.md" in record["reason"] and "was not built on the stale tree" in record["reason"]
    assert {"id": loser, "phase": loser, "grain": "phase", "reason": record["reason"]} in result["quarantined"]
    assert builds(runner) == []
    assert git("rev-parse", branch, cwd=repo) == before
    assert git("status", "--porcelain", cwd=repo) == ""


def test_a_phase_that_holds_the_initiative_tip_records_no_refreshed_entry(repo, cart, tmp_path) -> None:  # noqa: F811
    """A phase that has not merged yet (a task of it was refused) is reopened where it stands."""
    store = one_store()
    one = initiative(two_phases=False)
    first, _ = drive(repo, cart, tmp_path, store=store, run_id="epic-1", work=one, patches={"t1-probe": new_file_patch("t1-probe.txt")})
    assert first["phases"][0]["status"] == "partial" and first["phases"][0]["initiative_merge"]["status"] == "not_built"
    before = git("rev-parse", P1, cwd=repo)

    t1, t2 = one["items"]
    work = {**one, "items": [{**t1, "state": "approved", "merged": True}, t2]}
    result, runner = drive(repo, cart, tmp_path, store=store, run_id="epic-2", work=work, patches={"t2-bench": new_file_patch("t2-bench.txt")})

    record = result["phases"][0]
    assert record["reused_branch"] is True and "refreshed" not in record
    assert len(builds(runner)) == 1
    assert is_ancestor(repo, before, P1)


@pytest.mark.usefixtures("at_task")
def test_at_task_a_reused_phase_is_never_refreshed(repo, cart, tmp_path) -> None:  # noqa: F811
    one = initiative(two_phases=False)
    drive(repo, cart, tmp_path, run_id="epic-1", work={**one, "items": one["items"][:1]})
    before = git("rev-parse", P1, cwd=repo)

    t1, t2 = one["items"]
    work = {**one, "items": [{**t1, "state": "approved", "merged": True}, t2]}
    result, runner = drive(repo, cart, tmp_path, run_id="epic-2", work=work, patches={"t2-bench": new_file_patch("t2-bench.txt")})

    record = result["phases"][0]
    assert record["reused_branch"] is True and "refreshed" not in record
    assert len(builds(runner)) == 1 and is_ancestor(repo, before, P1)
    assert git("branch", "--list", "init/*", cwd=repo) == ""
