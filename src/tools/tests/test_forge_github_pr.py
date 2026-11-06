"""Pull request readers by number: parsers on literal JSON, and the gh calls of the edge."""

from __future__ import annotations

import json
import subprocess

import pytest

from agent_tools import forge_github
from agent_tools.forge import ForgeError

PR = {"number": 7, "state": "OPEN", "url": "https://github.com/o/r/pull/7", "headRefName": "feat",
      "headRefOid": "abc123", "baseRefName": "main", "body": "text"}


def test_pr_view_argv():
    assert forge_github.pr_view_argv(7) == ["gh", "pr", "view", "7", "--json", "number,state,url,headRefName,headRefOid,baseRefName,body"]


def test_parse_pr_view_valid():
    assert forge_github.parse_pr_view(json.dumps({**PR, "body": ""})) == {**PR, "body": ""}


@pytest.mark.parametrize("text", [
    json.dumps({k: v for k, v in PR.items() if k != "headRefOid"}),
    json.dumps({**PR, "baseRefName": None}),
    json.dumps({**PR, "headRefOid": ""}),
    json.dumps({**PR, "number": "7"}),
    json.dumps({**PR, "body": None}),
    "", "no pull request found", "[1, 2]", "null",
])
def test_parse_pr_view_unreadable(text):
    assert forge_github.parse_pr_view(text) is None


