"""D1: one lander pass handles both lane models. Real `cox route import`, `cox lander adopt --apply` and `cox lander once`
against real clones of real bare origins and the store, with the local forge.

Two initiatives in one store, each in a repository of its own, and in each two phases that edit the same line of the
same file, so the second phase's merge into the initiative branch conflicts:

- `fresh` is on the new model (`lane_model: phase` on its `initiative.md`, stamped on its landing record by adopt). Its
  phases are built on another clone, as the phase lane builds them on a worker, and pushed to origin: the lander's
  clone has no copy of them until it fetches. Its conflict goes to the merge agent. No merge agent is wired yet (L5,
  #1309), so the port refuses, and the initiative holds with needs_chair: nothing is merged that nobody resolved, no
  rectify task is filed, and the lander never writes the phase branch. `cox lander release` tries it again.
- `legacy` is on the old model (no `lane_model`). Its conflict is committed, markers and all, on the phase branch,
  and a rectify task is filed, exactly as before D1.
"""

from __future__ import annotations

from pathlib import Path

from smoke import plan
from smoke.test_lander_lanes import _git, _origin_git, _profile_with_cartridge, _record, _repository

LINE = "one\ntwo\nthree\n"
EDITS = {"p1": "one\nTWO from p1\nthree\n", "p2": "one\nTWO from p2\nthree\n"}


