"""The ref health scan: literal cases for the pure core, one real directory for the edge."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_tools.git_ref_health import corrupt_refs, scan_repo_refs

SHA1 = "a" * 40
SHA256 = "b" * 64


def test_valid_object_ids_pass() -> None:
    assert corrupt_refs({".git/refs/heads/a": SHA1 + "\n", ".git/refs/heads/b": SHA256}) == []


def test_empty_and_whitespace_only_are_reported() -> None:
    assert corrupt_refs({".git/refs/heads/e": "", ".git/refs/heads/w": " \n\t"}) == [
        ".git/refs/heads/e",
        ".git/refs/heads/w",
    ]


def test_truncated_id_is_reported() -> None:
    assert corrupt_refs({".git/refs/heads/t": "a" * 39}) == [".git/refs/heads/t"]


def test_other_text_is_reported() -> None:
    assert corrupt_refs({".git/refs/heads/g": "z" * 40}) == [".git/refs/heads/g"]


def test_symbolic_ref_passes() -> None:
    assert corrupt_refs({".git/refs/remotes/origin/HEAD": "ref: refs/remotes/origin/main\n"}) == []


def test_result_is_sorted() -> None:
    assert corrupt_refs({"b": "", "a": "", "c": SHA1}) == ["a", "b"]


def test_edge_returns_exact_path_of_empty_ref(tmp_path: Path) -> None:
    heads = tmp_path / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    (heads / "good").write_text(SHA1 + "\n")
    (heads / "empty-branch").write_text("")
    assert scan_repo_refs(tmp_path) == [str(tmp_path / ".git/refs/heads/empty-branch")]


def test_edge_without_git_directory_yields_nothing(tmp_path: Path) -> None:
    assert scan_repo_refs(tmp_path) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files regardless of mode")
def test_edge_reports_unreadable_file(tmp_path: Path) -> None:
    heads = tmp_path / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    locked = heads / "locked"
    locked.write_text(SHA1)
    locked.chmod(0)
    assert scan_repo_refs(tmp_path) == [str(locked)]
