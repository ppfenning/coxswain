"""Draft open, update and ready on each forge: the argv or request sent, and the failure for a head with no open PR."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_tools import forge_auto, forge_forgejo, forge_github, forge_local

REPO = Path("/repo")
PULL_API = "/api/v1/repos/o/r/pulls"


def _fake_gh(monkeypatch, listing):
    """Record (argv, timeout, cwd) per gh call; `pr list` answers `listing`, everything else succeeds."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("timeout"), kwargs.get("cwd")))
        out = json.dumps(listing) if tuple(argv[1:3]) == ("pr", "list") else "https://github.com/o/r/pull/7\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


def _writes(calls):
    return [argv for argv, _, _ in calls if tuple(argv[1:3]) != ("pr", "list")]


def test_github_draft_open_adds_the_draft_flag(monkeypatch):
    calls = _fake_gh(monkeypatch, [])
    assert forge_github.open_pr(REPO, "T", "B", head="h", base="main", draft=True)[0]
    assert _writes(calls) == [["gh", "pr", "create", "--title", "T", "--body", "B", "--head", "h", "--base", "main", "--draft"]]


def test_github_open_without_draft_sends_the_old_argv(monkeypatch):
    calls = _fake_gh(monkeypatch, [])
    forge_github.open_pr(REPO, "T", "B", head="h", base="main")
    assert _writes(calls) == [["gh", "pr", "create", "--title", "T", "--body", "B", "--head", "h", "--base", "main"]]


def test_github_update_edits_the_pr_found_by_head(monkeypatch):
    calls = _fake_gh(monkeypatch, [{"number": 7}])
    ok, detail = forge_github.update_pr(REPO, "New", "Body", head="feat")
    assert (ok, detail) == (True, "https://github.com/o/r/pull/7")
    assert calls[0][0] == ["gh", "pr", "list", "--head", "feat", "--state", "open", "--json", "number"]
    assert _writes(calls) == [["gh", "pr", "edit", "7", "--title", "New", "--body", "Body"]]
    assert calls[1][1:] == (120, REPO)


def test_github_ready_marks_the_pr_found_by_head(monkeypatch):
    calls = _fake_gh(monkeypatch, [{"number": 7}])
    assert forge_github.mark_ready(REPO, head="feat")[0]
    assert _writes(calls) == [["gh", "pr", "ready", "7"]]


@pytest.mark.parametrize("call", [
    lambda: forge_github.update_pr(REPO, "T", "B", head="feat"),
    lambda: forge_github.mark_ready(REPO, head="feat"),
])
def test_github_no_open_pr_fails_naming_the_head_and_opens_nothing(monkeypatch, call):
    calls = _fake_gh(monkeypatch, [])
    ok, detail = call()
    assert (ok, "feat" in detail) == (False, True)
    assert _writes(calls) == []


class _Api:
    """Stand-in for `forge_forgejo.request`: records (method, path, body); `pulls` is the list of open pulls."""

    def __init__(self, pulls):
        self.pulls = pulls
        self.calls = []

    def __call__(self, settings, env, method, path, body=None):
        self.calls.append((method, path.split("?")[0], body))
        number = next((p["number"] for p in self.pulls if path.endswith(f"/pulls/{p['number']}")), None)
        if method == "GET" and path.startswith(PULL_API + "?"):
            return 200, self.pulls
        if method == "GET" and number:
            return 200, next(p for p in self.pulls if p["number"] == number)
        if method == "POST":
            return 201, {"html_url": "http://forge/o/r/pulls/9"}
        return 201, {"html_url": f"http://forge/o/r/pulls/{number}"}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "tok")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "http://forge.example/o/r.git"], check=True)
    return tmp_path


def _api(monkeypatch, pulls):
    api = _Api(pulls)
    monkeypatch.setattr(forge_forgejo, "request", api)
    return api


def _pull(number, title, head="feat"):
    return {"number": number, "title": title, "head": {"ref": head}}


