"""Adopt over the four shapes an in-flight initiative was found in. No recorded commit is lost in any of them."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_tools import lander_adopt, lander_ports
from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_store as ls
from tests.test_lander import World, commit, run


@pytest.fixture
def world(tmp_path: Path) -> World:
    made = World(tmp_path)
    made.ports.repo = lambda record: made.repo  # no record exists yet to name it
    made.sent_back = []

    def send_back(initiative: str, task: str, reason: str) -> None:
        made.sent_back.append((task, reason))
        for row in made.rows[initiative]:
            if row["id"] == task:
                row["state"] = "ready"

    made.send_back = send_back
    return made


def task(ident: str, phase: str, state: str = "approved", commit_sha: str | None = None, needs=()) -> dict:
    return {"id": ident, "phase": phase, "state": state, "needs": list(needs), "merged_commit": commit_sha, "title": ident}


def phase(world: World, initiative: str, name: str, base: str, file: str, text: str = "x\n") -> str:
    branch = f"epic/{initiative}/{name}"
    run(world.repo, "branch", branch, base)
    return commit(world.repo, branch, file, text)


def adopt(world: World, initiative: str) -> list[str]:
    return lander_adopt.apply(world.ports, lander_adopt.plan(world.ports, initiative), world.send_back)


def snapshot(world: World) -> tuple:
    refs = run(world.repo, "for-each-ref", "--format=%(refname) %(objectname)")
    return refs, list(ls.list_records(world.conn).records), ls.list_items(world.conn), [dict(r) for rows in world.rows.values() for r in rows]


def assert_nothing_lost(world: World, initiative: str) -> None:
    """Every recorded commit of a task that is not done is on the initiative branch or still on its phase branch."""
    record = ls.get_record(world.conn, initiative)
    for row in world.rows[initiative]:
        sha = row.get("merged_commit")
        if not sha or row["state"] in ("done", "dropped", "ready"):
            continue
        on_phase = lg.is_ancestor(world.repo, sha, f"epic/{initiative}/{row['phase']}")
        assert lg.is_ancestor(world.repo, sha, record["branch"]) or on_phase, row["id"]


def test_a_straight_stack_with_nothing_landed_keeps_its_old_branch_and_merges_every_built_phase(world: World) -> None:
    """The old steward had merged phase one into `pr/<id>`; the last phase holds a quarantined human gate."""
    p1 = phase(world, "stack", "p1", "main", "p1.txt")
    p2 = phase(world, "stack", "p2", "epic/stack/p1", "p2.txt")
    run(world.repo, "branch", "pr/stack", "main")
    run(world.repo, "branch", "epic/stack/p3", "epic/stack/p2")
    lg.merge_into(world.repo, branch="pr/stack", source="epic/stack/p1", message="steward: merge p1")
    old_tip = run(world.repo, "rev-parse", "pr/stack")
    world.rows["stack"] = [task("a", "p1", commit_sha=p1), task("b", "p2", commit_sha=p2, needs=("a",)),
                           task("gate", "p3", "quarantined", needs=("b",))]

    found = lander_adopt.plan(world.ports, "stack")
    assert (found.branch, found.branch_exists, found.merge) == ("pr/stack", True, ("p1", "p2"))
    assert found.waiting == (("p3", "a task is quarantined; on_blocked applies once the record exists"),)
    events = lander_adopt.apply(world.ports, found, world.send_back)

    record = ls.get_record(world.conn, "stack")
    assert record["branch"] == "pr/stack" and record["adopted"] is True
    assert lg.is_ancestor(world.repo, old_tip, "pr/stack"), "the old branch only moved forward"
    assert {p: e["state"] for p, e in record["phases"].items()} == {"p1": "merged", "p2": "merged"}
    assert lr.phase_states(record, world.rows["stack"])["p3"] != "merged"
    assert set(world.forge.kinds()) <= {"push"} and world.sent_back == [], "only the phase merges' pushes"
    assert any("phase p2 of stack merged into pr/stack" in e for e in events)
    assert_nothing_lost(world, "stack")


def test_phases_landed_earlier_and_one_mixed_phase_marks_what_is_on_the_parent_done_and_leaves_the_rest_to_build(world: World) -> None:
    """Phase one landed as a squash. In phase two, one task's patch is on main, one is approved, one is still ready."""
    p1 = phase(world, "mixed", "p1", "main", "p1.txt")
    landed = phase(world, "mixed", "p2", "main", "landed.txt")
    approved = commit(world.repo, "epic/mixed/p2", "approved.txt", "approved\n")
    run(world.repo, "cherry-pick", p1)  # main now holds both under new shas, as a land leaves them
    run(world.repo, "cherry-pick", landed)
    world.rows["mixed"] = [task("a", "p1", "done", p1), task("b1", "p2", commit_sha=landed), task("b2", "p2", commit_sha=approved),
                           task("b3", "p2", "ready")]

    found = lander_adopt.plan(world.ports, "mixed")
    assert {t.task: t.action for t in found.tasks} == {"a": "none", "b1": "done", "b2": "keep", "b3": "none"}
    assert (found.branch, found.branch_exists, found.merge) == ("init/mixed", False, ())
    assert found.waiting == (("p2", "task(s) b3 are still to build; it is built on its branch as it stands"),)
    lander_adopt.apply(world.ports, found, world.send_back)

    states = {r["id"]: r["state"] for r in world.rows["mixed"]}
    assert states == {"a": "done", "b1": "done", "b2": "approved", "b3": "ready"}
    record = ls.get_record(world.conn, "mixed")
    assert record["base_sha"] == run(world.repo, "rev-parse", "main") and record["phases"] == {}
    assert lr.placeable_phases(record, world.rows["mixed"]) == ["p2"], "the ready task builds on the phase branch it has"
    assert_nothing_lost(world, "mixed")


