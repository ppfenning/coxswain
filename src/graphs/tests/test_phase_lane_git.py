"""The phase lane's roll-up message and its reviewer's diff, against real git: a revision committed through the merge
path (`_collide`) still carries its kind and round, and the diff a phase's reviewer reads is the phase's own however
far its base has moved. The rest of the lane is `smoke/test_phase_lane.py`'s and `smoke/test_lane_merge.py`'s."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from harness import lane_merge, lane_progress, lane_rollup, phase_lane


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git("add", name, cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    origin, repo = tmp_path / "origin.git", tmp_path / "app"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    base = _commit(repo, "app.py", "a\n", "init")
    _git("push", "-q", "origin", "main", cwd=repo)
    return repo, base


def _stub_lane(repo: Path, base: str, items: dict[str, dict[str, object]]) -> SimpleNamespace:
    """What `_roll_up` reads of a lane, and nothing else: no store, the claim always held."""
    return SimpleNamespace(
        claim=SimpleNamespace(initiative="ship", phase="p1"), items=items, shared=None, phase_worktree=repo,
        ctx=SimpleNamespace(run_id="ship.p1-1", repo=repo), base_sha=base, branch="epic/ship/p1",
        stopping=lambda: False, fence=SimpleNamespace(check=lambda: True), put=lambda rows: True,
        row=lambda *args: {}, log=lambda line: None,
    )


def test_a_revision_merged_on_the_roll_up_keeps_its_kind_and_round(tmp_path, monkeypatch):
    """The revision's change collides with t1 on the tip; the merge agent's change lands with `Cox-Merged-With`, and
    the commit is still the round's revision, so `lane_progress.read` counts the round and the loop moves on."""
    repo, base = _repo(tmp_path)
    task_wt = tmp_path / "rev"
    _git("worktree", "add", "-q", "--detach", str(task_wt), base, cwd=repo)
    _commit(task_wt, "app.py", "c\n", "the revision")
    _commit(repo, "app.py", "b\n", "t1")  # the tip moved: the revision's change does not apply
    tip = _git("rev-parse", "HEAD", cwd=repo)
    (repo / "app.py").write_text("b\nc\n", encoding="utf-8")
    merged_change = _git("diff", "--binary", cwd=repo) + "\n"
    _git("checkout", "-q", "--", "app.py", cwd=repo)

    asked: list[str] = []

    def collide(lane, task, at_base, at_tip, diff):
        asked.append(task)
        return lane_merge.Merge("commit", "t1", tip, ("app.py",), change=merged_change)

    monkeypatch.setattr(phase_lane, "_collide", collide)
    lane = _stub_lane(repo, base, {"p1-rev-2": {"id": "p1-rev-2", "kind": "revision", "round": 2, "title": "Dedup"}})
    tctx = SimpleNamespace(task_worktree=lambda phase, task: task_wt)
    rolled, sha = phase_lane._roll_up(lane, "p1-rev-2", base, tctx, "now")
    assert (rolled, asked) == (True, ["p1-rev-2"])
    trailers = _git("log", "-1", "--format=%(trailers)", sha, cwd=repo).splitlines()
    assert {"Cox-Kind: revision", "Cox-Round: 2", "Cox-Merged-With: t1", "Cox-Task: p1-rev-2"} <= set(trailers)
    ok, text = lane_rollup.log_since(repo, base, sha)
    assert ok and lane_progress.read(text, "ship", "p1").revisions == (2,)


def test_the_reviewer_s_diff_is_the_phase_s_own_after_its_base_moved(tmp_path):
    """Another phase landed on `init/` after this one was cut: the merge-base diff shows only this phase's change,
    where `base..tip` would show the other phase's file as deleted."""
    repo, cut = _repo(tmp_path)
    _git("checkout", "-q", "-b", "epic/ship/p1", cut, cwd=repo)
    tip = _commit(repo, "phase.txt", "mine\n", "p1's task")
    _git("checkout", "-q", "-b", "init/ship", cut, cwd=repo)
    moved = _commit(repo, "other.txt", "p0's\n", "p0 landed")
    ok, diff = phase_lane.review_diff(repo, moved, tip)
    assert ok and "phase.txt" in diff and "other.txt" not in diff
    two_dot = _git("diff", "--no-renames", moved, tip, cwd=repo)
    assert "deleted file" in two_dot and "other.txt" in two_dot  # what the reviewer saw before