def test_read_pr(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(forge_github, "_gh_text", lambda argv, repo, *, timeout: seen.append(argv) or json.dumps(PR))
    assert forge_github.read_pr(tmp_path, 7) == PR
    assert seen == [forge_github.pr_view_argv(7)]
    monkeypatch.setattr(forge_github, "_gh_text", lambda argv, repo, *, timeout: None)
    assert forge_github.read_pr(tmp_path, 7) is None


@pytest.mark.parametrize("pr, names, expected", [
    (PR, ("ci", "lint"), ("ci", "lint")),
    (PR, (), ()),
    (PR, None, None),
    (None, ("ci",), None),
])
def test_required_for_pr(monkeypatch, tmp_path, pr, names, expected):
    seen = []
    monkeypatch.setattr(forge_github, "read_pr", lambda repo, number: pr)
    monkeypatch.setattr(forge_github, "read_required_for_base", lambda repo, base: seen.append(base) or names)
    result = forge_github.required_for_pr(tmp_path, 7)
    assert result == expected and (result is None) == (expected is None)
    assert seen == (["main"] if pr else [])


RUNS = json.dumps({"total_count": 6, "check_runs": [
    {"name": "build", "status": "completed", "conclusion": "success", "html_url": "u/build"},
    {"name": "lint", "status": "completed", "conclusion": "failure", "html_url": "u/lint"},
    {"name": "deploy", "status": "queued", "conclusion": None, "html_url": "u/deploy"},
    {"name": "docs", "status": "completed", "conclusion": "skipped", "html_url": "u/docs"},
    {"name": "boot", "status": "completed", "conclusion": "startup_failure", "html_url": "u/boot"},
    {"name": "odd", "status": "completed", "conclusion": "brand_new", "html_url": "u/odd"},
]})
STATUSES = json.dumps({"state": "failure", "total_count": 3, "statuses": [
    {"context": "legacy-ok", "state": "success", "target_url": "t/ok"},
    {"context": "legacy-bad", "state": "error", "target_url": "t/bad"},
    {"context": "legacy-wait", "state": "pending", "target_url": None},
]})


def test_parse_check_rows_maps_the_four_states():
    rows = forge_github.parse_check_rows(json.loads(RUNS), json.loads(STATUSES))
    assert {r["name"]: r["state"] for r in rows} == {
        "build": "success", "lint": "failure", "deploy": "pending", "docs": "skipped", "boot": "failure",
        "odd": "pending", "legacy-ok": "success", "legacy-bad": "failure", "legacy-wait": "pending"}
    assert rows[0] == {"name": "build", "state": "success", "url": "u/build"}
    assert rows[-1] == {"name": "legacy-wait", "state": "pending", "url": None}


@pytest.mark.parametrize("runs, statuses", [
    ({}, {"statuses": []}),
    ({"check_runs": []}, []),
    ({"check_runs": [{"status": "completed", "conclusion": "success"}]}, {"statuses": []}),
    ({"check_runs": []}, {"statuses": [{"state": "success"}]}),
    ({"check_runs": ["x"]}, {"statuses": []}),
    ({"total_count": 3, "check_runs": [{"name": "a"}, {"name": "b"}]}, {"statuses": []}),
    ({"check_runs": []}, {"total_count": 1, "statuses": []}),
])
def test_parse_check_rows_unreadable(runs, statuses):
    assert forge_github.parse_check_rows(runs, statuses) is None


def _api(monkeypatch, runs, statuses):
    seen = []

    def fake(argv, repo, *, timeout):
        seen.append(argv)
        return runs if "check-runs" in argv[-1] else statuses

    monkeypatch.setattr(forge_github, "_gh_text", fake)
    return seen


def test_pr_check_rows_reads_both_endpoints_and_every_page(monkeypatch, tmp_path):
    def page(n):
        return json.dumps({"check_runs": [{"name": n, "status": "completed", "conclusion": "success", "html_url": "u"}]})

    seen = _api(monkeypatch, page("one") + page("two"), json.dumps({"statuses": []}))
    assert [r["name"] for r in forge_github.pr_check_rows(tmp_path, "abc")] == ["one", "two"]
    assert [a[-1] for a in seen] == ["repos/{owner}/{repo}/commits/abc/check-runs?per_page=100",
                                     "repos/{owner}/{repo}/commits/abc/status?per_page=100"]
    assert all(a[:3] == ["gh", "api", "--paginate"] for a in seen)


def test_pr_check_rows_none_exist_is_empty_not_none(monkeypatch, tmp_path):
    _api(monkeypatch, json.dumps({"check_runs": []}), json.dumps({"statuses": []}))
    assert forge_github.pr_check_rows(tmp_path, "abc") == ()


@pytest.mark.parametrize("runs, statuses", [(None, STATUSES), (RUNS, None), ("not json", STATUSES), (RUNS, ""), ("[]", STATUSES)])
def test_pr_check_rows_unreadable(monkeypatch, tmp_path, runs, statuses):
    _api(monkeypatch, runs, statuses)
    assert forge_github.pr_check_rows(tmp_path, "abc") is None


def test_squash_merge_argv():
    argv = forge_github.squash_merge_argv(7)
    assert argv[:4] == ["gh", "pr", "merge", "7"]
    assert "--squash" in argv and "--delete-branch" in argv


def _merge(monkeypatch, returncode, *, stdout="", stderr="", pr_state=None, error=None):
    def run(argv, *, timeout, cwd=None):
        if error:
            raise error
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(forge_github, "_run", run)
    monkeypatch.setattr(forge_github, "read_pr", lambda repo, number: None if pr_state is None else {**PR, "state": pr_state})


def test_merge_pr_merged(monkeypatch, tmp_path):
    _merge(monkeypatch, 0, stdout="merged\n")
    assert forge_github.merge_pr(tmp_path, 7) == (True, "merged")


def test_merge_pr_confirmation_on_stderr(monkeypatch, tmp_path):
    _merge(monkeypatch, 0, stderr="Merged pull request #7\n")
    assert forge_github.merge_pr(tmp_path, 7) == (True, "Merged pull request #7")


def test_merge_pr_refused_carries_the_forge_reason(monkeypatch, tmp_path):
    reason = "Pull request is not mergeable: the base branch policy prohibits the merge"
    _merge(monkeypatch, 1, stderr=reason + "\n", pr_state="OPEN")
    assert forge_github.merge_pr(tmp_path, 7) == (False, reason)


def test_merge_pr_landed_but_branch_cleanup_failed(monkeypatch, tmp_path):
    _merge(monkeypatch, 1, stderr="failed to delete local branch: could not determine current branch\n", pr_state="MERGED")
    ok, detail = forge_github.merge_pr(tmp_path, 7)
    assert ok is True and detail == "warning: failed to delete local branch: could not determine current branch"


def test_merge_pr_failed_and_state_unreadable(monkeypatch, tmp_path):
    _merge(monkeypatch, 1, stderr="boom\n")
    assert forge_github.merge_pr(tmp_path, 7) == (False, "boom")


def test_merge_pr_cannot_run(monkeypatch, tmp_path):
    _merge(monkeypatch, 0, error=ForgeError("gh timed out"))
    assert forge_github.merge_pr(tmp_path, 7) == (False, "gh timed out")
