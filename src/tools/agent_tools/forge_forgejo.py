"""The forgejo forge: the Forgejo REST API through `agent_tools.forgejo_api`, and `git push`. The protocol is in `agent_tools.forge`.

The protocol functions take only `repo`, so the project comes from the repo's `origin`, an http(s) or ssh URL, and the token
from the `FORGEJO_TOKEN` variable. The server comes from an http(s) origin, or from `FORGEJO_BASE_URL` for an ssh one. The profile's `forgejo_*` keys do not reach this module.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from agent_tools import bounded_run, forge_github, land
from agent_tools.forge import ForgeError
from agent_tools.forge_github import _update_local_default
from agent_tools.forgejo_api import (
    TOKEN_ENV_KEY,
    URL_KEY,
    ForgejoError,
    ForgejoSettings,
    read_settings,
    request,
)

__all__ = ["find_open_prs", "green_commits", "merge", "merge_state", "open_pr", "poll_checks", "push", "update_branch", "wait_checks"]

DEFAULT_TOKEN_ENV = "FORGEJO_TOKEN"
# Where an ssh origin finds its API address: the origin names a host, not an http(s) server.
BASE_URL_ENV = "FORGEJO_BASE_URL"
# The scp-like ssh form, `git@host:owner/name.git`; a `scheme://` URL never matches because `:` ends the host.
_SCP_REMOTE = re.compile(r"^(?P<user>[^@/:\s]+)@(?P<host>[^:/\s]+):(?P<path>.+)$")
# What forge_github's merge_state says for a PR that can and cannot merge; Forgejo has no finer status.
MERGEABLE, CONFLICTED, UNSETTLED = "CLEAN", "DIRTY", "UNKNOWN"
# Forgejo marks a pull request as a work in progress by a title prefix; removing it makes the PR ready.
DRAFT_PREFIX = "WIP: "
PAGE_SIZE = 50
# A guard on the page loop: a server that ignores `page` would otherwise repeat page one forever.
MAX_PAGES = 20
# How long a push waits for the server to show the pushed branch, and how often it asks.
PUSH_SETTLE_S, PUSH_POLL_S = 30.0, 0.25


@dataclass(frozen=True)
class Target:
    settings: ForgejoSettings
    owner: str
    name: str

    def path(self, tail: str) -> str:
        return f"/api/v1/repos/{quote(self.owner, safe='')}/{quote(self.name, safe='')}{tail}"


def _message(status: int, body: object) -> str:
    text = body.get("message") if isinstance(body, dict) else None
    return text if isinstance(text, str) and text else f"HTTP {status}"


def parse_remote(url: str) -> tuple[str, str, str] | None:
    """`(base_url, owner, name)` of an http(s) or ssh remote URL, or None for any other kind.

    An http(s) path prefix stays in the base. An ssh origin names no API address, so its base is `ssh://host`, without user or port.
    """
    url = url.strip()
    scp = _SCP_REMOTE.match(url)
    if scp:
        return parse_remote(f"ssh://{scp['user']}@{scp['host']}/{scp['path']}")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if parts.scheme not in ("http", "https", "ssh") or not parts.hostname or len(segments) < 2:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    prefix, owner, name = segments[:-2], segments[-2], segments[-1].removesuffix(".git")
    if parts.scheme == "ssh":
        return f"ssh://{host}", owner, name
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}" + "".join(f"/{s}" for s in prefix), owner, name


def url_host(url: str) -> str | None:
    """The lowercased host of `url`, port and credentials excluded; None when it has none or cannot be parsed."""
    try:
        return urlsplit(url.strip()).hostname
    except ValueError:
        return None


def origin_host(url: str) -> str | None:
    """The host of an origin `parse_remote` accepts, else None."""
    parsed = parse_remote(url)
    return url_host(parsed[0]) if parsed else None


def parse_open_prs(status: int, body: object, branch: str) -> list[int] | str:
    """Numbers of the open PRs whose head is `branch` in a pull list body, or the reason it could not be read."""
    if status != 200 or not isinstance(body, list):
        return f"could not list open pull requests for {branch}: {_message(status, body)}"
    try:
        return [int(p["number"]) for p in body if isinstance(p, dict) and (p.get("head") or {}).get("ref") == branch]
    except (KeyError, TypeError, ValueError):
        return f"could not read the open pull requests for {branch}"


def parse_open_pr(status: int, body: object) -> tuple[bool, str]:
    """`(True, html_url)` for a created pull request, else `(False, Forgejo's message)`."""
    url = body.get("html_url") if isinstance(body, dict) else None
    if status in (200, 201) and isinstance(url, str) and url:
        return True, url
    return False, _message(status, body)


def parse_checks(body: object) -> tuple[int, str]:
    """`(returncode, output)` as `land.check_poll_result` gives: 0 pass, `land.PENDING_RC` pending, 1 fail.

    A commit with no statuses is `(1, "no checks reported")`, which `land.wait_decision` retries until its timeout.
    """
    if not isinstance(body, dict):
        raise ForgeError("could not read the commit status")
    statuses = [s for s in body.get("statuses") or [] if isinstance(s, dict)]
    if not statuses:
        return 1, land._NO_CHECKS
    state = body.get("state")
    if state == "success":
        return 0, ""
    if state in ("failure", "error"):
        return 1, f"failing checks: {_context_names(statuses, ('failure', 'error'))}"
    if state == "pending":
        return land.PENDING_RC, f"checks pending: {_context_names(statuses, ('pending',))}"
    return 1, f"unrecognised combined status: {state}"


def _context_names(statuses: list[dict], states: tuple[str, ...]) -> str:
    """Contexts of the statuses in `states`, by each row's `status` (only the rollup says `state`); `combined status` when
    none is, as when the server's rollup disagrees with its rows."""
    return ", ".join(s.get("context") or "status" for s in statuses if s.get("status") in states) or "combined status"


