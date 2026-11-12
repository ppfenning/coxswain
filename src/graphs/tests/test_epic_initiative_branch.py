"""At `initiative` a run cuts one initiative branch, cuts every phase from it, and merges each built phase back into it.

That local merge is "phase complete". The tasks stay approved: they are done when the initiative's one pull request
lands, which is the lander's work and not the run's. At `task` there is no initiative branch and no landing record."""

from __future__ import annotations

import pytest

from core import landing_record
from harness import landing_store
from harness.epic import _open_view
from harness.store_migrate import open_store
from harness.store_write import Store
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    cart,
    drive,
    git,
    initiative,
    is_ancestor,
    new_file_patch,
    repo,
)
from tests.test_epic_waves import Gated, by_phase, work

INITIATIVE = "demo-initiative"
INIT = f"init/{INITIATIVE}"
P1, P2 = f"epic/{INITIATIVE}/p1-foundations", f"epic/{INITIATIVE}/p2-rollout"


@pytest.fixture
def store():
    made = Store(open_store("sqlite:///:memory:", "2026-11-12T00:00:00+00:00"))
    try:
        yield made
    finally:
        made.conn.close()


def rows(result: dict) -> list[dict]:
    """The run's tasks as landing-record rows."""
    needs = {i["id"]: i["needs"] for i in initiative()["items"]}
    return [{"id": t["id"], "phase": t["phase"], "state": t["state"], "needs": needs[t["id"]]} for t in result["tasks"]]


def test_a_two_phase_run_cuts_both_phases_from_the_initiative_branch_and_merges_both_into_it(repo, cart, tmp_path, store) -> None:  # noqa: F811
    main = git("rev-parse", "main", cwd=repo)

    result, _ = drive(repo, cart, tmp_path, store=store)

    p1, p2 = result["phases"]
    assert (p1["base"], p2["base"]) == (INIT, INIT)
    assert (p1["status"], p2["status"]) == ("complete", "complete")
    assert (p1["initiative_merge"]["status"], p2["initiative_merge"]["status"]) == ("merged", "merged")
    assert is_ancestor(repo, P1, P2), "phase two was cut after phase one merged, so it holds phase one's work"
    assert is_ancestor(repo, P1, INIT) and is_ancestor(repo, P2, INIT)
    assert git("rev-parse", "main", cwd=repo) == main, "nothing reaches the parent from a run"

    record = landing_store.get_record(store.conn, INITIATIVE)
    assert (record["branch"], record["parent"], record["base_sha"]) == (INIT, "main", main)
    assert {phase: entry["state"] for phase, entry in record["phases"].items()} == {
        "p1-foundations": "merged", "p2-rollout": "merged",
    }
    assert record["phases"]["p2-rollout"]["merge_sha"] == git("rev-parse", INIT, cwd=repo)
    assert record["state"] == "assembled" and p2["initiative_merge"]["assembled"] is True
    assert landing_record.initiative_state(record, rows(result)) == "assembled"
    # Approved, never done: done means the initiative's pull request merged, and only the lander writes it.
    assert {t["id"]: t["state"] for t in result["tasks"]} == {
        "t1-probe": "approved", "t2-bench": "approved", "t3-cutover": "approved",
    }
    queue = {i["id"]: i["state"] for i in landing_store.list_items(store.conn)}
    assert set(queue.values()) == {"done"} and len(queue) == 2


@pytest.mark.usefixtures("at_task")
def test_at_task_the_same_run_cuts_no_initiative_branch_and_keeps_no_landing_record(repo, cart, tmp_path, store) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, store=store)

    assert result["land_at"] == "task"
    assert git("branch", "--list", "init/*", "pr/*", cwd=repo) == ""
    assert landing_store.get_record(store.conn, INITIATIVE) is None
    assert landing_store.list_items(store.conn) == []
    assert all("initiative_merge" not in p for p in result["phases"])
    assert result["phases"][0]["base"] == "main"


def test_a_phase_that_needs_two_phases_is_cut_once_both_merged_and_holds_both(repo, cart, tmp_path, store) -> None:  # noqa: F811
    """No assembly from several parents: the initiative branch already holds each of them."""
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []), ("tj-join", "pj", ["ta-one", "tb-one"]))

    result, runner = drive(repo, cart, tmp_path, store=store, runner=Gated(gate=True), work=spec)

    by = by_phase(result)
    assert runner.both.is_set(), "the two parents built side by side"
    assert result["waves"] == [["pa", "pb"], ["pj"]]
    assert by["pj"]["status"] == "complete" and by["pj"]["base"] == INIT and "assembled_from" not in by["pj"]
    join = f"epic/{INITIATIVE}/pj"
    assert is_ancestor(repo, f"epic/{INITIATIVE}/pa", join) and is_ancestor(repo, f"epic/{INITIATIVE}/pb", join)
    record = landing_store.get_record(store.conn, INITIATIVE)
    assert record["state"] == "assembled"
    assert record["waves"]["1"]["phases"] == ["pa", "pb"] and record["waves"]["2"]["phases"] == ["pj"]


