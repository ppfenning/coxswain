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

from agent_tools import lander, lander_adopt, lander_base, lander_cli, lander_hold, lander_ports, lander_release
from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_ops as ops
from harness import landing_store as ls
from harness import store_lease
from tests.test_lander import ID, World, run, task

T0 = "2026-11-20T00:00:00Z"
GOLDEN = Path(__file__).parent / "fixtures" / "lander_old_model_collision.json"
P1_TEXT, P2_TEXT = "one\nTWO from p1\nthree\n", "one\nTWO from p2\nthree\n"
BOTH = "one\nTWO from p1\nTWO from p2\nthree\n"  # every line each side added, kept


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
    hold = lander_hold.merge_hold("p2", "a" * 40, "conflict in shared.txt: no", T0)
    assert hold["reason"].startswith(lander_hold.MERGE_HOLD + "p2 at aaaaaaaaaaaa")
    assert (hold["cause"], hold["attempts"], hold["last_attempt"]) == ("refused", 1, T0)
    held: lr.LandingRecord = {"initiative": "i", "held": hold}
    assert lander_hold.merge_held(held)
    assert not lander_hold.merge_held({"initiative": "i", "held": {"reason": "origin lacks it", "since": T0}})
    assert not lander_hold.merge_held({"initiative": "i"})
    assert not lander_hold.hold_lifted(held, "a" * 40)
    assert not lander_hold.hold_lifted(held, None)
    assert lander_hold.hold_lifted(held, "c" * 40)
    assert not lander_hold.hold_lifted({"initiative": "i", "held": {"reason": "other", "tip": "a" * 40}}, "c" * 40)


@pytest.mark.parametrize(("cause", "unsettled", "detail", "want"), [
    ("conflict", "refused", "conflict in a: the two edits contradict", "refused"),
    ("conflict", "refused", f"conflict in a: {lander_hold.NO_MERGE_AGENT}", "no_merge_agent"),
    ("conflict", "raised", "conflict in a: the resolver failed: RuntimeError: x", "resolver_error"),
    ("conflict", "fault", "conflict in a: the resolved merge could not be committed", "harness_fault"),
    ("checks_failed", "", "red", "checks_failed"),
    ("guard", "", "would drop abc", "guard"),
])
def test_a_merge_hold_records_why_it_was_held(cause, unsettled, detail, want):
    assert lander_hold.merge_cause(cause, unsettled, detail) == want


def _at(seconds: int) -> str:
    return ops.later(T0, seconds)


@pytest.mark.parametrize(("cause", "attempts", "wait", "due"), [
    ("resolver_error", 1, 599, False), ("resolver_error", 1, 600, True),
    ("resolver_error", 2, 1199, False), ("resolver_error", 2, 1200, True),
    ("harness_fault", 3, 2400, True), ("harness_fault", 4, 10**6, False),  # MERGE_RETRIES spent: never on its own
    ("no_merge_agent", 1, 3599, False), ("no_merge_agent", 1, 3600, True), ("no_merge_agent", 50, 3600, True),
    ("refused", 1, 10**6, False), ("checks_failed", 1, 10**6, False), ("guard", 1, 10**6, False),
])
def test_only_a_resolver_error_a_harness_fault_or_no_agent_is_retried_and_only_after_its_backoff(cause, attempts, wait, due):
    hold = lander_hold.merge_hold("p2", "a" * 40, "x", T0, cause, attempts)
    assert lander_hold.retry_due(hold, _at(wait)) is due


def test_a_released_hold_is_due_at_once_and_a_hold_with_no_cause_never_on_its_own():
    hold = lander_hold.merge_hold("p2", "a" * 40, "x", T0, "checks_failed")
    assert lander_hold.retry_due({**hold, "released": True}, T0)
    unrecorded = {"reason": hold["reason"], "since": T0, "phase": "p2", "tip": "a" * 40}  # made before causes were
    assert not lander_hold.retry_due(unrecorded, _at(10**6))
    assert not lander_hold.retry_due({"reason": "a drift", "since": T0, "released": True}, T0), "only a merge hold"
    assert lander_hold.MERGE_RETRIES == 3 and lander_hold.retry_delay("resolver_error", 3) == 2400


