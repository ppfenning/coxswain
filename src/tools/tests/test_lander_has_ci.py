"""Whether a branch carries CI the forge could report on: a workflow file on the branch, read from git."""
import subprocess
from pathlib import Path

from agent_tools.lander_ports import Forge


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "builder@example.test")
    _git(repo, "config", "user.name", "builder")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


def test_a_branch_with_no_workflow_has_no_ci(tmp_path: Path) -> None:
    assert Forge(object()).has_ci(_repo(tmp_path), "main") is False


def test_a_branch_with_a_workflow_has_ci(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ci")
    assert Forge(object()).has_ci(repo, "main") is True


def test_a_ref_git_cannot_read_counts_as_having_ci(tmp_path: Path) -> None:
    assert Forge(object()).has_ci(_repo(tmp_path), "no-such-branch") is True
