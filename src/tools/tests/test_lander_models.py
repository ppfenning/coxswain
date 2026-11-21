"""D1: the lander merges new-model phases, sends their conflicts to the merge agent, and handles both models in a pass.

The pure core first, with no mocks. Then the lander over a real repo, a real bare origin and a real store; the merge
agent is the only stand-in (`Ports.resolve_conflict`), as the forge is in `test_lander.py`. On the new model a phase is
built by a lane on another clone and pushed to `epic/<initiative>/<phase>` on origin; the lander merges origin's copy.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_tools import lander, lander_adopt, lander_ports
from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_ops as ops
from harness import landing_store as ls
from tests.test_lander import World, run, task

T0 = "2026-11-20T00:00:00Z"
P1_TEXT, P2_TEXT = "one\nTWO from p1\nthree\n", "one\nTWO from p2\nthree\n"
BOTH = "one\nTWO from p1 and p2\nthree\n"


# -- the pure core --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("model", "route", "source"), [
    ("phase", "merge_agent", "origin"),
    ("task", "rectify_task", "local"),
    ("", "rectify_task", "local"),
    ("Phase", "rectify_task", "local"),
])
def test_a_conflict_goes_to_the_merge_agent_only_on_the_new_model_which_always_merges_origins_phase(model, route, source):
    assert lander.conflict_route(model) == route
    assert lander.merge_source(model) == source


def test_the_source_ref_is_origins_phase_branch_or_the_ops_default():
    assert lander.source_ref("origin", "i", "p2") == "refs/remotes/origin/epic/i/p2"
    assert lander.source_ref("local", "i", "p2") is None


def test_the_new_model_runs_the_phase_level_gate_and_the_old_one_the_level_gate_as_before():
    assert lander.level_gate("phase") == lander.PHASE_GATE == "phase"
    assert lander.level_gate("task") == lander.level_gate("") == "level"


def test_the_phase_gate_runs_the_trees_phase_section_after_the_cartridges_level_checks(tmp_path):
    """The new model's level gate is the lane's smoke: the `# level: phase` section of the merged tree's own
    `.agent-checks`. The old model's `level` gate still reads the cartridge's list alone (L2's test of it stands)."""
    (tmp_path / ".agent-checks").write_text("true\n# level: phase\necho phase-smoke && exit 7\n# level: initiative\nexit 8\n")
    landing = {"level_checks": [{"name": "cartridge", "cmd": "true"}]}
    passed, output = lander_ports.run_gate(tmp_path, tmp_path, lander.PHASE_GATE, landing, None)
    assert not passed and "echo phase-smoke && exit 7" in output and "exit 8" not in output
    assert lander_ports.run_gate(tmp_path, tmp_path, "level", landing, None) == (True, "")
    (tmp_path / ".agent-checks").unlink()
    assert lander_ports.run_gate(tmp_path, tmp_path, lander.PHASE_GATE, landing, None) == (True, "")


def test_a_record_names_its_lane_model_and_one_that_names_none_is_the_old_model():
    assert lr.record_lane_model(None) == "task"
    assert lr.record_lane_model({"initiative": "i"}) == "task"
    assert lr.record_lane_model({"initiative": "i", "lane_model": "phase"}) == "phase"
    assert lr.record_lane_model({"initiative": "i", "lane_model": "bogus"}) == "task"


def test_an_old_model_record_is_written_exactly_as_before_and_a_new_one_adds_only_its_model():
    fields = {"repo": "/r", "branch": "init/i", "parent": "main", "base_sha": "b" * 40, "now": T0}
    before = {"initiative": "i", "repo": "/r", "branch": "init/i", "parent": "main", "base_sha": "b" * 40,
              "land_at": "initiative", "state": "building", "phases": {}, "waves": {}, "pr": None, "epic": None,
              "stack_base": None, "adopted": False, "created_at": T0, "updated_at": T0}
    for old in (lr.new_record("i", **fields), lr.new_record("i", **fields, lane_model="task"),
                lr.new_record("i", **fields, lane_model=None)):
        assert json.dumps(old) == json.dumps(before)
    assert lr.new_record("i", **fields, lane_model="phase") == {**before, "lane_model": "phase"}


def test_a_merge_hold_carries_its_phase_and_tip_and_only_a_new_tip_lifts_it():
    hold = lander.merge_hold("p2", "a" * 40, "conflict in shared.txt: no", T0)
    assert hold["reason"].startswith(lander.MERGE_HOLD + "p2 at aaaaaaaaaaaa")
    held: lr.LandingRecord = {"initiative": "i", "held": hold}
    assert lander.merge_held(held)
    assert not lander.merge_held({"initiative": "i", "held": {"reason": "origin lacks it", "since": T0}})
    assert not lander.merge_held({"initiative": "i"})
    assert not lander.hold_lifted(held, "a" * 40)
    assert not lander.hold_lifted(held, None)
    assert lander.hold_lifted(held, "c" * 40)
    assert not lander.hold_lifted({"initiative": "i", "held": {"reason": "other", "tip": "a" * 40}}, "c" * 40)


def test_a_resolvers_answer_must_cover_exactly_the_conflicted_files_and_leave_no_marker():
    assert lg.answer_problem({"a.txt": "x\n"}, ["a.txt"]) is None
    assert "left out b.txt" in str(lg.answer_problem({"a.txt": "x\n"}, ["a.txt", "b.txt"]))
    assert "do not conflict: c.txt" in str(lg.answer_problem({"a.txt": "x\n", "c.txt": ""}, ["a.txt"]))
    assert "markers in a.txt" in str(lg.answer_problem({"a.txt": "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> z\n"}, ["a.txt"]))
    assert not lg.marked("a <<<<<<< in the middle\n")


# -- the git: a resolver given the conflict in the scratch -------------------------------------------------------------

def _two_sides(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "g"
    repo.mkdir()
    run(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("one\ntwo\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "base")
    run(repo, "checkout", "-qb", "side")
    (repo / "f.txt").write_text("one\nside\n")
    run(repo, "commit", "-qam", "side")
    run(repo, "checkout", "-q", "main")
    (repo / "f.txt").write_text("one\nmain\n")
    run(repo, "commit", "-qam", "main")
    return repo, run(repo, "rev-parse", "main")


def test_without_a_resolver_a_conflict_aborts_as_it_always_did(tmp_path):
    repo, old = _two_sides(tmp_path)
    merged = lg.merge_into(repo, branch="main", source="side", message="m")
    assert (merged.status, merged.paths, merged.detail, merged.resolved_by) == ("conflict", ("f.txt",), "conflict in f.txt", "")
    assert run(repo, "rev-parse", "main") == old


def test_the_resolver_sees_the_markers_in_the_scratch_and_its_answer_is_the_merge(tmp_path):
    repo, old = _two_sides(tmp_path)
    seen: list[str] = []

    def resolve(scratch: Path, paths: tuple[str, ...]) -> lg.Resolved:
        seen.append((scratch / paths[0]).read_text())
        return lg.Resolved({"f.txt": "one\nmain and side\n"}, "the agent")

    merged = lg.merge_into(repo, branch="main", source="side", message="m", resolve=resolve)
    assert "<<<<<<<" in seen[0] and "side" in seen[0] and "main" in seen[0]
    assert (merged.status, merged.resolved_by) == ("merged", "the agent")
    assert run(repo, "show", "main:f.txt") == "one\nmain and side"
    assert run(repo, "rev-parse", "main^1") == old and lg.is_ancestor(repo, "side", "main")


@pytest.mark.parametrize(("answer", "why"), [
    ("cannot tell which", "conflict in f.txt: cannot tell which"),
    (lg.Resolved({"f.txt": "<<<<<<< x\n"}, "a"), "conflict in f.txt: the resolution left conflict markers in f.txt"),
    (lg.Resolved({}, "a"), "conflict in f.txt: the resolution left out f.txt"),
])
def test_a_resolver_that_does_not_settle_it_leaves_the_conflict_and_the_branch(tmp_path, answer, why):
    repo, old = _two_sides(tmp_path)
    merged = lg.merge_into(repo, branch="main", source="side", message="m", resolve=lambda s, p: answer)
    assert (merged.status, merged.detail) == ("conflict", why)
    assert run(repo, "rev-parse", "main") == old


def test_a_resolver_that_breaks_leaves_the_conflict(tmp_path):
    repo, old = _two_sides(tmp_path)

    def broken(scratch, paths):
        raise RuntimeError("the agent fell over")

    merged = lg.merge_into(repo, branch="main", source="side", message="m", resolve=broken)
    assert merged.status == "conflict" and "the resolver failed: RuntimeError: the agent fell over" in merged.detail
    assert run(repo, "rev-parse", "main") == old


def test_a_resolved_merge_still_meets_the_checks(tmp_path):
    repo, old = _two_sides(tmp_path)
    merged = lg.merge_into(repo, branch="main", source="side", message="m", checks=lambda tree: (False, "red"),
                           resolve=lambda s, p: lg.Resolved({"f.txt": "x\n"}, "a"))
    assert (merged.status, merged.detail) == ("checks_failed", "red")
    assert run(repo, "rev-parse", "main") == old


# -- the lander over both models --------------------------------------------------------------------------------------

class Agent:
    """The merge agent's stand-in: what it was asked, and its answer: every conflicted file's merged text, or, with
    `refuse` set, that reason instead."""

    def __init__(self, merged: str = BOTH) -> None:
        self.asked: list[tuple[lander.ConflictAsk, dict[str, str]]] = []
        self.merged = merged
        self.refuse: str | None = None

    def __call__(self, ask: lander.ConflictAsk) -> lg.Resolved | str:
        self.asked.append((ask, {p: (ask.scratch / p).read_text() for p in ask.paths}))
        if self.refuse is not None:
            return self.refuse
        return lg.Resolved(dict.fromkeys(ask.paths, self.merged), "the merge agent: kept both edits")


class Models(World):
    """`World` with a bare origin and a worker clone. A new-model initiative's phases are built on the worker, as the
    phase lane builds them, and pushed to origin; the lander's clone never has them until it fetches."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        run(self.repo, "remote", "add", "origin", str(self.origin))
        run(self.repo, "push", "-q", "origin", "main")
        self.worker = tmp_path / "worker"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.worker)], check=True)
        self.agent = Agent()
        self.ports = replace(self.ports, resolve_conflict=self.agent)

    def start_new(self, initiative: str, tasks: list[dict]) -> dict:
        self.rows[initiative] = tasks
        record, why = ops.start_record(self.conn, self.repo, initiative=initiative, branch=f"init/{initiative}",
                                       parent="main", parent_ref="main", now=self.now(), lane_model="phase")
        assert why == "" and record is not None
        run(self.repo, "push", "-q", "origin", f"init/{initiative}")
        return record

    def lane(self, initiative: str, phase: str, name: str, text: str) -> str:
        """What the phase lane leaves: the phase branch cut from origin's initiative branch, one commit, pushed."""
        branch = ops.phase_branch(initiative, phase)
        run(self.worker, "fetch", "-q", "origin")
        exists = run(self.worker, "ls-remote", "origin", f"refs/heads/{branch}")
        run(self.worker, "checkout", "-q", "-B", branch, f"origin/{branch}" if exists else f"origin/init/{initiative}")
        (self.worker / name).write_text(text)
        run(self.worker, "add", "-A")
        run(self.worker, "commit", "-qm", f"{branch}: {name}")
        run(self.worker, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        for row in self.rows[initiative]:
            if row["phase"] == phase and row["state"] in ("ready", "todo"):
                row["state"] = "approved"
        return run(self.worker, "rev-parse", "HEAD")

    def on_origin(self, ref: str) -> str:
        return run(self.origin, "rev-parse", ref)


@pytest.fixture
def models(tmp_path: Path) -> Models:
    return Models(tmp_path)


def _collide(models: Models, initiative: str = "n") -> tuple[str, str]:
    models.start_new(initiative, [task("a", "p1"), task("b", "p2")])
    return (models.lane(initiative, "p1", "shared.txt", P1_TEXT), models.lane(initiative, "p2", "shared.txt", P2_TEXT))


def test_two_new_model_phases_that_collide_roll_up_through_the_merge_agent_and_origin_holds_both(models: Models) -> None:
    p1, p2 = _collide(models)
    outcomes = lander.run_once(models.ports, phase_cap=5)
    assert [o.status for o in outcomes[:2]] == ["done", "done"], outcomes
    [(ask, sides)] = models.agent.asked
    assert (ask.initiative, ask.phase, ask.branch, ask.source, ask.paths) == (
        "n", "p2", "init/n", "refs/remotes/origin/epic/n/p2", ("shared.txt",))
    assert "<<<<<<<" in sides["shared.txt"] and "TWO from p1" in sides["shared.txt"] and "TWO from p2" in sides["shared.txt"]
    tip = models.on_origin("init/n")
    assert lg.is_ancestor(models.origin, p1, tip) and lg.is_ancestor(models.origin, p2, tip), "origin holds both phases"
    assert run(models.origin, "show", f"{tip}:shared.txt") == BOTH.rstrip("\n")
    phases = models.record("n")["phases"]
    assert phases["p2"]["resolved_by"] == "the merge agent: kept both edits" and "resolved_by" not in phases["p1"]
    assert (phases["p1"]["tip"], phases["p2"]["tip"]) == (p1, p2)
    assert models.on_origin("epic/n/p2") == p2, "the lander never writes a new-model phase branch"
    assert models.rectified == [] and models.notes == []
    assert any("resolved by the merge agent" in e for e in outcomes[1].events)


def test_the_new_model_merges_origins_phase_even_when_this_clone_holds_a_stale_copy(models: Models) -> None:
    models.start_new("n", [task("a", "p1")])
    run(models.repo, "branch", "epic/n/p1", "init/n")  # an old local copy, at the initiative tip, that never moves
    tip = models.lane("n", "p1", "x.txt", "x\n")
    lander.run_once(models.ports)
    assert models.record("n")["phases"]["p1"]["tip"] == tip


def test_a_new_model_phase_origin_lacks_waits(models: Models) -> None:
    models.start_new("n", [task("a", "p1", state="approved")])
    out = lander.merge_phase(models.ports, "n", "p1")
    assert out.status == "waiting" and "origin has no epic/n/p1" in out.detail


def test_a_merge_the_agent_cannot_settle_holds_the_initiative_tells_the_chair_once_and_asks_once(models: Models) -> None:
    _, p2 = _collide(models)
    models.agent.refuse = "the two edits contradict each other"
    before = models.on_origin("init/n")
    out = lander.run_once(models.ports, phase_cap=5)
    p1_merged = models.on_origin("init/n")
    assert p1_merged != before and out[1].status == "failed"
    held = models.record("n")["held"]
    assert held["reason"].startswith(f"{lander.MERGE_HOLD}p2 at {p2[:12]}: conflict in shared.txt")
    assert "the two edits contradict each other" in held["reason"] and (held["phase"], held["tip"]) == ("p2", p2)
    assert [(topic, n["phase"]) for topic, n in models.notes] == [("needs_chair", "p2")]
    assert models.rectified == [], "the new model files no rectify task"
    for _ in range(2):
        models.clock += 4000
        lander.run_once(models.ports, phase_cap=5)
    assert len(models.agent.asked) == 1 and len(models.notes) == 1, "a held merge is not asked again, nor told again"
    assert models.record("n")["held"] == held, "reconciliation leaves a merge hold"
    assert models.on_origin("init/n") == p1_merged


def test_a_fix_pushed_to_the_phase_lifts_the_hold_and_the_merge_is_tried_again(models: Models) -> None:
    _collide(models)
    models.agent.refuse = "no"
    lander.run_once(models.ports, phase_cap=5)
    assert lander.merge_held(models.record("n"))
    models.agent.refuse = None
    fixed = models.lane("n", "p2", "p2-note.txt", "fixed\n")
    models.clock += 4000
    lander.run_once(models.ports, phase_cap=5)
    assert models.record("n").get("held") is None
    assert len(models.agent.asked) == 2 and models.agent.asked[1][0].source == "refs/remotes/origin/epic/n/p2"
    assert lg.is_ancestor(models.origin, fixed, models.on_origin("init/n"))
    assert models.record("n")["phases"]["p2"]["state"] == "merged"


def test_a_resolution_that_fails_the_level_checks_holds_and_the_branch_stays(models: Models) -> None:
    _collide(models)
    lander.run_once(models.ports, phase_cap=1)  # p1 merges clean, before the checks go red
    before = models.on_origin("init/n")
    models.check_result["phase"] = (False, "tests/test_x.py::test_y FAILED")
    models.clock += 4000
    lander.run_once(models.ports, phase_cap=5)
    assert len(models.agent.asked) == 1
    assert "tests/test_x.py::test_y FAILED" in models.record("n")["held"]["reason"]
    assert models.on_origin("init/n") == before and models.rectified == []
    assert [n["phase"] for topic, n in models.notes if topic == "needs_chair"] == ["p2"]


def _old_collision(world: World) -> list[lander.Outcome]:
    world.start("o", [task("a", "p1"), task("b", "p2")])
    world.build("o", "p1", "shared.txt", P1_TEXT)
    world.build("o", "p2", "shared.txt", P2_TEXT)
    return lander.run_once(world.ports)


def test_the_old_model_is_exactly_as_before_whatever_merge_agent_is_wired(tmp_path: Path, monkeypatch) -> None:
    """The same old-model collision, once with the lander's default ports and once with a merge agent that would
    resolve anything: the same outcomes, the same record, the same commits, and the agent never asked."""
    for var in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):  # one clock for both repositories, so their shas compare
        monkeypatch.setenv(var, "2026-11-20T00:00:00Z")
    for name in ("plain", "wired"):
        (tmp_path / name).mkdir()
    plain, wired = World(tmp_path / "plain"), World(tmp_path / "wired")
    agent = Agent()
    wired.ports = replace(wired.ports, resolve_conflict=agent)
    outcomes = [_old_collision(w) for w in (plain, wired)]
    assert agent.asked == []
    assert [(o.status, o.detail, o.events) for o in outcomes[0]] == [(o.status, o.detail, o.events) for o in outcomes[1]]
    same = lambda w: json.dumps({**w.record("o"), "repo": ""}, sort_keys=True)  # noqa: E731
    assert same(plain) == same(wired)
    record = plain.record("o")
    assert "lane_model" not in record and all("resolved_by" not in e for e in record["phases"].values())
    assert [r[:2] for r in plain.rectified] == [r[:2] for r in wired.rectified] == [("o", "p2")]
    for world in (plain, wired):
        assert "<<<<<<<" in run(world.repo, "show", "epic/o/p2:shared.txt"), "the conflict is committed for the rectify task"
    refs = lambda w: run(w.repo, "for-each-ref", "--format=%(refname) %(objectname)")  # noqa: E731
    assert refs(plain) == refs(wired)


def test_one_pass_sends_an_old_model_conflict_to_rectify_and_a_new_model_one_to_the_merge_agent(models: Models) -> None:
    _collide(models, "n")
    models.start("o", [task("c", "p1"), task("d", "p2")])
    models.build("o", "p1", "shared.txt", P1_TEXT)
    models.build("o", "p2", "shared.txt", P2_TEXT)
    lander.run_once(models.ports, phase_cap=5)
    assert [a.initiative for a, _ in models.agent.asked] == ["n"]
    assert [r[:2] for r in models.rectified] == [("o", "p2")]
    assert models.record("n")["phases"]["p2"]["state"] == "merged"
    assert models.record("o")["phases"]["p2"]["state"] == "conflicted"


def test_adopt_stamps_the_initiatives_lane_model_on_the_record_it_makes(models: Models) -> None:
    for initiative, stamp in (("n", {"lane_model": "phase"}), ("o", {})):
        models.rows[initiative] = [task("a", "p1")]
        models.meta_of[initiative] = {"repo": str(models.repo), **stamp}
        models.ports = replace(models.ports, repo=lambda record: models.repo)
        found = lander_adopt.plan(models.ports, initiative)
        lander_adopt.apply(models.ports, found, lambda i, t, why: None)
    assert models.record("n")["lane_model"] == "phase"
    assert "lane_model" not in models.record("o")
    assert ls.get_record(models.conn, "o") == models.record("o")