def test_the_count_carries_on_at_the_same_phase_and_tip_and_starts_again_at_a_new_one():
    first = lander_hold.next_hold(None, "p2", "a" * 40, "boom", T0, "resolver_error")
    assert (first["attempts"], first["since"]) == (1, T0)
    second = lander_hold.next_hold(first, "p2", "a" * 40, "boom", _at(600), "resolver_error")
    assert (second["attempts"], second["since"], second["last_attempt"]) == (2, T0, _at(600))
    assert lander_hold.next_hold(second, "p2", "b" * 40, "boom", _at(700), "resolver_error")["attempts"] == 1
    assert lander_hold.next_hold(second, "p3", "a" * 40, "boom", _at(700), "resolver_error")["attempts"] == 1


def test_the_chair_is_told_a_first_hold_a_new_reason_a_released_retry_and_retries_spent_never_a_repeat():
    hold = lambda n, cause="resolver_error": lander_hold.merge_hold("p2", "a" * 40, "x", T0, cause, n)  # noqa: E731
    assert lander_hold.told_now(None, hold(1), False)
    assert lander_hold.told_now(hold(1), hold(2), False)
    assert not lander_hold.told_now(hold(1), hold(2), True)
    assert lander_hold.told_now({**hold(1, "refused"), "released": True}, hold(2, "refused"), True)
    assert lander_hold.told_now(hold(3), hold(4), True), "the last automatic retry failed: told"
    assert not lander_hold.told_now(hold(5, "no_merge_agent"), hold(6, "no_merge_agent"), True)


def test_release_marks_only_a_merge_hold_and_says_why_not_otherwise():
    hold = lander_hold.merge_hold("p2", "a" * 40, "x", T0, "checks_failed", 2)
    released, said = lander_hold.release({"initiative": "i", "held": hold})
    assert released is not None and released["held"] == {**hold, "released": True}
    assert "phase p2 is released" in said and "checks_failed, 2 attempt(s)" in said
    assert lander_hold.release(released)[0] is None and "already released" in lander_hold.release(released)[1]
    assert lander_hold.release({"initiative": "i"}) == (None, "i is not held")
    drift, why = lander_hold.release({"initiative": "i", "held": {"reason": "origin lacks abc", "since": T0}})
    assert drift is None and "not by a merge: origin lacks abc" in why


def test_reconciliation_keeps_a_drift_beside_a_merge_hold_and_a_cleared_drift_takes_only_itself():
    hold = lander_hold.merge_hold("p2", "a" * 40, "x", T0, "refused")
    assert lander_hold.reconciled_hold({"reason": "plain", "since": T0}, "d", T0) is None, "not a merge hold"
    assert lander_hold.reconciled_hold(hold, None, T0) == (hold, False)
    kept, news = lander_hold.reconciled_hold(hold, "origin lacks abc", T0) or (None, False)
    assert news and kept == {**hold, "drift": "origin lacks abc"}
    assert lander_hold.reconciled_hold(kept, "origin lacks abc", T0) == (kept, False)
    assert lander_hold.reconciled_hold(kept, None, T0) == (hold, True)
    assert lander_hold.without_merge_hold(kept, _at(5)) == {"reason": "origin lacks abc", "since": _at(5)}
    assert lander_hold.without_merge_hold(hold, T0) is None