def test_a_phase_with_a_task_that_did_not_build_is_not_merged_and_its_child_does_not_start(repo, cart, tmp_path, store) -> None:  # noqa: F811
    """The validator may say the goal is met around a quarantined task. The phase is still not whole, so it is not merged."""
    spec = work(("ta-one", "pa", []), ("ta-two", "pa", []), ("tb-one", "pb", ["ta-one"]))

    result, runner = drive(repo, cart, tmp_path, store=store, runner=Gated(fail=("ta-two",)), work=spec)

    by = by_phase(result)
    assert by["pa"]["status"] == "partial" and by["pa"]["initiative_merge"]["status"] == "not_built"
    assert "task(s) ta-two are not approved" in by["pa"]["reason"]
    assert by["pb"]["status"] == "blocked" and f"parent phase 'pa' is not merged into {INIT}" in by["pb"]["reason"]
    assert "tb-one" not in runner.begun
    assert not is_ancestor(repo, f"epic/{INITIATIVE}/pa", INIT)
    assert (landing_store.get_record(store.conn, INITIATIVE)["phases"] or {}).get("pa", {}).get("state") != "merged"


def test_a_phase_whose_merge_conflicts_stays_built_on_its_branch_and_its_child_does_not_start(repo, cart, tmp_path, store) -> None:  # noqa: F811
    """pa and pb write one file. Whichever merges second conflicts: nothing of it is lost, and nothing is built on it."""
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []), ("ta-two", "pc", ["ta-one"]), ("tb-two", "pd", ["tb-one"]))
    runner = Gated()
    runner.patches.update({"ta-one": new_file_patch("shared.md", "from pa"), "tb-one": new_file_patch("shared.md", "from pb")})

    result, runner = drive(repo, cart, tmp_path, store=store, work=spec, runner=runner)

    by = by_phase(result)
    lost = next(p for p in ("pa", "pb") if by[p]["initiative_merge"]["status"] == "conflicted")
    won, child_of_lost, child_of_won = ("pb", "pc", "pd") if lost == "pa" else ("pa", "pd", "pc")
    assert by[won]["status"] == "complete" and by[won]["initiative_merge"]["status"] == "merged"
    assert by[lost]["status"] == "awaiting_land"
    assert by[lost]["initiative_merge"]["paths"] == ["shared.md"] and by[lost]["initiative_merge"]["cause"] == "conflict"
    assert by[child_of_lost]["status"] == "blocked"
    assert f"parent phase '{lost}' is built but not merged into {INIT}" in by[child_of_lost]["reason"]
    assert by[child_of_won]["status"] == "complete"
    assert ("ta-two" if child_of_lost == "pc" else "tb-two") not in runner.begun
    # The conflicted phase keeps its branch and its approval; the initiative branch does not hold it.
    assert git("show", f"epic/{INITIATIVE}/{lost}:shared.md", cwd=repo) == f"from {lost}"
    assert not is_ancestor(repo, f"epic/{INITIATIVE}/{lost}", INIT)
    assert {t["id"]: t["state"] for t in result["tasks"]}["ta-one" if lost == "pa" else "tb-one"] == "approved"
    record = landing_store.get_record(store.conn, INITIATIVE)
    assert record["phases"][lost]["state"] == "conflicted" and record["phases"][lost]["conflict_paths"] == ["shared.md"]
    assert record["state"] == "building"


def test_with_an_initiative_branch_no_task_starts_before_its_whole_parent_phase_is_merged() -> None:
    """At `task` a task whose needs are merged starts early. An initiative branch holds a parent only once it merged whole."""
    items = [
        {"id": "a", "phase": "p1", "state": "approved", "merged": True, "merged_commit": "1" * 40, "needs": []},
        {"id": "b", "phase": "p1", "state": "ready", "needs": []},
        {"id": "c", "phase": "p2", "state": "ready", "needs": ["a"]},
    ]
    parents = {"p1": set(), "p2": {"p1"}}
    early = {i["id"]: i["state"] for i in _open_view(items, parents, set(), set())}
    whole = {i["id"]: i["state"] for i in _open_view(items, parents, set(), set(), whole_parents=True)}
    assert (early["c"], whole["c"]) == ("ready", "blocked")
    assert {i["id"]: i["state"] for i in _open_view(items, parents, {"p1"}, set(), whole_parents=True)}["c"] == "ready"
