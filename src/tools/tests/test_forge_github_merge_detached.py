"""`forge_github.merge` on a detached-HEAD repository: gh merges on the server, then its local cleanup exits non-zero.

2026-11-02: PRs 535, 536 and 537 merged, gh printed the detached-HEAD message and exited 1, and the land stopped.
"""
from types import SimpleNamespace

from agent_tools import cli, forge_github

DETACHED = "could not determine current branch: failed to run git: not on any branch"
MERGED = '{"mergedAt":"2026-11-02T04:12:00Z","state":"MERGED"}'
OPEN = '{"mergedAt":null,"state":"OPEN"}'
STEP = {"kind": "merge", "branch": "pr/i--p", "default_branch": "main", "squash": True, "delete_branch": True}
MERGE = ["gh", "pr", "merge", "pr/i--p", "--squash"]
VIEW = ["gh", "pr", "view", "pr/i--p", "--json", "state,mergedAt"]
LIST = ["gh", "pr", "list", "--head", "pr/i--p", "--state", "all", "--json", "number,state"]
DELETE = ["gh", "api", "-X", "DELETE", "repos/{owner}/{repo}/git/refs/heads/pr/i--p"]
NOT_ON_A_BRANCH = (128, "fatal: ref HEAD is not a symbolic ref")


def _gh(monkeypatch, answers):
    """Run each argv in `answers` (argv tuple to `(code, text)`) as gh would; every other command exits 0 and prints nothing."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        code, text = answers.get(tuple(argv), (0, ""))
        return SimpleNamespace(returncode=code, stdout="" if code else text, stderr=text if code else "")

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


def test_merge_verdict_prefers_the_server_state_over_the_exit_code():
    assert forge_github.merge_verdict(0, "merged", None) == (True, "merged")
    assert forge_github.merge_verdict(1, DETACHED, {"state": "merged"}) == (True, f"warning: {DETACHED}")
    assert forge_github.merge_verdict(1, DETACHED, {"state": "open"}) == (False, DETACHED)
    assert forge_github.merge_verdict(1, DETACHED, {"state": "unknown"}) == (False, DETACHED)
    assert forge_github.merge_verdict(1, "", {"state": "merged"}) == (True, "warning: gh exited non-zero after the PR merged")


def test_a_merged_pr_after_the_detached_head_exit_succeeds_with_the_warning(monkeypatch, tmp_path):
    calls = _gh(monkeypatch, {tuple(MERGE): (1, DETACHED), tuple(VIEW): (0, MERGED), tuple(DELETE): (0, "")})
    ok, detail = forge_github.merge(tmp_path, STEP)
    assert ok is True
    assert detail.splitlines()[:2] == [f"warning: {DETACHED}", "remote branch pr/i--p deleted"]
    assert calls[:3] == [MERGE, VIEW, DELETE]


def test_an_open_pr_after_a_failed_merge_fails_and_deletes_nothing(monkeypatch, tmp_path):
    calls = _gh(monkeypatch, {tuple(MERGE): (1, DETACHED), tuple(VIEW): (0, OPEN)})
    assert forge_github.merge(tmp_path, STEP) == (False, DETACHED)
    assert calls == [MERGE, VIEW]


def test_an_unreadable_pr_state_after_a_failed_merge_fails(monkeypatch, tmp_path):
    calls = _gh(monkeypatch, {tuple(MERGE): (1, DETACHED), tuple(VIEW): (1, "HTTP 502: Bad Gateway")})
    assert forge_github.merge(tmp_path, STEP) == (False, DETACHED)
    assert calls == [MERGE, VIEW, LIST]


def test_a_detached_head_repository_deletes_the_remote_head_branch_through_the_api(monkeypatch, tmp_path):
    calls = _gh(monkeypatch, {tuple(MERGE): (0, "merged"),
                              ("git", "-C", str(tmp_path), "symbolic-ref", "--short", "HEAD"): NOT_ON_A_BRANCH})
    ok, detail = forge_github.merge(tmp_path, STEP)
    assert (ok, detail) == (True, "merged\nremote branch pr/i--p deleted")
    assert calls.count(DELETE) == 1
    assert not any("--delete-branch" in c for c in calls)
    assert ["git", "-C", str(tmp_path), "branch", "-D", "pr/i--p"] in calls
    assert ["git", "-C", str(tmp_path), "fetch", "origin", "main:main"] in calls


def test_a_step_that_omits_delete_branch_deletes_as_the_cli_flag_always_did(monkeypatch, tmp_path):
    """The chair's carry merge passes only `branch` and `default_branch`."""
    calls = _gh(monkeypatch, {tuple(MERGE): (0, "merged")})
    step = {"branch": "pr/i--p", "default_branch": "main"}
    assert forge_github.merge(tmp_path, step)[1].splitlines()[:2] == ["merged", "remote branch pr/i--p deleted"]
    assert calls.count(DELETE) == 1