def test_strays_are_the_paths_besides_the_conflicted_ones_that_a_resolver_staged_or_changed():
    before = {"a.txt": "1 x 1;1 y 2;1 z 3;", "b.txt": "1 b 0;"}
    assert lg.strays(["a.txt"], before, {"a.txt": "1 m 0;", "b.txt": "1 b 0;"}, []) == []
    assert lg.strays(["a.txt"], before, {"a.txt": "1 m 0;", "b.txt": "1 c 0;"}, []) == ["b.txt"]
    assert lg.strays(["a.txt"], before, {"a.txt": "1 m 0;", "b.txt": "1 b 0;", "new.txt": "1 n 0;"}, []) == ["new.txt"]
    assert lg.strays(["a.txt"], before, {"a.txt": "1 m 0;"}, []) == ["b.txt"], "a removal is a change"
    assert lg.strays(["a.txt"], before, {**before, "a.txt": "1 m 0;"}, ["a.txt", "b.txt"]) == ["b.txt"]


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
        return lg.Resolved({"f.txt": "one\nmain\nside\n"}, "the agent")

    merged = lg.merge_into(repo, branch="main", source="side", message="m", resolve=resolve)
    assert "<<<<<<<" in seen[0] and "side" in seen[0] and "main" in seen[0]
    assert (merged.status, merged.resolved_by) == ("merged", "the agent")
    assert run(repo, "show", "main:f.txt") == "one\nmain\nside"
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
                           resolve=lambda s, p: lg.Resolved({"f.txt": "one\nmain\nside\n"}, "a"))
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
    assert held["reason"].startswith(f"{lander_hold.MERGE_HOLD}p2 at {p2[:12]}: conflict in shared.txt")
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
    assert lander_hold.merge_held(models.record("n"))
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


def _prime_rerere(repo: Path, ours: str, theirs: str, resolved: str) -> None:
    """Turn rerere on in `repo` and record a resolution of merging `theirs` into `ours`, in a worktree of its own that
    is then removed: the rr-cache remembers it, no ref moves."""
    run(repo, "config", "rerere.enabled", "true")
    side = repo.parent / "rerere-prime"
    run(repo, "worktree", "add", "-q", "--detach", str(side), ours)
    subprocess.run(["git", "-C", str(side), *ID, "merge", "--no-edit", theirs], capture_output=True, text=True)
    (side / "shared.txt").write_text(resolved)
    run(side, "add", "shared.txt")
    run(side, "commit", "-qm", "a resolution rerere records")
    run(repo, "worktree", "remove", "--force", str(side))
    assert list((repo / ".git" / "rr-cache").iterdir()), "rerere recorded the resolution"


def _old_collision(world: World, rerere: bool = False) -> list[lander.Outcome]:
    world.start("o", [task("a", "p1"), task("b", "p2")])
    world.build("o", "p1", "shared.txt", P1_TEXT)
    world.build("o", "p2", "shared.txt", P2_TEXT)
    if rerere:
        _prime_rerere(world.repo, "epic/o/p2", "epic/o/p1", BOTH)
    return lander.run_once(world.ports)


