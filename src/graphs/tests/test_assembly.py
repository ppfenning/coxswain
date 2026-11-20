"""Local assembly merges parent branches in sorted order and leaves a clean worktree on conflict."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.assembly import (
    ABORT,
    COMMIT_RESOLUTION,
    AssemblyResult,
    assemble,
    assembly_order,
    conflict_step,
    failure_cause,
    git,
)

BASE_TEXT = "".join(f"line {n}\n" for n in range(1, 21))


def run(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def branch_with(repo: Path, name: str, files: dict[str, str]) -> None:
    run(repo, "checkout", "-q", "-b", name, "main")
    for path, text in files.items():
        (repo / path).write_text(text)
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", name)
    run(repo, "checkout", "-q", "main")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    run(path, "init", "-q", "-b", "main")
    run(path, "config", "user.name", "t")
    run(path, "config", "user.email", "t@example.com")
    (path / "shared.txt").write_text(BASE_TEXT)
    run(path, "add", "-A")
    run(path, "commit", "-q", "-m", "base")
    return path


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    run(repo, "worktree", "add", "-q", "--detach", str(path), "main")
    return path


def test_assembly_order_sorts_and_drops_duplicates() -> None:
    assert assembly_order(["b", "a", "b", "c"]) == ("a", "b", "c")
    assert assembly_order(["task/z", "task/a"]) == ("task/a", "task/z")


def test_two_clean_parents_merge_in_sorted_order(repo: Path, worktree: Path) -> None:
    branch_with(repo, "p-b", {"b.txt": "b\n"})
    branch_with(repo, "p-a", {"a.txt": "a\n"})
    result = assemble(repo, worktree, "main", ["p-b", "p-a"], "assembled")
    assert result.ok and result.cause == ""
    assert result.head == run(repo, "rev-parse", "assembled")
    assert (worktree / "a.txt").exists() and (worktree / "b.txt").exists()
    subjects = run(repo, "log", "--merges", "--format=%s", "assembled").splitlines()
    assert subjects == ["Merge branch 'p-b' into assembled", "Merge branch 'p-a' into assembled"]


def test_a_conflict_is_aborted_and_only_an_unfinished_rerere_resolution_is_committed() -> None:
    assert conflict_step(["shared.txt"], merging=True) == ABORT
    assert conflict_step(["shared.txt"], merging=False) == ABORT
    assert conflict_step([], merging=False) == ABORT  # git refused before merging: nothing to commit
    assert conflict_step([], merging=True) == COMMIT_RESOLUTION


def test_the_failure_cause_names_parent_previous_and_the_sorted_files_or_gits_own_text() -> None:
    assert failure_cause("p-b", "p-a", ["z.txt", "a.txt"]) == "merging p-b after p-a conflicted in: a.txt, z.txt"
    assert failure_cause("p-a", "main", [], output="fatal: refusing") == "merging p-a after main conflicted in: fatal: refusing"
    assert failure_cause("p-a", "main", []) == "merging p-a after main conflicted in: none reported"
    assert failure_cause("p-a", "main", ["f"], rerere_off=True).endswith(
        "conflicted in: f (rerere resolution could not be applied; conflict found with rerere off)"
    )


def test_conflict_names_parent_previous_and_files_and_leaves_worktree_clean(repo: Path, worktree: Path) -> None:
    """The one real-git boundary test of a conflict: `diff --diff-filter=U` names the file, and `merge --abort` leaves
    the worktree clean at the last good merge. The cause and the abort decision are pinned above as pure functions."""
    branch_with(repo, "p-a", {"shared.txt": BASE_TEXT.replace("line 5\n", "from a\n")})
    branch_with(repo, "p-b", {"shared.txt": BASE_TEXT.replace("line 5\n", "from b\n")})
    result = assemble(repo, worktree, "main", ["p-b", "p-a"], "assembled")
    assert result == AssemblyResult(False, "", "merging p-b after p-a conflicted in: shared.txt")
    assert git(worktree, "status", "--porcelain") == (0, "")
    assert (worktree / "shared.txt").read_text() == BASE_TEXT.replace("line 5\n", "from a\n")


def test_conflict_on_first_parent_names_the_base_ref(repo: Path, worktree: Path) -> None:
    branch_with(repo, "p-a", {"shared.txt": BASE_TEXT.replace("line 5\n", "from a\n")})
    run(repo, "checkout", "-q", "-b", "moved", "main")
    (repo / "shared.txt").write_text(BASE_TEXT.replace("line 5\n", "moved\n"))
    run(repo, "commit", "-qam", "moved")
    result = assemble(repo, worktree, "moved", ["p-a"], "assembled")
    assert result.cause == "merging p-a after moved conflicted in: shared.txt"


def test_a_parent_already_contained_in_the_target_is_skipped(repo: Path, worktree: Path) -> None:
    branch_with(repo, "p-b", {"b.txt": "b\n"})
    run(repo, "checkout", "-q", "-b", "p-a", "p-b")
    (repo / "a.txt").write_text("a\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "p-a")
    run(repo, "checkout", "-q", "main")
    result = assemble(repo, worktree, "main", ["p-b", "p-a"], "assembled")
    assert result.ok
    assert run(repo, "log", "--merges", "--format=%s", "assembled").splitlines() == [
        "Merge branch 'p-a' into assembled"
    ]


def test_unknown_base_ref_fails_without_raising(repo: Path, worktree: Path) -> None:
    result = assemble(repo, worktree, "no-such-ref", ["p-a"], "assembled")
    assert not result.ok and result.head == "" and "no-such-ref" in result.cause


RESOLVED = BASE_TEXT.replace("line 5\n", "resolved\n")


def record_resolution(repo: Path) -> None:
    """Resolve p-a against p-b by hand once, with rerere on, so git records the resolution."""
    run(repo, "config", "rerere.enabled", "true")
    run(repo, "config", "rerere.autoupdate", "true")
    branch_with(repo, "p-a", {"shared.txt": BASE_TEXT.replace("line 5\n", "from a\n")})
    branch_with(repo, "p-b", {"shared.txt": BASE_TEXT.replace("line 5\n", "from b\n")})
    run(repo, "checkout", "-q", "-b", "trial", "p-a")
    assert git(repo, "merge", "p-b")[0] != 0
    (repo / "shared.txt").write_text(RESOLVED)
    run(repo, "add", "shared.txt")
    assert git(repo, "commit", "--no-edit")[0] == 0
    run(repo, "checkout", "-q", "main")
    run(repo, "branch", "-q", "-D", "trial")


def test_a_merge_rerere_resolved_and_left_unfinished_is_committed(repo: Path, worktree: Path) -> None:
    record_resolution(repo)
    result = assemble(repo, worktree, "main", ["p-a", "p-b"], "assembled")
    assert result.ok and result.cause == ""
    assert (worktree / "shared.txt").read_text() == RESOLVED
    assert run(repo, "log", "-n1", "--format=%s", "assembled") == "Merge branch 'p-b' into assembled"
    assert git(worktree, "status", "--porcelain") == (0, "")


def test_a_recorded_resolution_that_cannot_finish_the_merge_names_the_real_files(repo: Path, worktree: Path) -> None:
    record_resolution(repo)
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    # `git commit` concluding a merge runs pre-commit; `git merge` itself runs pre-merge-commit.
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    run(repo, "config", "core.hooksPath", str(hooks))
    result = assemble(repo, worktree, "main", ["p-a", "p-b"], "assembled")
    assert result.cause == (
        "merging p-b after p-a conflicted in: shared.txt "
        "(rerere resolution could not be applied; conflict found with rerere off)"
    )
    assert git(worktree, "status", "--porcelain") == (0, "")
