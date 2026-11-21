"""D1 wired to L5: a new-model phase conflict goes to the `merge_tasks` seat through the local harness, and its answer
meets L5's guards (conflict markers, diff3 included; lines a side added) before D1's own index and level checks.

The pure guards first, then `merge_into` with a real conflict in a real repo, then `lander_merge.resolve` with the seat
as a callable (the runner stand-in), then the whole lander pass over `test_lander_models`' origin and worker clone.
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_tools import lander, lander_hold, lander_merge, lander_ports
from harness import landing_git as lg
from harness import lane_merge
from tests.test_lander import run
from tests.test_lander_models import BOTH, P1_TEXT, P2_TEXT, Models, _collide, _pass, _two_sides

# -- the pure guards -------------------------------------------------------------------------------------------------

STAGES = {"f.txt": ("one\ntwo\n", "one\nmain\n", "one\nside\n")}


def test_the_guards_pass_a_merge_that_keeps_every_line_either_side_added():
    assert lg.guard_problem({"f.txt": "one\nmain\nside\n"}, STAGES) is None


def test_the_guards_refuse_a_diff3_marker_that_the_answer_check_alone_lets_through():
    diff3 = "one\nmain\n||||||| base\ntwo\nside\n"
    assert lg.answer_problem({"f.txt": diff3}, ["f.txt"]) is None, "the old marker check misses diff3"
    assert "conflict markers in f.txt" in str(lg.guard_problem({"f.txt": diff3}, STAGES))


def test_the_guards_refuse_a_merge_that_drops_a_line_a_side_added():
    why = str(lg.guard_problem({"f.txt": "one\nmain\n"}, STAGES))
    assert "dropped lines a side added in f.txt" in why and "'side'" in why


# -- merge_into: the guards on a real conflict, read from its index stages -------------------------------------------

@pytest.mark.parametrize(("merged", "why"), [
    ("one\nmain\n", "dropped lines a side added in f.txt: f.txt: 'side'"),
    ("one\nmain\n|||||||\nside\n", "left conflict markers in f.txt"),
])
def test_a_resolution_the_guards_refuse_is_a_refusal_and_the_branch_stays(tmp_path: Path, merged: str, why: str):
    repo, old = _two_sides(tmp_path)
    done = lg.merge_into(repo, branch="main", source="side", message="m",
                         resolve=lambda s, p: lg.Resolved({"f.txt": merged}, "a"))
    assert (done.status, done.unsettled) == ("conflict", "refused") and why in done.detail, done
    assert run(repo, "rev-parse", "main") == old
    assert lander_hold.merge_cause("conflict", done.unsettled, done.detail) == "refused"
    assert lander_hold.retry_delay("refused", 1) is None, "a refusal is never retried on its own"


def test_a_first_line_s_indent_is_read_from_the_stages_as_it_is(tmp_path: Path):
    """The stages are read raw: an indented first line a side added is the same line in the merge, not a lost one."""
    repo = tmp_path / "g"
    repo.mkdir()
    run(repo, "init", "-q", "-b", "main")
    (repo / "f.py").write_text("    a = 1\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "base")
    run(repo, "checkout", "-qb", "side")
    (repo / "f.py").write_text("    a = 2\n")
    run(repo, "commit", "-qam", "side")
    run(repo, "checkout", "-q", "main")
    (repo / "f.py").write_text("    a = 3\n")
    run(repo, "commit", "-qam", "main")
    done = lg.merge_into(repo, branch="main", source="side", message="m",
                         resolve=lambda s, p: lg.Resolved({"f.py": "    a = 3\n    a = 2\n"}, "a"))
    assert done.status == "merged", done


# -- lander_merge.resolve: the seat as a callable --------------------------------------------------------------------

class Seat:
    """The `merge_tasks` seat's stand-in: the prompts and labels it was given, and its reply."""

    def __init__(self, reply: dict[str, object] | None = None, *, wait_s: float = 0.0,
                 fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.reply = reply
        self.wait_s, self.fail = wait_s, fail

    def __call__(self, prompt: str, task: str) -> dict[str, object]:
        self.calls.append((prompt, task))
        if self.wait_s:
            time.sleep(self.wait_s)
        if self.fail is not None:
            raise self.fail
        return self.reply or {}


def _merged(text: str, path: str = "f.txt") -> dict[str, object]:
    return {"resolved": True, "files": [{"path": path, "content": text}], "summary": "kept both"}


def _ask(scratch: Path) -> lander.ConflictAsk:
    return lander.ConflictAsk("n", "p2", "main", "side", scratch, ("f.txt",))


def test_no_seat_is_the_retryable_no_merge_agent_hold(tmp_path: Path):
    assert lander_merge.resolve(_ask(tmp_path), None) == lander_hold.NO_MERGE_AGENT
    assert lander_hold.merge_cause("conflict", "refused", f"conflict in f.txt: {lander_hold.NO_MERGE_AGENT}") == "no_merge_agent"


def test_the_seat_gets_l5s_brief_with_both_sides_and_its_files_are_the_merge(tmp_path: Path):
    repo, old = _two_sides(tmp_path)
    seat = Seat(_merged("one\nmain\nside\n"))
    done = lg.merge_into(repo, branch="main", source="side", message="m",
                         resolve=lambda scratch, paths: lander_merge.resolve(
                             lander.ConflictAsk("n", "p2", "main", "side", scratch, paths), seat))
    assert done.status == "merged" and done.resolved_by == "the merge_tasks seat (kept both)", done
    [(prompt, task)] = seat.calls
    assert task == "n/p2"
    assert "<<<<<<<" in prompt and "main" in prompt and "side" in prompt and '<file path="f.txt">' in prompt
    assert "+side" in prompt, "the phase's whole change since it parted from the branch"
    assert run(repo, "show", "main:f.txt") == "one\nmain\nside" and run(repo, "rev-parse", "main^1") == old


@pytest.mark.parametrize(("reply", "why"), [
    ({"resolved": False, "files": [], "summary": "they contradict"}, "did not resolve it: they contradict"),
    (_merged("x\n", "g.txt"), "left out f.txt"),
])
def test_a_reply_that_is_not_a_merge_is_a_refusal(tmp_path: Path, reply: dict[str, object], why: str):
    repo, _ = _two_sides(tmp_path)
    done = lg.merge_into(repo, branch="main", source="side", message="m",
                         resolve=lambda scratch, paths: lander_merge.resolve(_ask(scratch), Seat(reply)))
    assert (done.status, done.unsettled) == ("conflict", "refused") and why in done.detail, done


def test_a_seat_that_runs_past_its_bound_is_a_retryable_resolver_error(tmp_path: Path):
    repo, old = _two_sides(tmp_path)
    seat = Seat(_merged("one\nmain\nside\n"), wait_s=2.0)
    started = time.monotonic()
    done = lg.merge_into(repo, branch="main", source="side", message="m",
                         resolve=lambda scratch, paths: lander_merge.resolve(_ask(scratch), seat, timeout_s=0.2))
    assert time.monotonic() - started < 1.5, "the merge does not wait for the agent past its bound"
    assert (done.status, done.unsettled) == ("conflict", "raised") and "TimeoutError" in done.detail, done
    assert lander_hold.merge_cause("conflict", done.unsettled, done.detail) == "resolver_error"
    assert lander_hold.retry_delay("resolver_error", 1) is not None, "retried, a bounded number of times"
    assert run(repo, "rev-parse", "main") == old


def test_a_seat_that_raises_raises_through_the_bound():
    broken = Seat(fail=RuntimeError("the runner fell over"))
    with pytest.raises(RuntimeError, match="the runner fell over"):
        lander_merge.bounded(lambda: broken("p", "t"), 5.0)
    assert lander_merge.bounded(lambda: 7, 5.0) == 7


def test_the_agents_bound_leaves_the_level_checks_room_inside_the_branch_lease():
    from harness import landing_ops as ops

    assert 0 < lander_merge.AGENT_TIMEOUT_S <= ops.CLAIM_SECONDS / 2


# -- the real port: no seat bound, no crash ----------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [{}, {"team": "smoke"}, {"team": "smoke", "cartridges_dir": "/nowhere"}])
def test_a_profile_that_names_no_seat_gives_the_no_merge_agent_hold(tmp_path: Path, profile: dict[str, object]):
    assert lander_ports.merge_seat(profile, tmp_path, tmp_path) is None
    resolve = lander_ports.conflict_resolver(profile, tmp_path)
    assert resolve(_ask(tmp_path)) == lander_hold.NO_MERGE_AGENT


def test_a_cartridge_that_binds_no_merge_tasks_seat_gives_the_no_merge_agent_hold(tmp_path: Path):
    (tmp_path / "c" / "smoke").mkdir(parents=True)
    (tmp_path / "c" / "smoke" / "cartridge.yaml").write_text("team: smoke\nversion: 1\n", encoding="utf-8")
    profile = {"team": "smoke", "cartridges_dir": str(tmp_path / "c"), "provider_profile": str(tmp_path / "p.yaml")}
    assert lander_ports.merge_seat(profile, tmp_path, tmp_path) is None


# -- the lander pass -------------------------------------------------------------------------------------------------

def _seated(models: Models, seat: Seat) -> None:
    models.ports = replace(models.ports, resolve_conflict=lambda ask: lander_merge.resolve(ask, seat))


@pytest.fixture
def models(tmp_path: Path) -> Models:
    return Models(tmp_path)


def test_two_colliding_phases_roll_up_through_the_seat_and_origin_holds_both(models: Models) -> None:
    p1, p2 = _collide(models)
    seat = Seat(_merged(BOTH, "shared.txt"))
    _seated(models, seat)
    out = lander.run_once(models.ports, phase_cap=5)
    assert [o.status for o in out[:2]] == ["done", "done"], out
    assert [t for _, t in seat.calls] == ["n/p2"]
    tip = models.on_origin("init/n")
    assert lg.is_ancestor(models.origin, p1, tip) and lg.is_ancestor(models.origin, p2, tip)
    assert run(models.origin, "show", f"{tip}:shared.txt") == BOTH.rstrip("\n")


def test_a_seat_that_drops_a_side_is_refused_and_never_retried_on_its_own(models: Models) -> None:
    _collide(models)
    seat = Seat(_merged(P2_TEXT, "shared.txt"))  # p2's line only: p1's is lost
    _seated(models, seat)
    _pass(models)
    held = models.record("n")["held"]
    assert held["cause"] == "refused" and "dropped lines a side added in shared.txt" in held["reason"], held
    assert "TWO from p1" in held["reason"] and "TWO from p1" in P1_TEXT
    _pass(models, 20000)
    assert len(seat.calls) == 1 and models.record("n")["held"] == held, "not retried on its own"


def test_a_seat_that_times_out_is_retried_and_merges_once_it_answers(models: Models) -> None:
    _collide(models)
    gate = threading.Event()
    slow = Seat(_merged(BOTH, "shared.txt"), wait_s=1.0)
    models.ports = replace(models.ports, resolve_conflict=lambda ask: lander_merge.resolve(
        ask, slow, timeout_s=0.05 if not gate.is_set() else 5.0))
    _pass(models)
    held = models.record("n")["held"]
    assert held["cause"] == "resolver_error" and "did not answer within" in held["reason"], held
    gate.set()
    _pass(models, 600)
    assert models.record("n").get("held") is None and models.record("n")["phases"]["p2"]["state"] == "merged"