def test_a_step_that_sets_delete_branch_false_leaves_the_branches(monkeypatch, tmp_path):
    calls = _gh(monkeypatch, {tuple(MERGE): (0, "merged")})
    forge_github.merge(tmp_path, {**STEP, "delete_branch": False})
    assert DELETE not in calls and ["git", "-C", str(tmp_path), "branch", "-D", "pr/i--p"] not in calls


def test_a_merged_pr_the_forge_already_deleted_is_found_through_the_listing(monkeypatch, tmp_path):
    """gh pr view cannot resolve a head the repository auto-deleted; the listing of its PRs still reads merged."""
    _gh(monkeypatch, {tuple(MERGE): (1, DETACHED), tuple(VIEW): (1, "no pull requests found for branch \"pr/i--p\""),
                      tuple(LIST): (0, '[{"number":535,"state":"MERGED"}]'), tuple(DELETE): (1, "gh: Reference does not exist (HTTP 422)")})
    ok, detail = forge_github.merge(tmp_path, STEP)
    assert ok is True and detail.splitlines()[:2] == [f"warning: {DETACHED}", "remote branch pr/i--p already gone"]


def test_a_branch_the_forge_already_deleted_is_not_a_failure(monkeypatch, tmp_path):
    _gh(monkeypatch, {tuple(MERGE): (0, "merged"), tuple(DELETE): (1, "gh: Reference does not exist (HTTP 422)")})
    assert forge_github.merge(tmp_path, STEP)[1].splitlines()[:2] == ["merged", "remote branch pr/i--p already gone"]


def test_a_failed_branch_delete_is_a_warning_and_the_merge_stays_ok(monkeypatch, tmp_path):
    _gh(monkeypatch, {tuple(MERGE): (0, "merged"), tuple(DELETE): (1, "gh: Resource not accessible (HTTP 403)")})
    ok, detail = forge_github.merge(tmp_path, STEP)
    assert ok is True
    assert detail.splitlines()[1] == "warning: remote branch pr/i--p not deleted: gh: Resource not accessible (HTTP 403)"


def test_the_walk_continues_past_a_merge_that_exited_non_zero_on_a_merged_pr(monkeypatch, tmp_path, capsys):
    _gh(monkeypatch, {tuple(MERGE): (1, DETACHED), tuple(VIEW): (0, MERGED)})
    clean = {"kind": "clean", "run": "r", "task": "a", "branch": "agents/r/a", "worktree_root": str(tmp_path / "wt")}
    steps = [STEP, clean]
    rc, reached, _ = cli._land_walk(tmp_path, steps, steps, None, None, "full", False, forge_github, [])
    out = capsys.readouterr().out
    assert (rc, reached) == (0, ["merge", "clean"])
    assert f"merge: warning: {DETACHED}" in out and "stopped" not in out


def test_the_walk_stops_after_a_merge_that_exited_non_zero_on_an_open_pr(monkeypatch, tmp_path, capsys):
    _gh(monkeypatch, {tuple(MERGE): (1, DETACHED), tuple(VIEW): (0, OPEN)})
    clean = {"kind": "clean", "run": "r", "task": "a", "branch": "agents/r/a", "worktree_root": str(tmp_path / "wt")}
    steps = [STEP, clean]
    rc, reached, _ = cli._land_walk(tmp_path, steps, steps, None, None, "full", False, forge_github, [])
    assert (rc, reached) == (1, ["merge"])
    assert "stopped; remaining: clean" in capsys.readouterr().out
