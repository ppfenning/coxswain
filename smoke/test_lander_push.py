"""2026-11-20: the lander is the one merger of an initiative branch, and origin's copy is the one every host reads.

Real `cox lander adopt --apply` and `cox lander once` against a real clone of a real bare origin and the store, with
the local forge (which never pushes an initiative branch itself, so every push seen on origin here is the lander's own
`git push`). Each test is one of the day's three failures:

- **No push after merge.** A phase merged into `init/<initiative>` on the lander's machine and was never pushed, so a
  lane host building the next phase could not see it. The merge is now on origin before the record says merged.
- **No re-merge of a moved phase.** A phase recorded merged was rebuilt, and its branch on origin then carried the real
  change. The lander merges it again, and the initiative branch on origin holds it.
- **Two "merge phase" commits.** The lander's copy of the branch held its own merge commit of a phase beside origin's.
  With the same tree it is reset to origin's and the next phase merges; with a different tree the initiative is held,
  and the chair is told once, naming both commits.
"""

from __future__ import annotations

from pathlib import Path

from smoke import plan
from smoke.test_lander_lanes import _git, _in_flight, _origin_git, _profile_with_cartridge, _record, _repository


def _origin_tip(sandbox, origin: Path, branch: str) -> str:
    done = _origin_git(sandbox, origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return done.stdout.strip() if done.returncode == 0 else ""


def _holds(sandbox, origin: Path, sha: str, branch: str) -> bool:
    return _origin_git(sandbox, origin, "merge-base", "--is-ancestor", sha, f"refs/heads/{branch}").returncode == 0


def test_a_phase_the_lander_merges_is_on_origin_before_it_is_recorded_merged(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path, gate="ticket")
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")

    tip = _in_flight(sandbox, repo, "app", "ship")  # `cox lander adopt --apply`, which merges p1

    head = _origin_tip(sandbox, origin, "init/ship")
    assert head, "the lander pushed init/ship to origin"
    assert _record(sandbox, "ship")["phases"]["p1"]["merge_sha"] == head, "the recorded merge is origin's tip"
    assert _holds(sandbox, origin, tip, "init/ship")


def test_a_phase_rebuilt_after_its_merge_is_merged_again_and_the_initiative_branch_carries_it(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path, gate="ticket")
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")
    first = _in_flight(sandbox, repo, "app", "ship")
    opened = sandbox.cox("lander", "once")  # the ticket gate stops the land at its pull request
    assert opened.returncode == 0 and "opened the pull request for ship" in opened.stdout, opened.stdout + opened.stderr
    # The phase is built again on a lane host, which pushes its phase branch; the record still says merged at `first`.
    _git(sandbox, repo, "checkout", "--quiet", "epic/ship/p1")
    (repo / "rebuilt.txt").write_text("the real change\n", encoding="utf-8")
    _git(sandbox, repo, "add", "rebuilt.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", "ship: the rebuilt feature")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    _git(sandbox, repo, "push", "--quiet", "origin", "epic/ship/p1")
    rebuilt = _git(sandbox, repo, "rev-parse", "epic/ship/p1")
    assert rebuilt != first and not _holds(sandbox, origin, rebuilt, "init/ship")

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"its branch on origin is at {rebuilt[:12]}" in done.stdout and "is merged again" in done.stdout, done.stdout
    head = _origin_tip(sandbox, origin, "init/ship")
    assert _holds(sandbox, origin, rebuilt, "init/ship")
    changed = _origin_git(sandbox, origin, "diff", "--name-only", "main", "refs/heads/init/ship").stdout.split()
    assert "rebuilt.txt" in changed, "the pull request's diff carries the rebuilt phase"
    entry = _record(sandbox, "ship")["phases"]["p1"]
    assert (entry["tip"], entry["merge_sha"]) == (rebuilt, head)
    again = sandbox.cox("lander", "once")
    assert again.returncode == 0 and "is merged again" not in again.stdout, again.stdout
    assert _origin_tip(sandbox, origin, "init/ship") == head, "merged once"


def _two_phases(sandbox, tmp_path) -> tuple[Path, Path]:
    """`ship` with p1 built and merged by `cox lander adopt --apply`, and p2 (which needs p1) not built yet."""
    _profile_with_cartridge(sandbox, tmp_path, gate="ticket")
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")
    _git(sandbox, repo, "checkout", "--quiet", "-b", "epic/ship/p1", "main")
    (repo / "p1.txt").write_text("p1\n", encoding="utf-8")
    _git(sandbox, repo, "add", "p1.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", "ship: p1")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    _git(sandbox, repo, "push", "--quiet", "origin", "epic/ship/p1")
    _task_files(sandbox, p1=_git(sandbox, repo, "rev-parse", "epic/ship/p1"))
    adopted = sandbox.cox("lander", "adopt", "--apply", "ship")
    assert adopted.returncode == 0 and "phase p1 of ship merged into init/ship" in adopted.stdout, adopted.stdout + adopted.stderr
    assert _origin_tip(sandbox, origin, "init/ship")
    return repo, origin


def _task_files(sandbox, *, p1: str, p2: str = "") -> None:
    sandbox.write("work/ship/initiative.md",
                  plan.work_item_text({"title": "ship", "repo": "~/src/app", "draft": "false"}, "An initiative."))
    sandbox.write("work/ship/p1/ship-t1.md", plan.work_item_text(
        {"id": "ship-t1", "title": "Phase one", "phase": "p1", "state": "approved", "merged_commit": p1}, "Task body"))
    sandbox.write("work/ship/p2/ship-t2.md", plan.work_item_text(
        {"id": "ship-t2", "title": "Phase two", "phase": "p2", "state": "approved" if p2 else "ready", "needs": "[ship-t1]",
         **({"merged_commit": p2} if p2 else {})}, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _merge_p1_again_here(sandbox, repo: Path, origin: Path, *, other_work: bool) -> tuple[str, str]:
    """This clone's `init/ship` gets its own "merge phase p1" commit on the base origin's was made on, as a lane host
    once made one: the same tree as origin's, or a different one. (this clone's tip, origin's)."""
    published = _origin_tip(sandbox, origin, "init/ship")
    _git(sandbox, repo, "fetch", "--quiet", "origin")
    _git(sandbox, repo, "checkout", "--quiet", "-B", "init/ship", f"{published}^1")
    _git(sandbox, repo, "merge", "--quiet", "--no-ff", "-m", "ship: merge phase p1 (on lane-host)", "epic/ship/p1")
    if other_work:
        (repo / "lane-only.txt").write_text("only on lane-host\n", encoding="utf-8")
        _git(sandbox, repo, "add", "lane-only.txt")
        _git(sandbox, repo, "commit", "--quiet", "-m", "ship: work only lane-host has")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    return _git(sandbox, repo, "rev-parse", "init/ship"), published


def _build_p2(sandbox, repo: Path, origin: Path) -> None:
    """p2 built on a lane host from origin's `init/ship`, its branch pushed, and its task approved."""
    _git(sandbox, repo, "branch", "--force", "epic/ship/p2", "origin/init/ship")
    _git(sandbox, repo, "checkout", "--quiet", "epic/ship/p2")
    (repo / "p2.txt").write_text("p2\n", encoding="utf-8")
    _git(sandbox, repo, "add", "p2.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", "ship: p2")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    _git(sandbox, repo, "push", "--quiet", "origin", "epic/ship/p2")
    _task_files(sandbox, p1=_git(sandbox, repo, "rev-parse", "epic/ship/p1"), p2=_git(sandbox, repo, "rev-parse", "epic/ship/p2"))


def test_a_second_merge_commit_with_the_same_tree_is_reset_to_origins_and_the_next_phase_merges(sandbox, tmp_path):
    repo, origin = _two_phases(sandbox, tmp_path)
    local, published = _merge_p1_again_here(sandbox, repo, origin, other_work=False)
    assert local != published
    _build_p2(sandbox, repo, origin)

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"this clone's {local[:12]} held the same tree as origin's {published[:12]}; reset to origin's" in done.stdout, done.stdout
    assert "phase p2 of ship merged into init/ship" in done.stdout, done.stdout
    head = _origin_tip(sandbox, origin, "init/ship")
    assert _holds(sandbox, origin, published, "init/ship") and _record(sandbox, "ship")["phases"]["p2"]["merge_sha"] == head
    subjects = _origin_git(sandbox, origin, "log", "--format=%s", "refs/heads/init/ship").stdout.splitlines()
    assert sum("merge phase p1" in s for s in subjects) == 1, subjects
    assert _git(sandbox, repo, "rev-parse", "init/ship") == head


def test_a_second_merge_commit_with_other_work_holds_the_initiative_and_names_both_commits(sandbox, tmp_path):
    repo, origin = _two_phases(sandbox, tmp_path)
    local, published = _merge_p1_again_here(sandbox, repo, origin, other_work=True)
    _build_p2(sandbox, repo, origin)

    done = sandbox.cox("lander", "once")
    again = sandbox.cox("lander", "once")

    assert done.returncode == 0 and again.returncode == 0, done.stdout + done.stderr + again.stdout + again.stderr
    assert "needs the chair: init/ship has diverged from origin with different content" in done.stdout, done.stdout
    held = str((_record(sandbox, "ship").get("held") or {}).get("reason") or "")
    assert local[:12] in held and published[:12] in held, held
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.returncode == 0 and inbox.stdout.count(f"this clone's {local[:12]}") == 1, inbox.stdout + inbox.stderr
    assert _origin_tip(sandbox, origin, "init/ship") == published and _git(sandbox, repo, "rev-parse", "init/ship") == local
