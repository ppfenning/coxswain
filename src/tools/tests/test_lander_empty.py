"""The lander refuses a phase or an initiative that carries nothing against main, and tells the chair once.

The decisions are pure and tested on example inputs; the edges and the lander are tested against a real repository and
a real store. 2026-11-20: a pull request was opened with "+0 -0, 0 files" from a phase branch that held only merges.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_tools import lander
from agent_tools import lander_empty as le
from harness import landing_ops as ops
from harness import landing_store as ls
from tests.test_lander import World, commit, run, task

TREE, OTHER = "a" * 40, "b" * 40


# -- the pure decisions -------------------------------------------------------------------------------------------

def test_a_merge_that_leaves_the_target_tree_as_it_is_carries_nothing() -> None:
    assert le.carries_nothing(le.TreeFacts(TREE, TREE, False), ["t1"])
    assert not le.carries_nothing(le.TreeFacts(TREE, OTHER, False), ["t1"]), "a change"


def test_nothing_is_refused_without_an_approved_task_or_a_known_tree() -> None:
    assert not le.carries_nothing(le.TreeFacts(TREE, TREE, False), []), "every task dropped or done: empty is right"
    assert not le.carries_nothing(le.TreeFacts(None, TREE, False), ["t1"])
    assert not le.carries_nothing(le.TreeFacts(TREE, None, False), ["t1"]), "a conflict carries something"


def test_a_held_branch_is_empty_only_where_holding_it_means_it_has_nothing_of_its_own() -> None:
    on_line, merged_in = le.TreeFacts(TREE, TREE, True), le.TreeFacts(TREE, TREE, True, merged_in=True)
    assert le.carries_nothing(on_line, ["t1"], "empty") and le.carries_nothing(merged_in, ["t1"], "empty"), "held by main"
    assert not le.carries_nothing(merged_in, ["t1"], "own"), "merged into its initiative branch: pushed, not yet recorded"
    assert le.carries_nothing(on_line, ["t1"], "own"), "cut from its initiative branch with no commit of its own"
    assert not le.carries_nothing(on_line, ["t1"], "merged"), "held by the parent of an open pull request: it merged"


def test_the_refusal_names_the_first_target_the_branch_carries_nothing_against() -> None:
    main, init = le.Target("origin/main", "main", "empty"), le.Target("refs/heads/init/i", "init/i", "own")
    change, nothing = le.TreeFacts(TREE, OTHER, False), le.TreeFacts(TREE, TREE, False)
    assert le.empty_against([(main, nothing), (init, nothing)], ["t1"]) == "main"
    assert le.empty_against([(main, change), (init, nothing)], ["t1"]) == "init/i"
    assert le.empty_against([(main, change), (init, change)], ["t1"]) is None


def test_the_approved_tasks_with_no_commit_on_the_remote_are_listed() -> None:
    tasks = [
        {"id": "a", "phase": "p1", "state": "approved"},
        {"id": "b", "phase": "p1", "state": "approved", "merged_commit": TREE},
        {"id": "c", "phase": "p1", "state": "approved", "merged_commit": OTHER},
        {"id": "d", "phase": "p1", "state": "dropped"},
        {"id": "e", "phase": "p2", "state": "approved"},
    ]
    remote = {TREE: True, OTHER: False}
    assert le.uncommitted(tasks, remote, "p1") == ["a", "c"]
    assert le.uncommitted(tasks, remote) == ["a", "c", "e"]
    assert le.approved_tasks(tasks, "p1") == ["a", "b", "c"]


def test_the_reason_and_the_alarm_say_what_was_compared_and_which_tasks_lack_commits() -> None:
    assert le.empty_reason("route-drift", "main") == "route-drift: no changes against main; the approved tasks have no commits"
    reason = le.empty_reason("i", "init/i", "p2")
    assert reason == "i: phase p2: no changes against init/i; the approved tasks have no commits"
    assert "(approved with no commit on the remote: a, c); " in le.empty_alarm(reason, ["a", "c"])
    assert le.empty_alarm(reason, []).endswith(le.WAY_OUT), "the alarm names the way out"
    assert "none; their commits change nothing" in le.empty_alarm(reason, [])


def test_an_alarm_is_told_once_and_forgotten_when_the_branch_carries_a_change_again() -> None:
    assert le.empty_news(None, "x") == "tell"
    assert le.empty_news("x", "x") == "quiet", "the same alarm is not told twice"
    assert le.empty_news("x", "y") == "tell", "a changed alarm is news"
    assert le.empty_news("x", None) == "forget"
    assert le.empty_news(None, None) == "quiet"
    assert le.empty_key("i") == "empty:i" and le.empty_key("i", "p1") == "empty:i:p1"


def test_a_notice_is_stale_once_its_initiative_landed_or_its_phase_reads_merged() -> None:
    told = ["empty:i", "empty:i:p1", "empty:i:p2", "empty:j:p1", "empty:k", "unreadable:i"]
    states = {"i": {"p1": "merged", "p2": "built"}, "j": None}
    assert le.stale_empty(told, states) == ["empty:i:p1", "empty:j:p1"], "k is not in this lane: left alone"
    assert le.stale_empty(["empty:j"], {"j": None}) == ["empty:j"]


# -- the edges, on a real repository ------------------------------------------------------------------------------

def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    run(repo, "add", "--all")
    run(repo, "commit", "-q", "-m", "base")
    return repo


def test_tree_facts_tell_a_branch_of_merges_from_one_with_a_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run(repo, "branch", "old")
    commit(repo, "main", "main.txt", "moved on\n")
    run(repo, "branch", "merges-only", "old")
    wt = tmp_path / "wt"
    run(repo, "worktree", "add", "-q", str(wt), "merges-only")
    run(wt, "-c", "user.email=t@invalid", "-c", "user.name=t", "merge", "-q", "--no-ff", "-m", "merge main", "main")
    run(repo, "worktree", "remove", "--force", str(wt))
    run(repo, "branch", "change", "main")
    commit(repo, "change", "feature.txt", "feature\n")

    merges = le.tree_facts(repo, "main", "refs/heads/merges-only")
    assert le.carries_nothing(merges, ["t1"]) and not merges.held
    assert not le.carries_nothing(le.tree_facts(repo, "main", "refs/heads/change"), ["t1"])
    old = le.tree_facts(repo, "main", "refs/heads/old")
    assert old.held and not old.merged_in, "a branch cut from main's own line with nothing added"
    run(repo, "merge", "-q", "--no-ff", "-m", "merge change", "change")
    pushed = le.tree_facts(repo, "main", "refs/heads/change")
    assert pushed.held and pushed.merged_in, "a branch merged in with commits of its own"
    assert le.tree_facts(repo, "main", "refs/heads/no-such-branch").merged_tree is None


def test_on_remote_reads_origin_and_with_no_origin_the_local_branches(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tip = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-q", "--detach")
    (repo / "loose.txt").write_text("loose\n")
    run(repo, "add", "--all")
    run(repo, "-c", "user.email=t@invalid", "-c", "user.name=t", "commit", "-q", "-m", "loose")
    loose = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-q", "main")
    assert le.on_remote(repo, [tip, loose, "f" * 40]) == {tip: True, loose: False, "f" * 40: False}

    origin = tmp_path / "origin.git"
    run(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    run(repo, "remote", "add", "origin", str(origin))
    assert le.on_remote(repo, [tip]) == {tip: False}, "with an origin, only what origin holds counts"
    run(repo, "push", "-q", "origin", "main")
    run(repo, "fetch", "-q", "origin")
    assert le.on_remote(repo, [tip]) == {tip: True}


# -- the lander, against a real repository and a real store -------------------------------------------------------

@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _merges_only(world: World, initiative: str, phase: str) -> None:
    """A phase branch as the fetch bug left one: cut from the initiative branch, then main merged into it, and its
    task approved with no commit of its own."""
    commit(world.repo, "main", "main.txt", "main moved on\n")
    branch = ops.phase_branch(initiative, phase)
    run(world.repo, "branch", branch, f"init/{initiative}")
    wt = world.repo.parent / "wt-merges"
    run(world.repo, "worktree", "add", "-q", str(wt), branch)
    run(wt, "-c", "user.email=t@invalid", "-c", "user.name=t", "merge", "-q", "--no-ff", "-m", f"merge main into {branch}", "main")
    run(world.repo, "worktree", "remove", "--force", str(wt))
    for row in world.rows[initiative]:
        if row["phase"] == phase:
            row["state"] = "approved"


def _chair_notes(world: World) -> list[str]:
    return [str(fields["reason"]) for kind, fields in world.notes if kind == "needs_chair"]


def test_a_phase_of_only_merges_is_refused_told_once_and_merges_once_it_carries_a_change(world: World) -> None:
    world.start("i", [task("a", "p1")])
    _merges_only(world, "i", "p1")
    ops.queue_phase(world.conn, world.record("i"), "p1", now=world.now(), reason="phase built")
    tip = run(world.repo, "rev-parse", "init/i")

    first, second = lander.run_once(world.ports), lander.run_once(world.ports)

    reason = "i: phase p1: no changes against main; the approved tasks have no commits"
    assert [(o.status, o.detail) for o in first] == [("waiting", reason)]
    assert [(o.status, o.detail) for o in second] == [("waiting", reason)], "a second pass refuses the same way"
    assert _chair_notes(world) == [f"{reason} (approved with no commit on the remote: a); {le.WAY_OUT}"], "told once, not once a pass"
    assert run(world.repo, "rev-parse", "init/i") == tip, "nothing merged"
    assert world.record("i")["phases"]["p1"]["state"] != "merged"
    assert "pr_create" not in world.forge.kinds() and "push" not in world.forge.kinds()
    assert list(ls.told(world.conn, le.EMPTY_KEY)) == ["empty:i:p1"]

    commit(world.repo, ops.phase_branch("i", "p1"), "feature.txt", "the real change\n")
    lander.run_once(world.ports)

    assert world.record("i")["state"] == "landed", "a phase with a real change still lands"
    assert world.forge.kinds().count("pr_create") == 1
    assert ls.told(world.conn, le.EMPTY_KEY) == {}, "the refusal is forgotten once the branch carries a change"
    assert len(_chair_notes(world)) == 1


def test_an_initiative_assembled_with_nothing_on_it_opens_no_pull_request_and_tells_the_chair_once(world: World) -> None:
    world.start("i", [task("a", "p1")])
    run(world.repo, "branch", ops.phase_branch("i", "p1"), "init/i")  # a phase branch with no commit at all
    world.rows["i"][0]["state"] = "approved"
    # A build run merges the phase itself, the moment it finishes: the land is the lander's last chance to refuse.
    merged = ops.merge_phase(world.conn, world.repo, initiative="i", phase="p1", tasks=world.rows["i"], worker="run:lane-host",
                             now=world.now())
    assert merged.status == "merged" and merged.assembled
    ls.enqueue(world.conn, ls.INITIATIVE_LAND, "i", repo=str(world.repo), parent="main", now=world.now(), reason="last phase merged")

    first, second = lander.run_once(world.ports), lander.run_once(world.ports)

    reason = "i: no changes against main; the approved tasks have no commits"
    assert [(o.status, o.detail) for o in [*first, *second]] == [("waiting", reason), ("waiting", reason)]
    assert _chair_notes(world) == [f"{reason} (approved with no commit on the remote: a); {le.WAY_OUT}"]
    assert "pr_create" not in world.forge.kinds() and "merge" not in world.forge.kinds()
    assert not (world.record("i").get("pr") or {}).get("url")


def test_a_later_phase_cut_from_the_initiative_branch_with_no_commit_is_refused(world: World) -> None:
    world.start("i", [task("a", "p1"), task("b", "p2", needs=("a",))])
    world.build("i", "p1")
    lander.run_once(world.ports)
    assert world.record("i")["phases"]["p1"]["state"] == "merged"
    run(world.repo, "branch", ops.phase_branch("i", "p2"), "init/i")  # held by init/i, and it carries p1 against main
    world.rows["i"][1]["state"] = "approved"
    ops.queue_phase(world.conn, world.record("i"), "p2", now=world.now(), reason="phase built")

    first, second = lander.run_once(world.ports), lander.run_once(world.ports)

    reason = "i: phase p2: no changes against init/i; the approved tasks have no commits"
    assert [(o.status, o.detail) for o in [*first, *second]] == [("waiting", reason), ("waiting", reason)]
    assert world.record("i")["phases"].get("p2", {}).get("state") != "merged"
    assert len(_chair_notes(world)) == 1 and "pr_create" not in world.forge.kinds()


def test_a_merged_phase_that_gains_an_approved_task_with_no_commit_fails_closed_and_names_the_way_out(world: World) -> None:
    world.start("i", [task("a", "p1")])
    world.rows["i"][0]["merged_commit"] = world.build("i", "p1")
    world.gate = "ticket"  # the pull request opens and waits for a person, so the phase can gain work after its merge
    lander.run_once(world.ports)
    assert world.record("i")["phases"]["p1"]["state"] == "merged"
    world.rows["i"].append({**task("b", "p1"), "state": "approved"})  # approved, with its work already on the branch
    phase = ops.phase_branch("i", "p1")
    wt = world.repo.parent / "wt-remerge"
    run(world.repo, "worktree", "add", "-q", str(wt), phase)
    run(wt, "-c", "user.email=t@invalid", "-c", "user.name=t", "merge", "-q", "--no-ff", "-m", "merge init/i", "init/i")
    run(world.repo, "worktree", "remove", "--force", str(wt))
    ops.queue_phase(world.conn, world.record("i"), "p1", now=world.now(), reason="phase built")

    lander.run_once(world.ports)
    lander.run_once(world.ports)

    [alarm] = _chair_notes(world)
    assert alarm.startswith("i: phase p1: no changes against init/i; the approved tasks have no commits")
    assert "approved with no commit on the remote: b)" in alarm and le.WAY_OUT in alarm

    world.rows["i"][-1]["state"] = "dropped"  # the way out: its work was already on the branch
    lander.run_once(world.ports)

    assert ls.told(world.conn, le.EMPTY_KEY) == {}, "the phase reads merged again, and the notice is gone"
    assert len(_chair_notes(world)) == 1


def test_commits_that_revert_each_other_are_refused_and_the_alarm_says_they_change_nothing(world: World) -> None:
    world.start("i", [task("a", "p1")])
    branch = ops.phase_branch("i", "p1")
    run(world.repo, "branch", branch, "init/i")
    commit(world.repo, branch, "feature.txt", "a change\n")
    wt = world.repo.parent / "wt-revert"
    run(world.repo, "worktree", "add", "-q", str(wt), branch)
    run(wt, "-c", "user.email=t@invalid", "-c", "user.name=t", "revert", "--no-edit", "HEAD")
    run(world.repo, "worktree", "remove", "--force", str(wt))
    world.rows["i"][0].update(state="approved", merged_commit=run(world.repo, "rev-parse", branch))
    ops.queue_phase(world.conn, world.record("i"), "p1", now=world.now(), reason="phase built")

    lander.run_once(world.ports)

    [alarm] = _chair_notes(world)
    assert alarm.startswith("i: phase p1: no changes against main; the approved tasks have no commits")
    assert "(approved with no commit on the remote: none; their commits change nothing)" in alarm


def test_a_refused_phase_a_build_run_merges_later_leaves_no_notice_behind(world: World) -> None:
    world.start("i", [task("a", "p1"), task("b", "p2", needs=("a",))])
    _merges_only(world, "i", "p1")
    ops.queue_phase(world.conn, world.record("i"), "p1", now=world.now(), reason="phase built")
    lander.run_once(world.ports)
    assert list(ls.told(world.conn, le.EMPTY_KEY)) == ["empty:i:p1"]
    merged = ops.merge_phase(world.conn, world.repo, initiative="i", phase="p1", tasks=world.rows["i"],
                             worker="run:lane-host", now=world.now())
    assert merged.status == "merged"

    lander.run_once(world.ports)

    assert ls.told(world.conn, le.EMPTY_KEY) == {}, "the next empty refusal of p1 is told again"