def parse_merge_state(body: object) -> str:
    """`CLEAN` or `DIRTY` from an open pull request's `mergeable`; `UNKNOWN` for a closed or merged one, whose `mergeable` is false
    without a conflict. ForgeError when `mergeable` is absent or not a bool."""
    flag = body.get("mergeable") if isinstance(body, dict) else None
    if not isinstance(flag, bool):
        raise ForgeError("could not read mergeable")
    if body.get("merged") is True or body.get("state", "open") != "open":
        return UNSETTLED
    return MERGEABLE if flag else CONFLICTED


def parse_merge_response(status: int, body: object) -> tuple[bool, str]:
    """`(True, "merged")` on 2xx, `(False, Forgejo's message)` on 405 and 409; any other status raises ForgeError."""
    if 200 <= status < 300:
        return True, "merged"
    if status in (405, 409):
        return False, _message(status, body)
    raise ForgeError(f"merge failed: {_message(status, body)}")


def parse_delete_response(status: int, body: object) -> tuple[bool, str]:
    """`(True, "deleted")` on 200 or 204, else `(False, Forgejo's message)`."""
    return (True, "deleted") if status in (200, 204) else (False, f"branch not deleted: {_message(status, body)}")


def parse_branch_tip(status: int, body: object) -> str | None:
    """The commit a `GET /branches/{branch}` answer names; None for a branch the server does not show (404) yet."""
    commit = body.get("commit") if status == 200 and isinstance(body, dict) else None
    sha = commit.get("id") if isinstance(commit, dict) else None
    return sha if isinstance(sha, str) and sha else None


def _call(target: Target, env: Mapping[str, str], method: str, tail: str, body: object | None = None) -> tuple[int, object | None]:
    try:
        return request(target.settings, env, method, target.path(tail), body=body)
    except ForgejoError as exc:
        raise ForgeError(str(exc)) from exc


def list_open_prs(target: Target, env: Mapping[str, str], branch: str, page: int = 1) -> list[int] | str:
    """Every page of open pulls, until one comes back short; the list has no head filter, so matching is client-side."""
    try:
        status, body = _call(target, env, "GET", f"/pulls?state=open&limit={PAGE_SIZE}&page={page}")
    except ForgeError as exc:
        return f"could not list open pull requests for {branch}: {exc}"
    found = parse_open_prs(status, body, branch)
    if isinstance(found, str) or len(body) < PAGE_SIZE or page >= MAX_PAGES:
        return found
    rest = list_open_prs(target, env, branch, page + 1)
    return rest if isinstance(rest, str) else [*found, *rest]


