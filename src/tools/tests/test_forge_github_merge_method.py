"""`forge_github.merge` honors an optional `method` on the step: squash by default, rebase to keep each source commit."""
from types import SimpleNamespace

from agent_tools import forge_github

STEP = {"kind": "merge", "branch": "pr/i--p", "default_branch": "main", "delete_branch": False}
MERGE = ["gh", "pr", "merge", "pr/i--p"]
DELETE = ["gh", "api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/heads/pr/i--p"]


def _gh(monkeypatch):
    """Record every argv run; each command exits 0 and prints nothing."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


def test_merge_flag_maps_each_method_to_its_flag():
    assert forge_github.merge_flag({}) == "--squash"
    assert forge_github.merge_flag({"method": "squash"}) == "--squash"
    assert forge_github.merge_flag({"method": "rebase"}) == "--rebase"
    assert forge_github.merge_flag({"method": "merge"}) is None
    assert forge_github.merge_flag({"method": ""}) is None


def test_a_step_without_method_squashes(monkeypatch, tmp_path):
    calls = _gh(monkeypatch)
    assert forge_github.merge(tmp_path, STEP)[0] is True
    assert calls[0] == [*MERGE, "--squash"]
    assert all("--rebase" not in argv for argv in calls)


def test_a_rebase_step_passes_the_rebase_flag(monkeypatch, tmp_path):
    calls = _gh(monkeypatch)
    assert forge_github.merge(tmp_path, {**STEP, "method": "rebase"})[0] is True
    assert calls[0] == [*MERGE, "--rebase"]
    assert all("--squash" not in argv for argv in calls)


def test_an_unknown_method_fails_without_running_anything(monkeypatch, tmp_path):
    calls = _gh(monkeypatch)
    ok, detail = forge_github.merge(tmp_path, {**STEP, "method": "bogus"})
    assert ok is False
    assert "bogus" in detail
    assert calls == []


def test_a_rebase_merge_still_deletes_the_remote_branch(monkeypatch, tmp_path):
    calls = _gh(monkeypatch)
    ok, detail = forge_github.merge(tmp_path, {**STEP, "method": "rebase", "delete_branch": True})
    assert ok is True
    assert DELETE in calls
    assert "remote branch pr/i--p deleted" in detail
