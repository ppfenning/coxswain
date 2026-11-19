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


def test_a_branch_nobody_pushed_is_made_here_from_the_record() -> None:
    assert lander.branch_source(local=False, on_origin=False) == "cut"


def test_the_building_hosts_merge_commit_that_holds_the_most_of_the_record_is_reused() -> None:
    record = {"base_sha": "b", "phases": {"p1": {"state": "merged", "tip": "t1", "merge_sha": "m1"},
                                          "p2": {"state": "merged", "tip": "t2", "merge_sha": "m2"},
                                          "p3": {"state": "open"}}}
    assert lander.reuse_point(record, {}) is None, "no recorded merge commit here: the record's base is used"
    assert lander.reuse_point(record, {"m1": frozenset({"b", "t1", "m1"})}) == "m1"
    every = frozenset({"b", "t1", "m1", "t2", "m2"})
    assert lander.reuse_point(record, {"m1": frozenset({"b", "t1", "m1"}), "m2": every}) == "m2"


def test_only_a_failed_item_is_told_as_needs_chair_and_a_waiting_one_with_an_old_error_as_stalled() -> None:
    assert lander.alarm_kind({"state": "failed", "attempts": 159, "last_error": "no local branch init/x"}) == "needs_chair"
    # Waiting on a person's review after an earlier failure: it is not failing now.
    assert lander.alarm_kind({"state": "queued", "attempts": 15, "last_error": "no local branch init/x"}) == "stalled"
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


def merge_sha(hosts: Hosts, phase: str) -> str:
    return str(hosts.record()["phases"][phase]["merge_sha"])


def queue(hosts: Hosts, phase: str) -> dict:
    ops.queue_phase(hosts.conn, hosts.record(), phase, now=hosts.now(), reason="phase built")
    return ls.get_item(hosts.conn, f"phase_merge:i:{phase}")


def assembled_on_the_lane(hosts: Hosts) -> tuple[str, str]:
    """p1 and p2 merged on the lane, `init/i` never pushed. p2 was cut after p1's merge, so its branch carries that
    merge commit; p2's own merge commit is only on the lane. (the lane's merge commits of p1 and p2)."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",))])
    hosts.lane_builds("p1")
    assert hosts.lane_merges("p1").status == "merged"
    hosts.lane_builds("p2")
    merged = hosts.lane_merges("p2")
    assert merged.status == "merged" and merged.assembled
    ls.enqueue(hosts.conn, ls.INITIATIVE_LAND, "i", repo=str(hosts.lane), parent="main", now=hosts.now(), reason="last phase merged")
    return merge_sha(hosts, "p1"), merge_sha(hosts, "p2")


def test_a_phase_built_on_a_lane_merges_on_the_lander_host_that_has_no_initiative_branch(hosts: Hosts) -> None:
    """The bug: the lane kept `init/i` local, merged p1 into it, and the lander was asked to merge p2. The lane's own
    merge commit of p1 came over on p2's branch, so it is used as it is: nothing merged again, nothing rewritten."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",))])
    p1 = hosts.lane_builds("p1")
    assert hosts.lane_merges("p1").status == "merged"
    lanes_p1_merge = merge_sha(hosts, "p1")
    p2 = hosts.lane_builds("p2")
    item = queue(hosts, "p2")
    assert remote_heads(hosts.origin, "refs/heads/init/*") == [], "the lane pushed only its phase branches"
    assert lg.rev(hosts.home, "refs/heads/init/i") is None

    outcome = lander.perform(hosts.ports, item)

    assert outcome.status == "done", outcome.detail
    tip = lg.rev(hosts.home, "refs/heads/init/i")
    assert tip and lg.is_ancestor(hosts.home, p1, tip) and lg.is_ancestor(hosts.home, p2, tip)
    assert merge_sha(hosts, "p1") == lanes_p1_merge, "the lane's merge commit is reused, so the record is not rewritten"
    assert run(hosts.home, "rev-parse", f"{merge_sha(hosts, 'p2')}^1") == lanes_p1_merge, "p2 merged onto the lane's commit"
    assert remote_heads(hosts.origin, "refs/heads/init/*") == ["refs/heads/init/i"], "the lander's copy is on origin"
    assert hosts.notes == []
    # The lane's copy is behind origin's, not diverged from it: its next fetch simply fast-forwards it.
    assert fetch_branch(hosts.lane, "init/i") is None
    assert lg.rev(hosts.lane, "refs/heads/init/i") == run(hosts.origin, "rev-parse", "refs/heads/init/i")