def create_pr(target: Target, env: Mapping[str, str], title: str, body: str, head: str, base: str) -> tuple[bool, str]:
    return parse_open_pr(*_call(target, env, "POST", "/pulls", {"title": title, "body": body, "head": head, "base": base}))


def read_checks(target: Target, env: Mapping[str, str], sha: str) -> tuple[int, str]:
    status, body = _call(target, env, "GET", f"/commits/{quote(sha, safe='')}/status")
    if status != 200:
        raise ForgeError(f"could not read the status of {sha}: {_message(status, body)}")
    return parse_checks(body)


def read_merge_state(target: Target, env: Mapping[str, str], pr: int) -> str:
    status, body = _call(target, env, "GET", f"/pulls/{pr}")
    if status != 200:
        raise ForgeError(f"could not read pull request {pr}: {_message(status, body)}")
    return parse_merge_state(body)


def squash_merge(target: Target, env: Mapping[str, str], pr: int) -> tuple[bool, str]:
    return parse_merge_response(*_call(target, env, "POST", f"/pulls/{pr}/merge", {"Do": "squash"}))


def delete_branch(target: Target, env: Mapping[str, str], branch: str) -> tuple[bool, str]:
    return parse_delete_response(*_call(target, env, "DELETE", f"/branches/{quote(branch, safe='/')}"))


def _git(repo: Path | str, *argv: str) -> str:
    """Stdout of `git -C repo argv`; ForgeError with git's stderr when it cannot run or exits nonzero."""
    try:
        r = bounded_run.completed(["git", "-C", str(repo), *argv], capture_output=True, text=True)
    except OSError as exc:
        raise ForgeError(f"git {' '.join(argv)}: {exc}") from exc
    if r.returncode != 0:
        raise ForgeError(r.stderr.strip() or f"git {' '.join(argv)} exited {r.returncode}")
    return r.stdout.strip()


def _ssh_api_base(host: str | None, env: Mapping[str, str]) -> str:
    """`FORGEJO_BASE_URL` for an ssh origin on `host`; ForgeError when it is unset or names another host."""
    configured = env.get(BASE_URL_ENV, "").strip()
    if not configured:
        raise ForgeError(f"origin {host} is an ssh remote, so its API address must come from {BASE_URL_ENV}, which is unset")
    if url_host(configured) != host:
        raise ForgeError(f"{BASE_URL_ENV} {configured!r} is not on the origin's host {host}")
    return configured


def _target(repo: Path | str, env: Mapping[str, str]) -> Target:
    """The Forgejo project `repo`'s `origin` names; ForgeError when it has no origin, is not an http(s) or ssh URL, or is ssh without a matching `FORGEJO_BASE_URL`."""
    url = _git(repo, "remote", "get-url", "origin")
    parsed = parse_remote(url)
    if parsed is None:
        raise ForgeError(f"origin {url!r} is not an http(s) or ssh Forgejo URL")
    base, owner, name = parsed
    if base.startswith("ssh://"):
        base = _ssh_api_base(url_host(base), env)
    try:
        return Target(read_settings({URL_KEY: base, TOKEN_ENV_KEY: DEFAULT_TOKEN_ENV}), owner, name)
    except ForgejoError as exc:
        raise ForgeError(str(exc)) from exc


def push(repo: Path, branch: str, expected_tip: str | None = None) -> tuple[bool, str]:
    """`git push`, then wait until the server shows `branch` at the pushed commit. Forgejo records a pushed branch from
    a queue after the push returns, and until it has, a pull request from that branch is refused with "The target
    couldn't be found". Found by the smoke suite against a real Forgejo; a fake server answered at once."""
    ok, detail = forge_github.push(repo, branch, expected_tip)
    if not ok:
        return ok, detail
    try:
        target, sha = _target(repo, os.environ), _git(repo, "rev-parse", f"refs/heads/{branch}")
    except ForgeError as exc:
        return False, f"pushed {branch}, but cannot ask the server for it: {exc}"
    deadline, seen = time.monotonic() + PUSH_SETTLE_S, None
    while True:
        try:
            seen = parse_branch_tip(*_call(target, os.environ, "GET", f"/branches/{quote(branch, safe='')}"))
        except ForgeError as exc:
            seen = f"unreadable ({exc})"
        if seen == sha:
            return True, detail
        if time.monotonic() >= deadline:
            return False, f"pushed {branch} at {sha[:12]}, but the server showed {seen or 'no such branch'} after {PUSH_SETTLE_S:.0f}s"
        time.sleep(PUSH_POLL_S)