def _as_golden(world: World, outcomes: list[lander.Outcome], tmp: Path) -> dict:
    """What the golden copy pins, read the way it was captured (main's code, before D1, the same collision)."""
    def norm(value: object) -> object:
        text = json.dumps(value)
        for root in {str(tmp), str(tmp.resolve())}:
            text = text.replace(root, "<tmp>")
        return json.loads(text)
    return {
        "outcomes": norm([[o.status, o.item, o.detail, list(o.events)] for o in outcomes]),
        "record": norm({**world.record("o"), "repo": ""}),
        "rectified": norm([list(r) for r in world.rectified]),
        "notes": norm([[topic, note] for topic, note in world.notes]),
        "refs": run(world.repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines(),
        "p2_shared": run(world.repo, "show", "epic/o/p2:shared.txt"),
    }


@pytest.mark.parametrize(("wired", "rerere"), [(False, False), (True, False), (True, True)],
                         ids=["default-ports", "merge-agent-wired", "merge-agent-wired-and-rerere-primed"])
def test_the_old_model_is_exactly_as_before(tmp_path: Path, monkeypatch, wired: bool, rerere: bool) -> None:
    """The old-model collision against a golden copy of what main's lander did before D1 (captured once by running
    main's code on this same collision, `fixtures/lander_old_model_collision.json`): the same outcomes, record,
    rectify task, notices, refs and conflicted file. With a merge agent wired, it is never asked; with rerere on and a
    resolution of this very conflict recorded, the conflict is still committed with its markers, as it was."""
    for var in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):  # the clock the golden copy was captured on
        monkeypatch.setenv(var, "2026-11-20T00:00:00Z")
    world = World(tmp_path)
    agent = Agent()
    if wired:
        world.ports = replace(world.ports, resolve_conflict=agent)
    outcomes = _old_collision(world, rerere)
    assert agent.asked == []
    assert _as_golden(world, outcomes, tmp_path) == json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert "lane_model" not in world.record("o")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_the_batch_check_merges_with_rerere_off(world: World) -> None:
    """`_check_together` merges with rerere off, as every lander merge does: a resolution recorded for a conflict
    must not make a batch merge look settled, and the batch check records none."""
    for name, text in (("left", P1_TEXT), ("right", P2_TEXT)):
        run(world.repo, "branch", name, "main")
        commit_on(world.repo, name, text)
    _prime_rerere(world.repo, "right", "left", BOTH)
    cache = world.repo / ".git" / "rr-cache"
    recorded = sorted(p.name for p in cache.iterdir())
    ok, why = lander._check_together(world.ports, world.repo, "main", ["left", "right"])
    assert not ok and "right does not merge with the rest of the batch" in why
    assert sorted(p.name for p in cache.iterdir()) == recorded, "nothing recorded: rerere was off"
    run(world.repo, "config", "rerere.enabled", "true")
    for name, text in (("up", "one\nTWO up\nthree\n"), ("down", "one\nTWO down\nthree\n")):  # a conflict never seen
        run(world.repo, "branch", name, "main")
        commit_on(world.repo, name, text)
    ok, _ = lander._check_together(world.ports, world.repo, "main", ["up", "down"])
    assert not ok and sorted(p.name for p in cache.iterdir()) == recorded, "a new conflict is not recorded either"


def commit_on(repo: Path, branch: str, text: str) -> str:
    side = repo.parent / f"side-{branch}"
    run(repo, "worktree", "add", "-q", str(side), branch)
    (side / "shared.txt").write_text(text)
    run(side, "commit", "-qam", f"{branch}: edit the line")
    tip = run(side, "rev-parse", "HEAD")
    run(repo, "worktree", "remove", "--force", str(side))
    return tip


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


# -- the merge hold's exits: a new tip, the automatic retry, an operator's release ---------------------------------------

class Flaky(Agent):
    """An agent that raises `fails` times, then answers."""

    def __init__(self, fails: int) -> None:
        super().__init__()
        self.fails = fails

    def __call__(self, ask: lander.ConflictAsk) -> lg.Resolved | str:
        answer = super().__call__(ask)
        if self.fails > 0:
            self.fails -= 1
            raise RuntimeError("the harness lost the agent")
        return answer


def _pass(models: Models, seconds: int = 0) -> list[lander.Outcome]:
    models.clock += seconds
    return lander.run_once(models.ports, phase_cap=5)


def _told(models: Models) -> list[str]:
    return [n["reason"] for topic, n in models.notes if topic == "needs_chair"]


