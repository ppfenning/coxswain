"""The check ledger reuses a pass for the same tree and command. The runner is stubbed; git is real."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness import check_reuse
from harness.check_reuse import (
    LEDGER_NAME,
    MAX_ROWS,
    find_reusable,
    ledger_key,
    reused_result,
    run_checks_reusing,
    tree_of,
    trim,
)


def _row(tree="t1", cmd="pytest", outcome="passed", **extra):
    return {
        "tree": tree,
        "name": "tests",
        "cmd": cmd,
        "passed": outcome == "passed",
        "outcome": outcome,
        "exit_code": 0 if outcome == "passed" else 1,
        "counts": {"passed": 3},
        "cpu_seconds": 4.5,
        **extra,
    }


def test_key_equal_only_for_same_tree_and_command():
    assert ledger_key("t1", "pytest") == ledger_key("t1", "pytest")
    assert ledger_key("t1", "pytest") != ledger_key("t2", "pytest")
    assert ledger_key("t1", "pytest") != ledger_key("t1", "pytest ")


@pytest.mark.parametrize("outcome", ["failed", "timed_out", "unrunnable", "skipped"])
def test_unsuccessful_row_is_not_reused(outcome):
    assert find_reusable([_row(outcome=outcome)], "t1", "pytest") is None


def test_different_command_or_tree_misses():
    rows = [_row()]
    assert find_reusable(rows, "t1", "ruff check") is None
    assert find_reusable(rows, "t2", "pytest") is None


def test_newest_row_wins():
    old, new = _row(exit_code=0, marker="old"), _row(exit_code=0, marker="new")
    assert find_reusable([old, new], "t1", "pytest")["marker"] == "new"
    assert find_reusable([new, _row(outcome="failed")], "t1", "pytest")["marker"] == "new"


def test_reused_result_shape():
    row = _row()
    before = dict(row)
    out = reused_result(row)
    assert row == before
    assert out["reused"] is True
    assert out["cpu_seconds"] == 0.0
    assert out["cpu_seconds_saved"] == 4.5
    assert out["excerpt"].startswith("reused: same tree and command passed earlier")
    assert (out["name"], out["cmd"], out["passed"], out["outcome"], out["exit_code"], out["counts"]) == (
        "tests",
        "pytest",
        True,
        "passed",
        0,
        {"passed": 3},
    )


def test_trim_keeps_newest():
    assert trim([1, 2, 3, 4, 5], 2) == [4, 5]
    assert trim([1, 2], 0) == []


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "a@b.c")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def test_tree_of_tracks_tracked_and_untracked_not_ignored(repo):
    index_before = (repo / ".git" / "index").read_bytes()
    base = tree_of(repo)
    assert base and tree_of(repo) == base
    (repo / "ignored.txt").write_text("x\n")
    assert tree_of(repo) == base
    (repo / "a.txt").write_text("two\n")
    edited = tree_of(repo)
    assert edited != base
    (repo / "new.txt").write_text("n\n")
    assert tree_of(repo) != edited
    (repo / "a.txt").write_text("one\n")
    (repo / "new.txt").unlink()
    assert tree_of(repo) == base
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_tree_of_none_outside_git(tmp_path):
    assert tree_of(tmp_path / "nowhere") is None


class _Runner:
    def __init__(self, outcome="passed"):
        self.calls: list[list[str]] = []
        self.outcome = outcome

    def __call__(self, worktree, checks, **kwargs):
        self.calls.append([c["cmd"] for c in checks])
        ok = self.outcome == "passed"
        return [
            {
                "name": c["name"],
                "cmd": c["cmd"],
                "passed": ok,
                "outcome": self.outcome,
                "exit_code": 0 if ok else 1,
                "counts": {},
                "cpu_seconds": 2.0,
                "excerpt": "ran",
            }
            for c in checks
        ]


def _ledger(repo: Path) -> Path:
    return repo / ".git" / LEDGER_NAME


def test_miss_runs_and_appends_then_hit_skips_run(repo, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(check_reuse, "run_checks", runner)
    checks = [{"name": "tests", "cmd": "pytest"}]
    first = run_checks_reusing(repo, checks, ledger_dir=repo / ".git")
    assert runner.calls == [["pytest"]]
    assert first[0]["excerpt"] == "ran"
    lines = _ledger(repo).read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["cmd"] == "pytest"
    second = run_checks_reusing(repo, checks, ledger_dir=repo / ".git")
    assert runner.calls == [["pytest"]]
    assert second[0]["reused"] is True and second[0]["cpu_seconds_saved"] == 2.0


def test_changed_tree_misses(repo, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(check_reuse, "run_checks", runner)
    checks = [{"name": "tests", "cmd": "pytest"}]
    run_checks_reusing(repo, checks, ledger_dir=repo / ".git")
    (repo / "a.txt").write_text("changed\n")
    run_checks_reusing(repo, checks, ledger_dir=repo / ".git")
    assert runner.calls == [["pytest"], ["pytest"]]


def test_mixed_hit_and_miss_keep_order(repo, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(check_reuse, "run_checks", runner)
    run_checks_reusing(repo, [{"name": "b", "cmd": "ruff"}], ledger_dir=repo / ".git")
    checks = [{"name": "a", "cmd": "pytest"}, {"name": "b", "cmd": "ruff"}, {"name": "c", "cmd": "mypy"}]
    out = run_checks_reusing(repo, checks, ledger_dir=repo / ".git")
    assert runner.calls[-1] == ["pytest", "mypy"]
    assert [r["cmd"] for r in out] == ["pytest", "ruff", "mypy"]
    assert [bool(r.get("reused")) for r in out] == [False, True, False]


def test_failed_result_is_not_recorded(repo, monkeypatch):
    monkeypatch.setattr(check_reuse, "run_checks", _Runner("failed"))
    out = run_checks_reusing(repo, [{"name": "t", "cmd": "pytest"}], ledger_dir=repo / ".git")
    assert out[0]["passed"] is False
    assert not _ledger(repo).exists()


def test_ledger_trims_past_limit(repo, monkeypatch):
    monkeypatch.setattr(check_reuse, "run_checks", _Runner())
    old = "".join(json.dumps(_row(tree="old", cmd=f"c{i}")) + "\n" for i in range(MAX_ROWS))
    _ledger(repo).write_text(old)
    run_checks_reusing(repo, [{"name": "t", "cmd": "pytest"}], ledger_dir=repo / ".git")
    rows = [json.loads(line) for line in _ledger(repo).read_text().splitlines()]
    assert len(rows) == MAX_ROWS
    assert rows[-1]["cmd"] == "pytest" and rows[0]["cmd"] == "c1"


def test_git_failure_falls_back_to_plain_run(tmp_path, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(check_reuse, "run_checks", runner)
    checks = [{"name": "t", "cmd": "pytest"}]
    out = run_checks_reusing(tmp_path / "nowhere", checks, ledger_dir=tmp_path)
    assert runner.calls == [["pytest"]]
    assert out[0]["excerpt"] == "ran"
    assert not (tmp_path / LEDGER_NAME).exists()


def test_unreadable_ledger_falls_back_to_plain_run(repo, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(check_reuse, "run_checks", runner)
    _ledger(repo).mkdir()
    out = run_checks_reusing(repo, [{"name": "t", "cmd": "pytest"}], ledger_dir=repo / ".git")
    assert runner.calls == [["pytest"]]
    assert out[0]["excerpt"] == "ran"