def _files(sandbox, initiative: str, name: str, tips: dict[str, str], *, new_model: bool) -> None:
    """The initiative and one task per phase, approved at its tip once `tips` names one, imported into the store."""
    own = {"title": initiative, "repo": f"~/src/{name}", "draft": "false", **({"lane_model": "phase"} if new_model else {})}
    sandbox.write(f"work/{initiative}/initiative.md", plan.work_item_text(own, "An initiative."))
    for phase in ("p1", "p2"):
        tip = tips.get(phase, "")
        fields = {"id": f"{initiative}-{phase}", "title": f"Edit the line in {phase}", "phase": phase,
                  "state": "approved" if tip else "ready", **({"merged_commit": tip} if tip else {})}
        sandbox.write(f"work/{initiative}/{phase}/{initiative}-{phase}.md", plan.work_item_text(fields, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _commit(sandbox, where: Path, branch: str, start: str, text: str) -> str:
    _git(sandbox, where, "checkout", "--quiet", "-B", branch, start)
    (where / "shared.txt").write_text(text, encoding="utf-8")
    _git(sandbox, where, "add", "shared.txt")
    _git(sandbox, where, "commit", "--quiet", "-m", f"{branch}: edit the line")
    tip = _git(sandbox, where, "rev-parse", "HEAD")
    _git(sandbox, where, "checkout", "--quiet", "main")
    return tip


def _repo_with_line(sandbox, tmp_path: Path, name: str) -> tuple[Path, Path]:
    repo, origin = _repository(sandbox, tmp_path, name, "exit 0\n")
    (repo / "shared.txt").write_text(LINE, encoding="utf-8")
    _git(sandbox, repo, "add", "shared.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", "the shared line")
    _git(sandbox, repo, "push", "--quiet", "origin", "main")
    return repo, origin


def _origin_has(sandbox, origin: Path, ref: str) -> str:
    done = _origin_git(sandbox, origin, "rev-parse", "--verify", "--quiet", ref)
    return done.stdout.strip() if done.returncode == 0 else ""


def test_one_pass_holds_a_new_model_conflict_for_the_merge_agent_and_sends_an_old_model_one_to_rectify(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path, gate="ticket")
    fresh, fresh_origin = _repo_with_line(sandbox, tmp_path, "fresh")
    legacy, _ = _repo_with_line(sandbox, tmp_path, "legacy")
    _files(sandbox, "fresh", "fresh", {}, new_model=True)
    _files(sandbox, "legacy", "legacy", {}, new_model=False)
    for initiative in ("fresh", "legacy"):  # the records, before anything is built: adopt merges nothing yet
        adopted = sandbox.cox("lander", "adopt", "--apply", initiative)
        assert adopted.returncode == 0 and f"{initiative}: record made" in adopted.stdout, adopted.stdout + adopted.stderr
    assert _record(sandbox, "fresh")["lane_model"] == "phase"
    assert "lane_model" not in _record(sandbox, "legacy")

    # The new model's phases: built on a worker's clone and pushed to origin, never in the lander's clone.
    worker = tmp_path / "worker"
    _git(sandbox, tmp_path, "clone", "--quiet", str(fresh_origin), str(worker))
    new_tips = {p: _commit(sandbox, worker, f"epic/fresh/{p}", "origin/main", text) for p, text in EDITS.items()}
    for phase in EDITS:
        _git(sandbox, worker, "push", "--quiet", "origin", f"epic/fresh/{phase}")
    # The old model's phases: on the lander's own clone, as an old-model build run leaves them.
    old_tips = {p: _commit(sandbox, legacy, f"epic/legacy/{p}", "main", text) for p, text in EDITS.items()}
    _files(sandbox, "fresh", "fresh", new_tips, new_model=True)
    _files(sandbox, "legacy", "legacy", old_tips, new_model=False)

    done = sandbox.cox("lander", "once")

    out = done.stdout + done.stderr
    assert done.returncode == 1, out  # `once` exits 1 when an item failed: both p2 merges did
    assert "phase p1 of fresh merged into init/fresh" in out and "phase p1 of legacy merged into init/legacy" in out, out
    # New model: the merge agent was asked, could not (none is wired), and the initiative holds; the chair is told.
    held = _record(sandbox, "fresh")["held"]
    assert held["reason"].startswith(f"the lander could not merge new-model phase p2 at {new_tips['p2'][:12]}: conflict in shared.txt")
    assert "no merge agent yet" in held["reason"] and (held["phase"], held["tip"]) == ("p2", new_tips["p2"]), held
    assert (held["cause"], held["attempts"]) == ("no_merge_agent", 1), "retried hourly until a merge agent is wired"
    assert not (sandbox.workspace / "work" / "fresh" / "p2" / "p2-rectify.md").exists(), "no rectify task on the new model"
    assert _origin_has(sandbox, fresh_origin, "refs/heads/epic/fresh/p2") == new_tips["p2"], "the lander never writes it"
    merged = _origin_has(sandbox, fresh_origin, "refs/heads/init/fresh")
    assert merged and _origin_git(sandbox, fresh_origin, "merge-base", "--is-ancestor", new_tips["p1"], merged).returncode == 0
    assert _origin_git(sandbox, fresh_origin, "merge-base", "--is-ancestor", new_tips["p2"], merged).returncode != 0
    # Old model, same pass: the conflict committed on the phase branch for a rectify task, which is filed.
    assert (sandbox.workspace / "work" / "legacy" / "p2" / "p2-rectify.md").is_file(), out
    assert "<<<<<<<" in _git(sandbox, legacy, "show", "epic/legacy/p2:shared.txt")
    assert "lane_model" not in _record(sandbox, "legacy") and not _record(sandbox, "legacy").get("held")
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.returncode == 0 and inbox.stdout.count("the lander could not merge new-model phase p2") == 1, inbox.stdout

    again = sandbox.cox("lander", "once")
    assert "phase_merge:fresh:p2: waiting: fresh is held" in again.stdout, again.stdout + again.stderr
    assert _record(sandbox, "fresh")["held"] == held, "a held merge is not tried again"
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.stdout.count("the lander could not merge new-model phase p2") == 1, "and the chair is told once"

    # An operator releases the hold (`cox lander release`): the next pass tries the merge again, and says how it went.
    refused = sandbox.cox("lander", "release", "legacy")
    assert refused.returncode == 1 and "legacy is not held" in refused.stdout, refused.stdout + refused.stderr
    released = sandbox.cox("lander", "release", "fresh")
    assert released.returncode == 0 and "the merge hold on phase p2 is released" in released.stdout, released.stdout + released.stderr
    retried = sandbox.cox("lander", "once")
    assert "an operator released it" in retried.stdout, retried.stdout + retried.stderr
    again_held = _record(sandbox, "fresh")["held"]
    assert (again_held["attempts"], again_held["tip"], again_held.get("released")) == (2, new_tips["p2"], None), again_held
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.stdout.count("the lander could not merge new-model phase p2") == 2, "the release's answer is told"