def test_a_resolver_that_raises_is_retried_with_backoff_and_merges_once_it_answers(models: Models) -> None:
    _collide(models)
    models.agent = Flaky(fails=2)
    models.ports = replace(models.ports, resolve_conflict=models.agent)
    _pass(models)
    held = models.record("n")["held"]
    assert (held["cause"], held["attempts"]) == ("resolver_error", 1) and "RuntimeError" in held["reason"]
    _pass(models, 300)  # inside the first backoff (600 s): not tried
    assert len(models.agent.asked) == 1
    _pass(models, 400)
    assert len(models.agent.asked) == 2 and models.record("n")["held"]["attempts"] == 2
    assert models.record("n")["held"]["since"] == held["since"], "one hold, counted on"
    _pass(models, 1300)
    assert len(models.agent.asked) == 3
    record = models.record("n")
    assert record.get("held") is None and record["phases"]["p2"]["state"] == "merged" and not record.get("retried")
    assert len(_told(models)) == 1, "a retry that fails the same way is not told again"


def test_a_resolver_that_keeps_raising_stops_after_its_retries_and_the_chair_is_told_they_are_spent(models: Models) -> None:
    _collide(models)
    models.agent = Flaky(fails=100)
    models.ports = replace(models.ports, resolve_conflict=models.agent)
    for wait in (0, 600, 1200, 2400, 9000, 9000):
        _pass(models, wait)
    held = models.record("n")["held"]
    assert len(models.agent.asked) == lander_hold.MERGE_RETRIES + 1 == held["attempts"]
    assert not lander_hold.retry_due(held, models.now())
    told = _told(models)
    assert len(told) == 2 and "its automatic retries are spent" in told[1]


def test_a_harness_fault_committing_the_answer_is_retried(models: Models) -> None:
    _collide(models)

    def lock_the_index(ask: lander.ConflictAsk) -> lg.Resolved:
        git_dir = run(ask.scratch, "rev-parse", "--absolute-git-dir")
        (Path(git_dir) / "index.lock").write_text("")  # git cannot stage the answer: the harness's fault, not the agent's
        return lg.Resolved(dict.fromkeys(ask.paths, BOTH), "a")

    models.ports = replace(models.ports, resolve_conflict=lock_the_index)
    _pass(models)
    held = models.record("n")["held"]
    assert held["cause"] == "harness_fault" and "could not be staged" in held["reason"]
    models.ports = replace(models.ports, resolve_conflict=models.agent)
    _pass(models, 600)
    assert models.record("n").get("held") is None and models.record("n")["phases"]["p2"]["state"] == "merged"


def test_no_merge_agent_yet_is_retried_hourly_and_merges_once_a_merge_agent_is_wired(models: Models) -> None:
    """L5 lands: a conflict held only because no merge agent was wired is merged on the next retry, not stuck."""
    _collide(models)
    models.ports = replace(models.ports, resolve_conflict=lander_base._no_merge_agent)  # the lander as it is before L5
    _pass(models)
    held = models.record("n")["held"]
    assert held["cause"] == "no_merge_agent" and lander_hold.NO_MERGE_AGENT in held["reason"]
    for _ in range(3):  # unbounded: however long the wait, it is tried again
        _pass(models, 3601)
    assert models.record("n")["held"]["attempts"] == 4 and len(_told(models)) == 1, "told once"
    models.ports = replace(models.ports, resolve_conflict=models.agent)  # L5 wired
    _pass(models, 3601)
    assert models.record("n").get("held") is None and models.record("n")["phases"]["p2"]["state"] == "merged"
    assert len(models.agent.asked) == 1


@pytest.mark.parametrize("cause", ["refused", "checks_failed"])
def test_a_refusal_or_a_failed_level_check_waits_for_a_person_who_can_release_it(models: Models, cause: str) -> None:
    _collide(models)
    if cause == "refused":
        models.agent.refuse = "the two edits contradict each other"
    else:
        models.check_result["phase"] = (False, "tests/test_x.py::test_y FAILED")
    _pass(models)
    held = models.record("n")["held"]
    assert held["cause"] == cause
    _pass(models, 20000)
    assert len(models.agent.asked) == 1 and models.record("n")["held"] == held, "never retried on its own"
    released, said = lander_release.release_merge_hold(models.ports, "n")
    assert released and "released" in said and models.record("n")["held"]["released"] is True
    models.agent.refuse = None
    models.check_result.pop("phase", None)
    out = _pass(models)
    assert any("an operator released it" in e for e in out_events(models, out))
    assert len(models.agent.asked) == 2 and models.record("n").get("held") is None
    assert models.record("n")["phases"]["p2"]["state"] == "merged"