def first_green(candidates: Sequence[str], read: Callable[[str], tuple[int, str]]) -> tuple[str, ...]:
    """The first of `candidates` (newest first) whose checks `read` passes (`parse_checks` code 0), as a one-commit
    tuple, else (). Stops at that commit, so a long history costs one read per commit newer than the green one."""
    return next(((sha,) for sha in candidates if read(sha)[0] == 0), ())


def green_commits(repo: Path, candidates: Sequence[str]) -> tuple[str, ...] | str:
    """What the edge upgrade asks of a checkout whose origin is on Forgejo: the newest of `candidates` whose combined
    commit status is success, as `first_green` gives it; or why the statuses could not be read. A commit with no
    status has not passed. Forgejo Actions and any other CI report through commit statuses, so this needs no
    workflow name, where `forge_github.ci_runs_argv` asks for one workflow's runs."""
    try:
        target = _target(repo, os.environ)
        return first_green(candidates, lambda sha: read_checks(target, os.environ, sha))
    except ForgeError as exc:
        return str(exc)


def find_open_prs(repo: Path, branch: str) -> list[int] | str:
    """Numbers of the open PRs whose head is `branch`, or the reason they could not be listed."""
    try:
        return list_open_prs(_target(repo, os.environ), os.environ, branch)
    except ForgeError as exc:
        return f"could not list open pull requests for {branch}: {exc}"


def open_pr(repo: Path, title: str, body: str, *, head: str | None = None, base: str | None = None, draft: bool = False) -> tuple[bool, str]:
    """`base` None is the branch `origin/HEAD` names, so it is unset for a repo that was not cloned. Forgejo has no draft flag on create: a draft is a title that starts with `DRAFT_PREFIX`."""
    try:
        source = head or _git(repo, "symbolic-ref", "--short", "HEAD")
        into = base or _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")
        return create_pr(_target(repo, os.environ), os.environ, f"{DRAFT_PREFIX}{title}" if draft else title, body, source, into)
    except ForgeError as exc:
        return False, str(exc)


def is_draft_title(title: str) -> bool:
    return title.upper().startswith(DRAFT_PREFIX.strip().upper())


def _open_pull(repo: Path, head: str) -> tuple[Target, int, str]:
    """Target, number and title of the open PR from `head`; ForgeError naming `head` when there is none."""
    target = _target(repo, os.environ)
    found = list_open_prs(target, os.environ, head)
    if isinstance(found, str):
        raise ForgeError(found)
    if not found:
        raise ForgeError(f"no open pull request for {head}")
    status, body = _call(target, os.environ, "GET", f"/pulls/{found[0]}")
    if status != 200 or not isinstance(body, dict):
        raise ForgeError(f"could not read pull request {found[0]}: {_message(status, body)}")
    return target, found[0], str(body.get("title") or "")


def _patch_pull(target: Target, number: int, fields: dict) -> tuple[bool, str]:
    status, body = _call(target, os.environ, "PATCH", f"/pulls/{number}", fields)
    url = body.get("html_url") if isinstance(body, dict) else None
    if status in (200, 201):
        return True, url if isinstance(url, str) and url else f"pull request {number}"
    return False, _message(status, body)


def update_pr(repo: Path, title: str, body: str, *, head: str) -> tuple[bool, str]:
    """Edit the title and body of the open PR from `head`. A draft stays a draft: its prefix is kept."""
    try:
        target, number, current = _open_pull(repo, head)
        keep = DRAFT_PREFIX if is_draft_title(current) and not is_draft_title(title) else ""
        return _patch_pull(target, number, {"title": f"{keep}{title}", "body": body})
    except ForgeError as exc:
        return False, str(exc)


def mark_ready(repo: Path, *, head: str) -> tuple[bool, str]:
    """Take the open PR from `head` out of draft by dropping the title prefix."""
    try:
        target, number, current = _open_pull(repo, head)
        if not is_draft_title(current):
            return True, f"pull request {number} is already ready"
        return _patch_pull(target, number, {"title": current[len(DRAFT_PREFIX.strip()):].lstrip()})
    except ForgeError as exc:
        return False, str(exc)


