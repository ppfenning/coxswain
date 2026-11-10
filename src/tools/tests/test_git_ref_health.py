"""The ref health scan: a pure core on literal mappings, and the edge on a throwaway repository directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_tools.git_ref_health import corrupt_refs, scan_repo

SHA1 = "a" * 40
SHA256 = "0123456789abcdef" * 4


def test_valid_ids_pass() -> None:
    assert corrupt_refs({".git/refs/heads/a": SHA1 + "\n", ".git/refs/heads/b": SHA256}) == []


def test_empty_file_is_reported_and_sorted() -> None:
    refs = {".git/refs/heads/epic/x/p1": "", ".git/refs/heads/a": " \n", ".git/refs/heads/ok": SHA1}
    assert corrupt_refs(refs) == [".git/refs/heads/a", ".git/refs/heads/epic/x/p1"]


def test_truncated_id_is_reported() -> None:
    assert corrupt_refs({".git/refs/heads/x": SHA1[:39] + "\n"}) == [".git/refs/heads/x"]


def test_symbolic_ref_passes() -> None:
    assert corrupt_refs({".git/refs/remotes/origin/HEAD": "ref: refs/remotes/origin/main\n"}) == []


def test_symbolic_ref_without_a_target_is_reported() -> None:
    refs = {".git/refs/heads/a": "ref:", ".git/refs/heads/b": "ref: \n", ".git/refs/heads/c": "ref: x\ny"}
    assert corrupt_refs(refs) == [".git/refs/heads/a", ".git/refs/heads/b", ".git/refs/heads/c"]


def test_edge_returns_a_path_that_removes_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heads = tmp_path / ".git" / "refs" / "heads"
    (heads / "epic" / "x").mkdir(parents=True)
    (heads / "main").write_text(SHA1 + "\n")
    (heads / "epic" / "x" / "p1").write_text("")
    found = scan_repo(tmp_path)
    assert found == [".git/refs/heads/epic/x/p1"]
    monkeypatch.chdir(tmp_path)
    Path(found[0]).unlink()
    assert scan_repo(tmp_path) == []


def test_edge_without_git_directory_returns_nothing(tmp_path: Path) -> None:
    assert scan_repo(tmp_path) == []


def test_edge_reports_files_that_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heads = tmp_path / ".git" / "refs" / "heads"
    heads.mkdir(parents=True)
    (heads / "locked").write_text(SHA1)
    (heads / "binary").write_bytes(b"\xff\xfe\x00")
    (heads / "main").write_text(SHA1)
    real = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "locked":
            raise PermissionError(13, "denied")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)
    assert scan_repo(tmp_path) == [".git/refs/heads/binary", ".git/refs/heads/locked"]


def test_edge_skips_a_directory_it_cannot_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heads = tmp_path / ".git" / "refs" / "heads"
    (heads / "locked").mkdir(parents=True)
    (heads / "locked" / "ok").write_text(SHA1)
    (heads / "empty").write_text("")
    real = Path.iterdir

    def iterdir(self: Path):
        if self.name == "locked":
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert scan_repo(tmp_path) == [".git/refs/heads/empty"]