def test_one_approved_task_left_becomes_a_small_initiative_ready_to_land(world: World) -> None:
    only = phase(world, "small", "p1", "main", "fix.txt")
    world.rows["small"] = [task("fix", "p1", commit_sha=only)]

    events = adopt(world, "small")

    record = ls.get_record(world.conn, "small")
    assert record["state"] == "assembled" and lg.is_ancestor(world.repo, only, "init/small")
    assert ls.get_item(world.conn, "initiative_land:small")["state"] == "queued"
    assert any("small is assembled: queued to land on main" in e for e in events)
    assert_nothing_lost(world, "small")


def test_an_initiative_with_everything_done_is_recorded_landed_and_nothing_else(world: World) -> None:
    world.rows["old"] = [task("a", "p1", "done"), task("b", "p2", "dropped", needs=("a",))]
    events = adopt(world, "old")
    assert ls.get_record(world.conn, "old")["state"] == "landed"
    assert ls.list_items(world.conn) == [] and events[-1] == "old: recorded landed"


def test_an_approval_off_its_phase_branch_is_carried_and_one_that_is_nowhere_goes_back_to_ready(world: World) -> None:
    on_phase = phase(world, "loose", "p1", "main", "kept.txt")
    run(world.repo, "branch", "agents/run-9/b", "main")
    stray = commit(world.repo, "agents/run-9/b", "stray.txt", "stray\n")
    world.rows["loose"] = [task("a", "p1", commit_sha=on_phase), task("b", "p1", commit_sha=stray), task("c", "p1", commit_sha="0" * 40)]

    found = lander_adopt.plan(world.ports, "loose")
    assert {t.task: t.action for t in found.tasks} == {"a": "keep", "b": "carry", "c": "ready"}
    assert found.merge == () and found.waiting[0][0] == "p1", "with c back to ready the phase is not built"
    lander_adopt.apply(world.ports, found, world.send_back)

    assert lg.is_ancestor(world.repo, stray, "epic/loose/p1") and lg.is_ancestor(world.repo, on_phase, "epic/loose/p1")
    assert [t for t, _ in world.sent_back] == ["c"] and "is not in this clone" in world.sent_back[0][1]
    assert_nothing_lost(world, "loose")