def merge_state(pr: int, *, repo: Path | str | None = None) -> str:
    if repo is None:
        raise ForgeError("forgejo: a PR number alone does not name a repository")
    return read_merge_state(_target(repo, os.environ), os.environ, pr)


def merge(repo: Path, step: dict) -> tuple[bool, str]:
    """Squash-merge the PR of `step["branch"]`, delete its branch, then update the local default branch.

    Once Forgejo merges, the PR has merged, so a failed delete or local update is reported in the detail and the result stays ok.
    """
    try:
        target = _target(repo, os.environ)
        found = list_open_prs(target, os.environ, step["branch"])
        if isinstance(found, str):
            return False, found
        if not found:
            return False, f"no open pull request for {step['branch']}"
        merged, detail = squash_merge(target, os.environ, found[0])
    except ForgeError as exc:
        return False, str(exc)
    if not merged:
        return False, detail
    try:
        deleted = delete_branch(target, os.environ, step["branch"])[1]
    except ForgeError as exc:
        deleted = f"branch not deleted: {exc}"
    left = _drop_local_branch(repo, step["branch"], step["default_branch"])
    return True, "\n".join(filter(None, [detail, deleted, left, _update_local_default(repo, step["default_branch"])]))


def _drop_local_branch(repo: Path | str, branch: str, default: str) -> str:
    """Leave `branch` for `default` when it is checked out, then delete it, as `gh pr merge --delete-branch` does."""
    try:
        on_branch = _git(repo, "symbolic-ref", "--short", "HEAD") == branch
    except ForgeError:
        on_branch = False
    try:
        switched = _git(repo, "checkout", "-q", default) if on_branch else ""
    except ForgeError as exc:
        return f"local {branch} kept: {exc}"
    try:
        _git(repo, "branch", "-D", branch)
    except ForgeError:
        return switched
    return f"deleted local {branch}"


def update_branch(pr: int, *, repo: Path | str | None = None) -> None:
    """Merge the base branch into PR `pr`'s head; ForgeError with Forgejo's message on failure."""
    if repo is None:
        raise ForgeError("forgejo: a PR number alone does not name a repository")
    status, body = _call(_target(repo, os.environ), os.environ, "POST", f"/pulls/{pr}/update")
    if not 200 <= status < 300:
        raise ForgeError(f"could not update pull request {pr}: {_message(status, body)}")


def poll_checks(repo: Path, *, ref: str = "HEAD", memo: Mapping | None = None) -> tuple[str, str, dict]:
    """One read of `ref`'s combined status, no wait: (`green` | `pending` | `failed` | `error`, detail, memo). A read
    that fails is `pending` for the caller's next pass, and `error` after `land.POLL_ERROR_LIMIT` in a row. Forgejo
    has no rerun."""
    try:
        sha = _git(repo, "rev-parse", ref)
    except ForgeError as exc:
        return land.unreadable_step(land.poll_memo(memo, ""), str(exc))
    seen = land.poll_memo(memo, sha)
    try:
        code, output = read_checks(_target(repo, os.environ), os.environ, sha)
    except ForgeError as exc:
        return land.unreadable_step(seen, str(exc))
    state = land.check_state(code, output)
    return state, output or state, {**seen, "errors": 0}


def wait_checks(repo: Path, timeout_s: float, sleep=time.sleep, now=time.monotonic, *, ref: str = "HEAD") -> tuple[bool, str]:
    try:
        target, sha = _target(repo, os.environ), _git(repo, "rev-parse", ref)
    except ForgeError as exc:
        return False, str(exc)
    # Edge bend (A2): a count of consecutive unreadable polls, reset by any readable one.
    errors = 0

    def poll() -> tuple[int, str]:
        nonlocal errors
        try:
            result = read_checks(target, os.environ, sha)
        except ForgeError as exc:
            errors += 1
            unreadable = land.unreadable_poll(errors, str(exc))
            if land.is_pending(unreadable[0]):
                sleep(land.poll_backoff_s(errors))
            return unreadable
        errors = 0
        return result
    return land.await_checks(poll, timeout_s, sleep, now)