def test_forgejo_draft_open_prefixes_the_title(monkeypatch, repo):
    api = _api(monkeypatch, [])
    assert forge_forgejo.open_pr(repo, "T", "B", head="h", base="main", draft=True)[0]
    assert api.writes() == [("POST", PULL_API, {"title": "WIP: T", "body": "B", "head": "h", "base": "main"})]


def test_forgejo_open_without_draft_sends_the_old_body(monkeypatch, repo):
    api = _api(monkeypatch, [])
    forge_forgejo.open_pr(repo, "T", "B", head="h", base="main")
    assert api.writes() == [("POST", PULL_API, {"title": "T", "body": "B", "head": "h", "base": "main"})]


def test_forgejo_update_patches_title_and_body(monkeypatch, repo):
    api = _api(monkeypatch, [_pull(4, "Old")])
    assert forge_forgejo.update_pr(repo, "New", "Body", head="feat") == (True, "http://forge/o/r/pulls/4")
    assert api.writes() == [("PATCH", f"{PULL_API}/4", {"title": "New", "body": "Body"})]


def test_forgejo_update_keeps_a_draft_a_draft(monkeypatch, repo):
    api = _api(monkeypatch, [_pull(4, "WIP: Old")])
    forge_forgejo.update_pr(repo, "New", "Body", head="feat")
    assert api.writes() == [("PATCH", f"{PULL_API}/4", {"title": "WIP: New", "body": "Body"})]


def test_forgejo_ready_drops_the_draft_prefix(monkeypatch, repo):
    api = _api(monkeypatch, [_pull(4, "WIP: Old")])
    assert forge_forgejo.mark_ready(repo, head="feat")[0]
    assert api.writes() == [("PATCH", f"{PULL_API}/4", {"title": "Old"})]


def test_forgejo_ready_on_a_ready_pr_sends_no_write(monkeypatch, repo):
    api = _api(monkeypatch, [_pull(4, "Old")])
    assert forge_forgejo.mark_ready(repo, head="feat")[0]
    assert api.writes() == []


@pytest.mark.parametrize("call", [
    lambda repo: forge_forgejo.update_pr(repo, "T", "B", head="feat"),
    lambda repo: forge_forgejo.mark_ready(repo, head="feat"),
])
def test_forgejo_no_open_pr_fails_naming_the_head_and_opens_nothing(monkeypatch, repo, call):
    api = _api(monkeypatch, [_pull(5, "Other", head="other")])
    assert call(repo) == (False, "no open pull request for feat")
    assert api.writes() == []


def test_local_accepts_draft_and_answers_one_line():
    assert forge_local.open_pr(REPO, "T", "B", head="h", base="main", draft=True) == forge_local.open_pr(REPO, "T", "B", head="h", base="main")
    for ok, detail in (forge_local.update_pr(REPO, "T", "B", head="h"), forge_local.mark_ready(REPO, head="h")):
        assert ok is True and "\n" not in detail


def test_auto_forwards_draft_and_the_new_verbs(monkeypatch):
    seen = []

    class Stub:
        def open_pr(self, repo, title, body, **kw):
            seen.append(("open", kw))
            return True, "u"

        def update_pr(self, repo, title, body, **kw):
            seen.append(("update", title, body, kw))
            return True, "u"

        def mark_ready(self, repo, **kw):
            seen.append(("ready", kw))
            return True, "u"

    monkeypatch.setattr(forge_auto, "forge_of", lambda repo: Stub())
    forge_auto.open_pr(REPO, "T", "B", head="h", base="main", draft=True)
    forge_auto.open_pr(REPO, "T", "B", head="h")
    forge_auto.update_pr(REPO, "T", "B", head="h")
    forge_auto.mark_ready(REPO, head="h")
    assert seen == [
        ("open", {"head": "h", "base": "main", "draft": True}),
        ("open", {"head": "h", "base": None}),
        ("update", "T", "B", {"head": "h"}),
        ("ready", {"head": "h"}),
    ]