def test_the_plan_alone_writes_nothing_and_adopting_twice_changes_nothing(world: World) -> None:
    only = phase(world, "small", "p1", "main", "fix.txt")
    world.rows["small"] = [task("fix", "p1", commit_sha=only)]
    before = snapshot(world)
    lines = lander_adopt.plan(world.ports, "small").lines()
    assert snapshot(world) == before, "the dry run reads only"
    assert lines == ["small: branch init/small (cut from main), parent main", "  task fix (p1): keep", "  phase p1: merge into init/small"]

    adopt(world, "small")
    after = snapshot(world)
    assert adopt(world, "small") == ["small: skipped: it has a landing record already"]
    assert snapshot(world) == after
    assert lander_adopt.candidates(world.ports, ["small", "other"]) == ["other"]


def test_an_initiative_that_lands_at_task_is_not_adopted(world: World) -> None:
    world.rows["per-task"] = [task("a", "p1", "ready")]
    world.meta_of["per-task"] = {"land_at": "task"}
    assert lander_adopt.plan(world.ports, "per-task").skip == "it lands at task, which keeps no landing record"
    assert lander_adopt.plan(world.ports, "unknown").skip == "it has no tasks"


def test_an_initiative_whose_repository_is_not_known_is_skipped_and_git_never_runs_in_the_current_directory(world: World) -> None:
    world.ports.repo = None
    world.rows["nowhere"] = [task("a", "p1", "ready")]
    found = lander_adopt.plan(world.ports, "nowhere")
    assert found.skip == "nowhere: its repository is not known on this machine"
    assert lander_adopt.apply(world.ports, found, world.send_back) == ["nowhere: skipped: nowhere: its repository is not known on this machine"]
    assert ls.list_records(world.conn) == lr.Listing()


def _origin_only(world: World, tmp_path: Path, initiative: str) -> str:
    """A bare origin the clone pushed `main` to, and a commit another machine pushed to origin's phase branch: the
    clone has neither the branch nor the commit."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True)
    run(world.repo, "remote", "add", "origin", str(origin))
    run(world.repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], capture_output=True, check=True)
    run(other, "switch", "-q", "-c", f"epic/{initiative}/p1")
    sha = commit(other, f"epic/{initiative}/p1", "note.txt", "ok\n")
    run(other, "push", "-q", "origin", f"epic/{initiative}/p1")
    assert lg.rev(world.repo, sha) is None
    return sha


def test_an_approval_only_on_origins_phase_branch_is_fetched_kept_and_merged_not_sent_back(world: World, tmp_path: Path) -> None:
    sha = _origin_only(world, tmp_path, "far")
    world.ports.fetch = lander_ports.fetch_branch  # the phase merge's own fetch, as the lander runs it
    world.rows["far"] = [task("t1", "p1", commit_sha=sha)]
    heads_before = run(world.repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")

    assert lander_adopt.refresh(world.ports, "far") == []
    assert lg.rev(world.repo, "refs/remotes/origin/epic/far/p1") == sha
    assert run(world.repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads") == heads_before, \
        "the fetch moves no local branch"
    found = lander_adopt.plan(world.ports, "far")
    assert [(t.task, t.action) for t in found.tasks] == [("t1", "keep")] and found.merge == ("p1",), found
    events = lander_adopt.apply(world.ports, found, world.send_back)

    assert world.sent_back == [], events
    assert lg.is_ancestor(world.repo, sha, ls.get_record(world.conn, "far")["branch"]), events


def test_a_failed_fetch_is_reported_and_the_plan_is_made_against_the_clone_as_it_stands(world: World, tmp_path: Path) -> None:
    run(world.repo, "remote", "add", "origin", str(tmp_path / "gone.git"))
    world.rows["cut"] = [task("t1", "p1", commit_sha="0" * 40)]
    (said,) = lander_adopt.refresh(world.ports, "cut")
    assert said.startswith("cut: origin's phase branches could not be fetched (") and said.endswith(
        "; planned against this clone as it stands"), said
    assert [t.action for t in lander_adopt.plan(world.ports, "cut").tasks] == ["ready"]


def test_the_refspec_names_only_the_initiatives_phase_branches_on_remote_tracking_refs() -> None:
    assert lander_adopt.phase_refspec("ship") == "+refs/heads/epic/ship/*:refs/remotes/origin/epic/ship/*"
