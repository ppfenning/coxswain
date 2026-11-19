"""The initiative branch a lane host kept local: the lander's host has none, and still merges and lands.

Real git (a bare origin, a lane clone, the lander's clone), a real store shared by both, a fake forge whose push is a
real `git push`. No mocks: the lander runs through its own `Ports`, with the real `fetch_branch`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_lander import Forge, commit, run, task

from agent_tools import lander
from agent_tools.lander_ports import fetch_branch
from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_ops as ops
from harness import landing_store as ls
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate

# -- the pure decision ---------------------------------------------------------------------------------------------


def test_a_local_branch_is_used_as_it_is() -> None:
    assert lander.branch_source(local=True, on_origin=True) == "local"
    assert lander.branch_source(local=True, on_origin=False) == "local"


def test_a_branch_only_origin_has_is_fetched_not_recreated() -> None:
    assert lander.branch_source(local=False, on_origin=True) == "origin"


def test_a_branch_nobody_pushed_is_cut_from_the_parent() -> None:
    assert lander.branch_source(local=False, on_origin=False) == "cut"


def test_an_item_that_keeps_failing_is_told_as_needs_chair_and_one_never_run_as_stalled() -> None:
    assert lander.alarm_kind({"state": "failed", "attempts": 159, "last_error": "no local branch init/x"}) == "needs_chair"
    # The sweep queues a failed item again every pass: between passes it reads queued, with its error and attempts.
    assert lander.alarm_kind({"state": "queued", "attempts": 15, "last_error": "no local branch init/x"}) == "needs_chair"
    assert lander.alarm_kind({"state": "queued", "attempts": 0, "last_error": ""}) == "stalled"


# -- real git ------------------------------------------------------------------------------------------------------


class PushingForge(Forge):
    """The fake forge, except that a push really goes to origin (fast-forward only, as the real one)."""

    def push(self, repo, branch):
        self.calls.append(("push", branch))
        done = subprocess.run(["git", "-C", str(repo), "push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
                              capture_output=True, text=True)
        return done.returncode == 0, done.stderr.strip()


class Hosts:
    """A lane host and the lander's host, two clones of one bare origin, sharing one store."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        run(tmp_path, "init", "--bare", "-b", "main", str(self.origin))
        seed = tmp_path / "seed"
        run(tmp_path, "clone", "-q", str(self.origin), str(seed))
        run(seed, "checkout", "-q", "-b", "main")
        (seed / "base.txt").write_text("base\n")
        run(seed, "add", "--all")
        run(seed, "commit", "-q", "-m", "base")
        run(seed, "push", "-q", "origin", "main")
        self.lane, self.home = tmp_path / "omarchy" / "repo", tmp_path / "jarvis" / "repo"
        for clone in (self.lane, self.home):
            clone.parent.mkdir()
            run(tmp_path, "clone", "-q", str(self.origin), str(clone))
        self.conn = connect(f"sqlite:///{tmp_path / 'store.db'}")
        migrate(self.conn, "2026-11-12T00:00:00Z", default_modules())
        self.rows: list[dict] = []
        self.notes: list[tuple] = []
        self.clock = 0
        self.forge = PushingForge()
        self.ports = lander.Ports(
            conn=self.conn, worker="lander@jarvis", now=self.now, tasks=lambda i: [dict(r) for r in self.rows],
            meta=lambda i: {"initiative": {"id": i, "title": f"Title of {i}"}, "cartridge": {}, "gate": "full", "epic_issue": None},
            forge=self.forge, checks=lambda repo, tree, gate: (True, "ok"), mark_merged=lambda i, t, tip: None,
            mark_done=lambda i, t, pr: None, notify=lambda kind, fields: self.notes.append((kind, dict(fields))),
            repo=lambda record: self.home, fetch=fetch_branch, log=lambda line: None,
        )

    def now(self) -> str:
        self.clock += 1
        return f"2026-11-12T{10 + self.clock // 3600:02d}:{self.clock % 3600 // 60:02d}:{self.clock % 60:02d}Z"

    def lane_starts(self, tasks: list[dict]) -> None:
        """The epic on the lane: the record, and `init/i` cut on the lane only (`push_initiative: at_pr`)."""
        self.rows = tasks
        record, why = ops.start_record(self.conn, self.lane, initiative="i", branch="init/i", parent="main",
                                       parent_ref="origin/main", now=self.now())
        assert why == "" and record is not None

    def lane_builds(self, phase: str) -> str:
        """A phase built on the lane, cut from the lane's `init/i`, and its branch pushed: the one thing that is."""
        branch = ops.phase_branch("i", phase)
        run(self.lane, "branch", branch, "init/i")
        tip = commit(self.lane, branch, f"{phase}.txt", f"{phase}\n")
        run(self.lane, "push", "-q", "origin", branch)
        for row in self.rows:
            if row["phase"] == phase:
                row["state"] = "approved"
        return tip

    def lane_merges(self, phase: str) -> ops.PhaseMerge:
        """The build run's own phase merge, on the lane, into the lane's local `init/i`; nothing is pushed."""
        return ops.merge_phase(self.conn, self.lane, initiative="i", phase=phase, tasks=self.rows, worker="run:omarchy",
                               now=self.now(), fetch=lambda b: fetch_branch(self.lane, b))

    def record(self) -> lr.LandingRecord:
        found = ls.get_record(self.conn, "i")
        assert found is not None
        return found