def test_an_initiative_assembled_on_a_lane_lands_from_the_lander_host(hosts: Hosts) -> None:
    """Every phase merged on the lane, `init/i` never pushed. p1's merge commit is reused; p2's is only on the lane, so
    p2 is merged again here and its merge commit on the record becomes the lander's."""
    lanes_p1, lanes_p2 = assembled_on_the_lane(hosts)
    assert lg.rev(hosts.home, lanes_p2) is None

    outcome = lander.land_initiative(hosts.ports, "i")

    assert outcome.status == "done", outcome.detail
    assert merge_sha(hosts, "p1") == lanes_p1, "reused, not rewritten"
    assert merge_sha(hosts, "p2") != lanes_p2 and lg.rev(hosts.home, merge_sha(hosts, "p2")), "rewritten to the lander's commit"
    assert hosts.record()["state"] == "landed"
    assert run(hosts.home, "show", "main:p2.txt") == "p2" and run(hosts.home, "show", "main:p1.txt") == "p1"
    assert ("merge", "init/i") in hosts.forge.calls


def test_a_branch_only_origin_has_is_brought_here_at_its_tip_and_nothing_is_pushed_for_it(hosts: Hosts) -> None:
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",))])
    hosts.lane_builds("p1")
    hosts.lane_merges("p1")
    run(hosts.lane, "push", "-q", "origin", "init/i")  # as `push_initiative: early` would
    published = run(hosts.origin, "rev-parse", "refs/heads/init/i")
    hosts.lane_builds("p2")

    outcome = lander.perform(hosts.ports, queue(hosts, "p2"))

    assert outcome.status == "done", outcome.detail
    # `fetch_branch` brings origin's copy here before `branch_source` is read, so it is used as the local copy.
    assert not any("made here" in e or "pushed to origin" in e for e in outcome.events), "never recreated"
    assert run(hosts.home, "rev-parse", "init/i^1") == published, "p2 merged onto origin's tip"
    assert ("push", "init/i") not in hosts.forge.calls, "a copy that holds the record is not pushed again under at_pr"
    assert merge_sha(hosts, "p1") == published