def out_events(models: Models, out: list[lander.Outcome]) -> list[str]:
    return [*models.logged, *(e for o in out for e in o.events)]


def test_a_released_merge_that_fails_again_holds_again_counted_and_told(models: Models) -> None:
    _collide(models)
    models.agent.refuse = "no"
    _pass(models)
    assert lander_release.release_merge_hold(models.ports, "n")[0]
    _pass(models)
    held = models.record("n")["held"]
    assert (held["attempts"], held.get("released")) == (2, None) and len(models.agent.asked) == 2
    assert len(_told(models)) == 2, "the operator who released it hears how it went"


def test_release_refuses_what_is_not_a_merge_hold(models: Models) -> None:
    assert lander_release.release_merge_hold(models.ports, "ghost") == (False, "ghost has no landing record")
    record = models.start_new("n", [task("a", "p1")])
    assert lander_release.release_merge_hold(models.ports, "n") == (False, "n is not held")
    ls.put_record(models.conn, {**record, "held": {"reason": "origin lacks abc", "since": T0}})
    released, why = lander_release.release_merge_hold(models.ports, "n")
    assert not released and "not by a merge" in why


def test_release_waits_for_the_branch_lease_and_never_overwrites_a_writer_midway(models: Models) -> None:
    _collide(models)
    models.agent.refuse = "no"
    _pass(models)
    held = models.record("n")["held"]
    lease = ops.branch_lease("n")
    sweep = store_lease.acquire(models.conn, lease, "sweep-on-other-host", models.now(), ops.CLAIM_SECONDS)
    assert sweep.ok
    released, said = lander_release.release_merge_hold(models.ports, "n")
    assert not released and "sweep-on-other-host is moving the branch of n" in said
    assert models.record("n")["held"] == held, "nothing written while another writer holds the branch"
    store_lease.release(models.conn, lease, "sweep-on-other-host", int(sweep.epoch or 0))
    released, _ = lander_release.release_merge_hold(models.ports, "n")
    assert released and models.record("n")["held"]["released"] is True
    after = store_lease.acquire(models.conn, lease, "sweep-on-other-host", models.now(), ops.CLAIM_SECONDS)
    assert after.ok, "the release gives the branch lease back"


def test_cox_lander_release_runs_the_release(models: Models, monkeypatch, capsys) -> None:
    _collide(models)
    models.agent.refuse = "no"
    _pass(models)
    monkeypatch.setattr(lander_cli, "_workspace", lambda a: (Path("."), {}))
    monkeypatch.setattr(lander_ports, "build", lambda ws, profile: replace(models.ports, conn=_Unclosed(models.conn)))
    args = lander_cli.argparse.Namespace(initiative="n", profile=None)
    assert lander_cli.cmd_release(args) == 0 and "phase p2 is released" in capsys.readouterr().out
    assert lander_cli.cmd_release(args) == 1 and "already released" in capsys.readouterr().out