@pytest.fixture
def hosts(tmp_path: Path) -> Hosts:
    return Hosts(tmp_path)


def remote_heads(origin: Path, pattern: str) -> list[str]:
    return [line.split("\t")[1] for line in run(origin, "ls-remote", ".", pattern).splitlines() if line]


def test_a_phase_built_on_a_lane_merges_on_the_lander_host_that_has_no_initiative_branch(hosts: Hosts) -> None:
    """The bug: the lane kept `init/i` local, merged p1 into it, and the lander was asked to merge p2."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",))])
    p1 = hosts.lane_builds("p1")
    assert hosts.lane_merges("p1").status == "merged"
    p2 = hosts.lane_builds("p2")
    ops.queue_phase(hosts.conn, hosts.record(), "p2", now=hosts.now(), reason="phase built")
    assert remote_heads(hosts.origin, "refs/heads/init/*") == [], "the lane pushed only its phase branches"
    assert lg.rev(hosts.home, "refs/heads/init/i") is None

    outcome = lander.perform(hosts.ports, ls.get_item(hosts.conn, "phase_merge:i:p2"))

    assert outcome.status == "done", outcome.detail
    tip = lg.rev(hosts.home, "refs/heads/init/i")
    assert tip and lg.is_ancestor(hosts.home, p1, tip) and lg.is_ancestor(hosts.home, p2, tip)
    record = hosts.record()
    assert lg.is_ancestor(hosts.home, record["phases"]["p1"]["merge_sha"], tip), "p1's merge commit is rewritten to the lander's"
    assert remote_heads(hosts.origin, "refs/heads/init/*") == ["refs/heads/init/i"], "the lander's copy is on origin"
    assert hosts.notes == []


def test_an_initiative_assembled_on_a_lane_lands_from_the_lander_host(hosts: Hosts) -> None:
    """Every phase merged on the lane, `init/i` never pushed: the land rebuilds it from the record and lands it."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",))])
    hosts.lane_builds("p1")
    assert hosts.lane_merges("p1").status == "merged"
    hosts.lane_builds("p2")
    merged = hosts.lane_merges("p2")
    assert merged.status == "merged" and merged.assembled
    ls.enqueue(hosts.conn, ls.INITIATIVE_LAND, "i", repo=str(hosts.lane), parent="main", now=hosts.now(), reason="last phase merged")

    outcome = lander.land_initiative(hosts.ports, "i")

    assert outcome.status == "done", outcome.detail
    assert hosts.record()["state"] == "landed"
    landed = run(hosts.home, "show", "main:p2.txt")
    assert landed == "p2" and run(hosts.home, "show", "main:p1.txt") == "p1"
    assert ("merge", "init/i") in hosts.forge.calls


def test_once_the_lander_has_published_the_branch_a_lane_with_its_own_copy_refuses_to_move_it(hosts: Hosts) -> None:
    """Two copies cannot fight: origin's (the lander's) is the one, and the lane's diverged copy is never pushed over it."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",)), task("c", "p3", needs=("b",))])
    hosts.lane_builds("p1")
    hosts.lane_merges("p1")
    hosts.lane_builds("p2")
    ops.queue_phase(hosts.conn, hosts.record(), "p2", now=hosts.now(), reason="phase built")
    assert lander.perform(hosts.ports, ls.get_item(hosts.conn, "phase_merge:i:p2")).status == "done"
    published = run(hosts.origin, "rev-parse", "refs/heads/init/i")

    lane_tip = lg.rev(hosts.lane, "refs/heads/init/i")
    hosts.lane_builds("p3")
    refused = hosts.lane_merges("p3")

    assert refused.status == "error" and "diverged" in refused.detail
    assert lg.rev(hosts.lane, "refs/heads/init/i") == lane_tip, "the lane's copy is not moved"
    assert run(hosts.origin, "rev-parse", "refs/heads/init/i") == published, "origin's copy is not touched by the lane"
    assert lander.perform(hosts.ports, ls.get_item(hosts.conn, "phase_merge:i:p3")).status == "done", "the lander merges it"


def test_a_recorded_phase_whose_tip_is_nowhere_goes_to_the_chair_with_its_reason(hosts: Hosts) -> None:
    hosts.lane_starts([task("a", "p1"), task("b", "p2")])  # two phases of one wave: p2 does not hold p1
    hosts.lane_builds("p1")
    hosts.lane_builds("p2")
    hosts.lane_merges("p1")
    run(hosts.origin, "update-ref", "-d", "refs/heads/epic/i/p1")  # the phase branch is gone from origin too
    ops.queue_phase(hosts.conn, hosts.record(), "p2", now=hosts.now(), reason="phase built")

    outcome = lander.perform(hosts.ports, ls.get_item(hosts.conn, "phase_merge:i:p2"))

    assert outcome.status == "failed" and "no copy of its tip" in outcome.detail
    assert [kind for kind, _ in hosts.notes] == ["needs_chair"] and hosts.notes[0][1]["reason"] == outcome.detail
    assert ls.get_item(hosts.conn, "phase_merge:i:p2")["last_error"] == outcome.detail
    assert lg.rev(hosts.home, "refs/heads/init/i") is None, "nothing half-built is left behind"
    assert remote_heads(hosts.origin, "refs/heads/init/*") == []