def test_once_the_lander_has_published_the_branch_a_lane_with_a_diverged_copy_refuses_to_move_it(hosts: Hosts) -> None:
    """Two copies cannot fight: origin's (the lander's) is the one, and the lane's diverged copy is never pushed over it."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",)), task("c", "p3", needs=("a",)), task("d", "p4", needs=("c",))])
    hosts.lane_builds("p1")
    hosts.lane_merges("p1")
    hosts.lane_builds("p2")
    hosts.lane_builds("p3")
    hosts.lane_merges("p2")  # its merge commit is on the lane only: the lander merges p2 again, and the copies diverge
    assert lander.perform(hosts.ports, queue(hosts, "p3")).status == "done"
    published = run(hosts.origin, "rev-parse", "refs/heads/init/i")

    lane_tip = lg.rev(hosts.lane, "refs/heads/init/i")
    hosts.lane_builds("p4")
    refused = hosts.lane_merges("p4")

    assert refused.status == "error" and "diverged" in refused.detail
    assert lg.rev(hosts.lane, "refs/heads/init/i") == lane_tip, "the lane's copy is not moved"
    assert run(hosts.origin, "rev-parse", "refs/heads/init/i") == published, "origin's copy is not touched by the lane"
    assert lander.perform(hosts.ports, ls.get_item(hosts.conn, "phase_merge:i:p4")).status == "done", "the lander merges it"


def test_a_recorded_phase_whose_tip_is_nowhere_holds_the_initiative_and_the_chair_is_told_once(hosts: Hosts) -> None:
    hosts.lane_starts([task("a", "p1"), task("b", "p2")])  # two phases of one wave: p2 does not hold p1
    hosts.lane_builds("p1")
    hosts.lane_builds("p2")
    hosts.lane_merges("p1")
    run(hosts.origin, "update-ref", "-d", "refs/heads/epic/i/p1")  # the phase branch is gone from origin too
    item = queue(hosts, "p2")

    passes = [lander.perform(hosts.ports, item) for _ in range(3)]

    assert {o.status for o in passes} == {"waiting"} and "no copy of its tip" in passes[0].detail
    assert [kind for kind, _ in hosts.notes] == ["needs_chair"], "told once, not once a pass"
    assert "no copy of its tip" in hosts.record()["held"]["reason"]
    assert lg.rev(hosts.home, "refs/heads/init/i") is None, "nothing half-built is left behind"
    assert remote_heads(hosts.origin, "refs/heads/init/*") == []
    hosts.clock += 4 * 3600
    assert lander.queue_alarm(hosts.ports) == [] and len(hosts.notes) == 1, "the alarm does not tell a held initiative again"

    run(hosts.lane, "push", "-q", "origin", "epic/i/p1")  # a person puts the phase branch back
    settled = lander.perform(hosts.ports, item)
    assert settled.status == "done", settled.detail
    assert "held" not in hosts.record() or not hosts.record()["held"], "the pass that succeeds lifts the hold"


def test_a_merge_that_conflicts_when_made_again_puts_everything_back_and_holds_once(hosts: Hosts) -> None:
    """The lane resolved a conflict between p1 and p2 by hand; on the lander's host neither merge commit is to be had,
    so the merge made again from the record conflicts. Nothing is left changed, and the chair is told once."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2")])
    for phase in ("p1", "p2"):
        branch = ops.phase_branch("i", phase)
        run(hosts.lane, "branch", branch, "init/i")
        commit(hosts.lane, branch, "shared.txt", f"{phase}\n")
        run(hosts.lane, "push", "-q", "origin", branch)
        for row in hosts.rows:
            if row["phase"] == phase:
                row["state"] = "approved"
    hosts.lane_merges("p1")
    wt = hosts.lane.parent / "resolve"
    run(hosts.lane, "worktree", "add", "-q", str(wt), "init/i")
    subprocess.run(["git", "-C", str(wt), "merge", "epic/i/p2"], capture_output=True)
    (wt / "shared.txt").write_text("p1 and p2\n")
    run(wt, "commit", "-q", "--all", "-m", "resolved by hand")
    resolved = run(wt, "rev-parse", "HEAD")
    record = lr.with_phase(hosts.record(), "p2", hosts.now(), state="merged", tip=run(hosts.lane, "rev-parse", "epic/i/p2"),
                           merge_sha=resolved)
    ls.put_record(hosts.conn, lr.with_state(record, "assembled", hosts.now()))
    before = hosts.record()
    ls.enqueue(hosts.conn, ls.INITIATIVE_LAND, "i", repo=str(hosts.lane), parent="main", now=hosts.now(), reason="last phase merged")

    passes = [lander.land_initiative(hosts.ports, "i") for _ in range(2)]

    assert {o.status for o in passes} == {"waiting"} and "(conflict)" in passes[0].detail
    assert [kind for kind, _ in hosts.notes] == ["needs_chair"]
    assert lg.rev(hosts.home, "refs/heads/init/i") is None and remote_heads(hosts.origin, "refs/heads/init/*") == []
    after = hosts.record()
    assert after["phases"] == before["phases"], "the record's merges are not rewritten"
    assert after["held"]["reason"].startswith(lander.ADOPT_HOLD)


def test_a_rebuilt_branch_origin_refuses_is_put_back_and_the_record_is_unchanged(hosts: Hosts) -> None:
    lanes_p1, lanes_p2 = assembled_on_the_lane(hosts)
    hook = hosts.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'init branches are closed' >&2\nexit 1\n")
    hook.chmod(0o755)

    outcome = lander.land_initiative(hosts.ports, "i")

    assert outcome.status == "waiting" and "origin refused it" in outcome.detail
    assert lg.rev(hosts.home, "refs/heads/init/i") is None, "the local copy made here is gone again"
    assert (merge_sha(hosts, "p1"), merge_sha(hosts, "p2")) == (lanes_p1, lanes_p2), "the record is unchanged"
    assert not hosts.record().get("held") and hosts.notes == [], "a refused push waits; it is not a hold"
    assert ls.get_item(hosts.conn, "initiative_land:i")["state"] == "queued"


def test_with_a_fetch_that_only_updates_origins_ref_origins_copy_is_branched_here_not_recreated(hosts: Hosts) -> None:
    """The `origin` source on its own: a fetch that leaves origin's tip in refs/remotes (the lander's `_fetch_origin`)."""
    hosts.lane_starts([task("a", "p1"), task("b", "p2", needs=("a",))])
    hosts.lane_builds("p1")
    hosts.lane_merges("p1")
    run(hosts.lane, "push", "-q", "origin", "init/i")
    published = run(hosts.origin, "rev-parse", "refs/heads/init/i")

    def remote_only(repo: Path, branch: str) -> str | None:
        lander._fetch_origin(repo, branch)
        return None

    hosts.ports.fetch = remote_only
    adopted = lander.adopt_branch(hosts.ports, hosts.home, "i")

    assert adopted.wait is None and adopted.events == (f"i: init/i brought here from origin at {published[:12]}",)
    assert lg.rev(hosts.home, "refs/heads/init/i") == published
    assert hosts.forge.calls == [], "nothing pushed: origin's copy already holds the record"