class _Unclosed:
    """The store connection, with `close` a no-op: the test reads the store after the command closes it."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        return None


def test_a_drift_reconciliation_finds_is_kept_beside_the_merge_hold_and_clearing_it_keeps_the_hold(
        models: Models, monkeypatch) -> None:
    _, p2 = _collide(models)
    models.agent.refuse = "no"
    _pass(models)
    merge = models.record("n")["held"]
    found: list[str | None] = ["origin's init/n lacks phase p1's merge abc"]
    monkeypatch.setattr(lander, "_judge", lambda ports, repo, initiative: lander.Judgement(reason=found[0]))
    events = lander.reconcile(models.ports, "n")
    held = models.record("n")["held"]
    assert held == {**merge, "drift": found[0]} and "beside its merge hold" in events[-1]
    assert _told(models)[-1] == f"held: {found[0]}"
    assert lander.reconcile(models.ports, "n") == [] and len(_told(models)) == 2, "an unchanged drift is not told twice"
    assert found[0] in lander.held_note(models.record("n"))
    found[0] = None
    events = lander.reconcile(models.ports, "n")
    assert models.record("n")["held"] == merge, "the drift cleared; the merge hold, with its phase and tip, stays"
    assert "the merge hold stays" in events[-1] and len(_told(models)) == 2
    assert lander.reconcile(models.ports, "n") == []
    _pass(models, 20000)
    assert len(models.agent.asked) == 1, "the merge agent is not asked again: the hold never lifted"
    assert models.record("n")["held"]["tip"] == p2


def test_a_merge_hold_lifted_by_a_new_tip_leaves_a_drift_kept_beside_it(models: Models, monkeypatch) -> None:
    _collide(models)
    models.agent.refuse = "no"
    _pass(models)
    monkeypatch.setattr(lander, "_judge", lambda ports, repo, initiative: lander.Judgement(reason="a drift"))
    lander.reconcile(models.ports, "n")
    models.lane("n", "p2", "p2-note.txt", "fixed\n")
    events = lander.sweep(models.ports)
    assert models.record("n")["held"]["reason"] == "a drift" and not lander_hold.merge_held(models.record("n"))
    assert any("stays held: a drift" in e for e in events)


# -- the scratch the agent answers in: nothing but the conflicted paths may change ----------------------------------------

def test_a_resolver_that_stages_or_changes_another_file_is_refused(tmp_path: Path) -> None:
    repo, old = _two_sides(tmp_path)
    (repo / "g.txt").write_text("g\n")
    run(repo, "add", "g.txt")
    run(repo, "commit", "-qm", "g")
    old = run(repo, "rev-parse", "main")

    def sneaky(edit: str):
        def resolve(scratch: Path, paths: tuple[str, ...]) -> lg.Resolved:
            if edit == "modify":
                (scratch / "g.txt").write_text("changed\n")
            elif edit == "stage":
                (scratch / "h.txt").write_text("new\n")
                run(scratch, "add", "h.txt")
            elif edit == "commit":
                run(scratch, "add", "-A")
                run(scratch, "commit", "-qm", "the agent committed it itself")
            return lg.Resolved({"f.txt": "one\nmain\nside\n"}, "a")
        return resolve

    for edit, why in (("modify", "touched files that did not conflict: g.txt"),
                      ("stage", "touched files that did not conflict: h.txt"),
                      ("commit", "moved the merge it was given")):
        merged = lg.merge_into(repo, branch="main", source="side", message="m", resolve=sneaky(edit))
        assert (merged.status, merged.unsettled) == ("conflict", "refused") and why in merged.detail, (edit, merged)
        assert run(repo, "rev-parse", "main") == old
    clean = lg.merge_into(repo, branch="main", source="side", message="m", resolve=sneaky("none"))
    assert clean.status == "merged" and run(repo, "show", "main:g.txt") == "g", "the merge's own changes are kept"


def test_a_resolver_that_raises_or_refuses_says_which(tmp_path: Path) -> None:
    repo, _ = _two_sides(tmp_path)
    assert lg.merge_into(repo, branch="main", source="side", message="m", resolve=lambda s, p: "no").unsettled == "refused"

    def broken(scratch, paths):
        raise RuntimeError("x")

    assert lg.merge_into(repo, branch="main", source="side", message="m", resolve=broken).unsettled == "raised"
    assert lg.merge_into(repo, branch="main", source="side", message="m").unsettled == "", "no resolver asked"
