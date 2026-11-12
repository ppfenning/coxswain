"""The github forge: `gh` and `git push`. The protocol is in `agent_tools.forge`."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import time
from pathlib import Path

from agent_tools import land
from agent_tools.forge import ForgeError

READ_TIMEOUT_S = 30  # read-only gh and git calls: pr view and list, checks and api reads, rev-parse, symbolic-ref
WRITE_TIMEOUT_S = 120  # every other gh and git call: push, pr create, merge, update-branch, rerun, pull, fetch


def _run(argv: list[str], *, timeout: int, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    """`argv` run with a bound; ForgeError naming the command and the bound when it overruns. A hung gh or git never returns, so no subprocess here goes without `timeout`."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ForgeError(f"{' '.join(argv)} timed out after {timeout}s") from exc


def find_open_prs(repo: Path, branch: str) -> list[int] | str:
    """Numbers of the open PRs whose head is `branch`, or the reason they
    could not be listed."""
    try:
        r = _run(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"], timeout=READ_TIMEOUT_S, cwd=repo)
    except (OSError, ForgeError) as exc:
        return f"could not list open pull requests for {branch}: {exc}"
    if r.returncode != 0:
        return f"could not list open pull requests for {branch}: {(r.stderr or r.stdout).strip()}"
    try:
        return [int(p["number"]) for p in json.loads(r.stdout or "[]")]
    except (ValueError, KeyError, TypeError):
        return f"could not read the open pull requests for {branch}: {r.stdout.strip()}"


def find_branch_prs(repo: Path, branch: str) -> list[dict] | str:
    """`{"number", "state"}` per PR of any state whose head is `branch`, `state` being `open`, `merged` or `closed`
    (anything else gh says is kept lower case), or the reason they could not be listed."""
    argv = ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state"]
    try:
        r = _run(argv, timeout=READ_TIMEOUT_S, cwd=repo)
    except (OSError, ForgeError) as exc:
        return f"could not list pull requests for {branch}: {exc}"
    if r.returncode != 0:
        return f"could not list pull requests for {branch}: {(r.stderr or r.stdout).strip()}"
    try:
        return [{"number": int(p["number"]), "state": str(p["state"]).lower()} for p in json.loads(r.stdout or "[]")]
    except (ValueError, KeyError, TypeError):
        return f"could not read the pull requests for {branch}: {r.stdout.strip()}"


PR_LIST_LIMIT = 200  # more open PRs than a task can have; a listing that reaches it may be cut off


def list_open_prs_by_prefix(repo: Path, prefix: str) -> list[dict] | str:
    """`{"number", "head"}` per open PR whose head starts with `prefix`, or the reason they could not be listed. A listing that reaches `PR_LIST_LIMIT` is a reason, never a partial list."""
    argv = ["gh", "pr", "list", "--state", "open", "--limit", str(PR_LIST_LIMIT), "--json", "number,headRefName"]
    try:
        r = _run(argv, timeout=READ_TIMEOUT_S, cwd=repo)
    except (OSError, ForgeError) as exc:
        return f"could not list open pull requests: {exc}"
    if r.returncode != 0:
        return f"could not list open pull requests: {(r.stderr or r.stdout).strip()}"
    try:
        rows = json.loads(r.stdout or "[]")
        if len(rows) >= PR_LIST_LIMIT:
            return f"open pull request listing reached its limit of {PR_LIST_LIMIT}; it may be incomplete"
        return [{"number": int(p["number"]), "head": p["headRefName"]} for p in rows
                if p["headRefName"].startswith(prefix)]
    except (ValueError, KeyError, TypeError, AttributeError):
        return f"could not read the open pull requests: {r.stdout.strip()}"


def close_pr(repo: Path, number: int, comment: str) -> tuple[bool, str]:
    """Post `comment` on PR `number`, then close it. A closed PR is left alone; a comment already posted is not posted again."""
    try:
        view = _run(["gh", "pr", "view", str(number), "--json", "state,comments"], timeout=READ_TIMEOUT_S, cwd=repo)
        if view.returncode != 0:
            return False, (view.stderr or view.stdout).strip()
        data = json.loads(view.stdout or "{}")
        state = data.get("state")
        posted = any(c.get("body") == comment for c in data.get("comments") or [])
        if state == "CLOSED":
            return True, "already closed"
        if state == "MERGED":
            return False, f"PR {number} is already merged"
        comment_step = [] if posted else [["gh", "pr", "comment", str(number), "--body", comment]]
        for argv in [*comment_step, ["gh", "pr", "close", str(number)]]:
            r = _run(argv, timeout=WRITE_TIMEOUT_S, cwd=repo)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout).strip()
    except (OSError, ForgeError, ValueError, AttributeError) as exc:
        return False, f"could not close PR {number}: {exc}"
    return True, "closed"


def _ref_missing(message: str) -> bool:
    """gh's reply to deleting a ref that is not there: 422 "Reference does not exist". A 404 means the repo is unknown or hidden, not that the branch is gone."""
    return "reference does not exist" in message.lower()


def delete_branch(repo: Path, branch: str) -> tuple[bool, str]:
    argv = ["gh", "api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/heads/{branch}"]
    try:
        r = _run(argv, timeout=WRITE_TIMEOUT_S, cwd=repo)
    except (OSError, ForgeError) as exc:
        return False, f"could not delete {branch}: {exc}"
    if r.returncode == 0:
        return True, "deleted"
    message = (r.stderr or r.stdout).strip()
    return (True, "already gone") if _ref_missing(message) else (False, message)


UNKNOWN_PR_STATE = {"state": "unknown", "merged_at": None}
_PR_STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def parse_pr_state(stdout: str) -> dict:
    """`gh pr view --json state,mergedAt` output as `{"state", "merged_at"}`; `merged_at` is gh's UTC string, set only when merged."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return dict(UNKNOWN_PR_STATE)
    raw = data.get("state") if isinstance(data, dict) else None
    state = _PR_STATES.get(raw) if isinstance(raw, str) else None
    if state is None:
        return dict(UNKNOWN_PR_STATE)
    merged_at = data.get("mergedAt")
    return {"state": state, "merged_at": merged_at if state == "merged" and isinstance(merged_at, str) else None}


def pr_state(url: str) -> dict:
    try:
        r = _run(["gh", "pr", "view", url, "--json", "state,mergedAt"], timeout=READ_TIMEOUT_S)
    except (OSError, ForgeError):
        return dict(UNKNOWN_PR_STATE)
    return parse_pr_state(r.stdout) if r.returncode == 0 else dict(UNKNOWN_PR_STATE)


def push_argv(repo: Path, branch: str, expected_tip: str | None = None) -> list[str]:
    """The push argv; with `expected_tip` it leases the branch, so only that remote tip may be replaced."""
    lease = [f"--force-with-lease={branch}:{expected_tip}"] if expected_tip else []
    return ["git", "-C", str(repo), "push", *lease, "-u", "origin", branch]


def push(repo: Path, branch: str, expected_tip: str | None = None) -> tuple[bool, str]:
    try:
        r = _run(push_argv(repo, branch, expected_tip), timeout=WRITE_TIMEOUT_S)
    except ForgeError as exc:
        return False, str(exc)
    return r.returncode == 0, (branch if r.returncode == 0 else r.stderr.strip() or r.stdout.strip())


def open_pr(repo: Path, title: str, body: str, *, head: str | None = None, base: str | None = None, draft: bool = False) -> tuple[bool, str]:
    refs = [*(["--head", head] if head else []), *(["--base", base] if base else []), *(["--draft"] if draft else [])]
    try:
        r = _run(["gh", "pr", "create", "--title", title, "--body", body, *refs], timeout=WRITE_TIMEOUT_S, cwd=repo)
    except ForgeError as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stdout.strip() or r.stderr.strip())


def _gh_on_open_pr(repo: Path, head: str, verb: list[str]) -> tuple[bool, str]:
    """Run `gh pr <verb...>` on the one open PR whose head is `head`; `(False, detail naming head)` when there is none. Never opens a PR."""
    found = find_open_prs(repo, head)
    if isinstance(found, str):
        return False, found
    if not found:
        return False, f"no open pull request for {head}"
    try:
        r = _run(["gh", "pr", verb[0], str(found[0]), *verb[1:]], timeout=WRITE_TIMEOUT_S, cwd=repo)
    except (OSError, ForgeError) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stdout.strip() or r.stderr.strip())


def update_pr(repo: Path, title: str, body: str, *, head: str) -> tuple[bool, str]:
    """Edit the title and body of the open PR from `head`. A draft stays a draft."""
    return _gh_on_open_pr(repo, head, ["edit", "--title", title, "--body", body])


def mark_ready(repo: Path, *, head: str) -> tuple[bool, str]:
    """Take the open PR from `head` out of draft."""
    return _gh_on_open_pr(repo, head, ["ready"])


def _update_local_default(repo: Path, default: str) -> str:
    """Bring the local `default` branch up to `origin` without a checkout; git's output, or the failure."""
    try:
        current = _run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], timeout=READ_TIMEOUT_S)
        on_default = current.returncode == 0 and current.stdout.strip() == default
        argv = ["pull", "--ff-only", "origin", default] if on_default else ["fetch", "origin", f"{default}:{default}"]
        r = _run(["git", "-C", str(repo), *argv], timeout=WRITE_TIMEOUT_S)
    except ForgeError as exc:
        return f"local {default} not updated: {exc}"
    out = r.stdout.strip() or r.stderr.strip()
    return out if r.returncode == 0 else f"local {default} not updated: {out}"


def merge_verdict(exit_code: int, cli_text: str, pr: dict | None) -> tuple[bool, str]:
    """Whether a `gh pr merge` merged. The server's state outranks the exit code: gh merges on the server,
    then its local branch cleanup exits non-zero on a detached HEAD ("could not determine current branch").
    A non-zero exit with the PR read as merged is ok, the CLI's text kept as a warning; any other state fails."""
    if exit_code == 0:
        return True, cli_text
    if pr is not None and pr.get("state") == "merged":
        return True, f"warning: {cli_text}" if cli_text else "warning: gh exited non-zero after the PR merged"
    return False, cli_text


def _branch_pr_state(repo: Path, branch: str) -> dict:
    """Edge. `{"state", "merged_at"}` of the PR whose head is `branch`; unknown when the forge cannot say.
    `gh pr view` may not resolve a branch the forge already deleted, so the listing of every PR of that head is the fallback."""
    try:
        r = _run(["gh", "pr", "view", branch, "--json", "state,mergedAt"], timeout=READ_TIMEOUT_S, cwd=repo)
        state = parse_pr_state(r.stdout) if r.returncode == 0 else dict(UNKNOWN_PR_STATE)
    except (OSError, ForgeError):
        state = dict(UNKNOWN_PR_STATE)
    if state["state"] != "unknown":
        return state
    prs = find_branch_prs(repo, branch)
    if not isinstance(prs, str) and any(p["state"] == "merged" for p in prs) and not any(p["state"] == "open" for p in prs):
        return {"state": "merged", "merged_at": None}
    return state


def merge_flag(step: dict) -> str | None:
    """The `gh pr merge` method flag for `step["method"]`: squash when absent, rebase, or None for any other value."""
    return {"squash": "--squash", "rebase": "--rebase"}.get(step.get("method", "squash"))


def merge(repo: Path, step: dict) -> tuple[bool, str]:
    """Merge the PR of `step["branch"]`, then delete its remote and local head branch and update the local default branch.

    `step["method"]` is "squash" (the default when absent) or "rebase"; any other value fails without running anything.

    gh's own `--delete-branch` is not used: its local cleanup needs a current branch and fails on a detached
    HEAD after the server merged, so the head is deleted through the API instead, unless `step["delete_branch"]`
    is false; a step that omits the key deletes, as `--delete-branch` always did. A non-zero gh exit is judged by the PR's server state (`merge_verdict`). Once the PR has merged,
    a failed branch delete or local update is reported in the detail and the result stays ok.
    """
    flag = merge_flag(step)
    if flag is None:
        return False, f"unknown merge method: {step['method']!r}"
    try:
        r = _run(["gh", "pr", "merge", step["branch"], flag], timeout=WRITE_TIMEOUT_S, cwd=repo)
    except ForgeError as exc:
        return False, str(exc)
    text = r.stdout.strip() or r.stderr.strip()
    ok, merged = merge_verdict(r.returncode, text, None if r.returncode == 0 else _branch_pr_state(repo, step["branch"]))
    if not ok:
        return False, merged
    deleted = []
    if step.get("delete_branch", True):
        gone, how = delete_branch(repo, step["branch"])
        deleted = [f"remote branch {step['branch']} {how}" if gone else f"warning: remote branch {step['branch']} not deleted: {how}"]
        with contextlib.suppress(OSError, ForgeError):  # gh's --delete-branch also removed the local branch; one a worktree holds or HEAD sits on stays
            _run(["git", "-C", str(repo), "branch", "-D", step["branch"]], timeout=WRITE_TIMEOUT_S)
    return True, "\n".join(filter(None, [merged, *deleted, _update_local_default(repo, step["default_branch"])]))


def parse_merge_state(stdout: str) -> str:
    """The `mergeStateStatus` of `gh pr view --json mergeStateStatus` output, unchanged; ForgeError when absent."""
    data = _loads(stdout)
    state = data.get("mergeStateStatus") if isinstance(data, dict) else None
    if not isinstance(state, str) or not state:
        raise ForgeError(f"could not read mergeStateStatus: {stdout.strip()}")
    return state


def merge_state_argv(pr: int) -> list[str]:
    return ["gh", "pr", "view", str(pr), "--json", "mergeStateStatus"]


def update_branch_argv(pr: int) -> list[str]:
    return ["gh", "pr", "update-branch", str(pr)]


def _gh_run(argv: list[str], cwd: Path | str | None = None, *, timeout: int) -> subprocess.CompletedProcess:
    """`argv` run through gh in `cwd` (the repository, so gh finds its GitHub remote); ForgeError with gh's stderr
    when it cannot run, overruns `timeout`, or exits nonzero."""
    try:
        r = _run(argv, timeout=timeout, cwd=cwd)
    except OSError as exc:
        raise ForgeError(f"{' '.join(argv)}: {exc}") from exc
    if r.returncode != 0:
        raise ForgeError((r.stderr or r.stdout or "").strip() or f"{' '.join(argv)} exited {r.returncode}")
    return r


def merge_state(pr: int, *, repo: Path | str | None = None) -> str:
    return parse_merge_state(_gh_run(merge_state_argv(pr), repo, timeout=READ_TIMEOUT_S).stdout or "")


def update_branch(pr: int, *, repo: Path | str | None = None) -> None:
    _gh_run(update_branch_argv(pr), repo, timeout=WRITE_TIMEOUT_S)


def _read_checks(repo: Path, ref: str = "HEAD"):
    """`(True, (check_runs, status))` for `ref`'s REST check bodies, or `(False, detail)` on a failed or unparseable call."""
    try:
        head = _run(["git", "-C", str(repo), "rev-parse", ref], timeout=READ_TIMEOUT_S)
    except ForgeError as exc:
        return False, str(exc)
    if head.returncode != 0:
        return False, head.stderr.strip() or f"git rev-parse {ref} failed"
    argvs = land.rest_checks_argvs(head.stdout.strip())
    bodies = []
    for argv, key in zip(argvs, ("check_runs", "statuses")):
        try:
            r = _run(argv, timeout=READ_TIMEOUT_S, cwd=repo)
        except ForgeError as exc:
            return False, str(exc)
        body = land.merge_pages(r.stdout or "", key) if r.returncode == 0 else None
        if body is None:
            return False, (r.stderr or r.stdout or "").strip() or f"unreadable output from {argv[-1]}"
        bodies.append(body)
    return True, tuple(bodies)


def _suite_row(suite: dict, run: dict) -> dict:
    """A suite has no page url of its own, so its checks page is built from the repository url, head sha and suite id."""
    repo_url, sha, sid = (suite.get("repository") or {}).get("html_url"), suite.get("head_sha"), suite.get("id")
    page = f"{repo_url}/commit/{sha}/checks?check_suite_id={sid}" if repo_url and sha and sid is not None else None
    return {"workflow": run.get("name") or (suite.get("app") or {}).get("name"),
            "conclusion": suite.get("conclusion") or None,
            "url": run.get("html_url") or page}


def parse_check_suites(suites, runs) -> tuple[dict, ...]:
    """Workflow name and run url come from the run sharing `check_suite_id`, else the suite's app name and checks page."""
    rows = runs.get("workflow_runs") if isinstance(runs, dict) else None
    by_suite = {r["check_suite_id"]: r for r in rows or [] if isinstance(r, dict) and r.get("check_suite_id") is not None}
    listed = suites.get("check_suites") if isinstance(suites, dict) else None
    return tuple(_suite_row(s, by_suite.get(s.get("id"), {})) for s in listed or [] if isinstance(s, dict))


def check_suites_argvs(sha: str) -> tuple[list[str], list[str]]:
    root = "repos/{owner}/{repo}"
    return (["gh", "api", "--paginate", f"{root}/commits/{sha}/check-suites?per_page=100"],
            ["gh", "api", "--paginate", f"{root}/actions/runs?head_sha={sha}&per_page=100"])


def _read_pages(argv: list[str], repo: Path, key: str) -> dict:
    out = _gh_run(argv, repo, timeout=READ_TIMEOUT_S).stdout or ""
    body = land.merge_pages(out, key)
    if body is None:
        raise ForgeError(f"unreadable output from {argv[-1]}: {out.strip()}")
    return body


def _read_runs(argv: list[str], repo: Path) -> dict | None:
    """None when the runs cannot be read; each suite then falls back to its app name and checks page."""
    try:
        return _read_pages(argv, repo, "workflow_runs")
    except ForgeError:
        return None


def read_check_suites(repo: Path, sha: str) -> tuple[dict, ...]:
    """`read_check_suites`: every page of `sha`'s check suites as `parse_check_suites` rows; ForgeError when the suites cannot be read."""
    suites_argv, runs_argv = check_suites_argvs(sha)
    return parse_check_suites(_read_pages(suites_argv, repo, "check_suites"), _read_runs(runs_argv, repo))


def _loads(stdout: str):
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


def parse_base_ref(stdout: str) -> str | None:
    """The `baseRefName` of `gh pr view --json baseRefName` output, or None."""
    data = _loads(stdout)
    base = data.get("baseRefName") if isinstance(data, dict) else None
    return base if isinstance(base, str) and base else None


def parse_ruleset_contexts(stdout: str) -> tuple[str, ...] | None:
    """Context names of every `required_status_checks` rule in a `rules/branches/{base}` body; None when unparseable, () when none require any."""
    data = _loads(stdout)
    if not isinstance(data, list):
        return None
    try:
        names = [c["context"] for rule in data if isinstance(rule, dict) and rule.get("type") == "required_status_checks"
                 for c in rule["parameters"]["required_status_checks"]]
        return tuple(dict.fromkeys(names))
    except (KeyError, TypeError):
        return None


def parse_protection_contexts(stdout: str) -> tuple[str, ...] | None:
    """`contexts` and `checks[].context` of a classic `required_status_checks` body; None when unparseable."""
    data = _loads(stdout)
    if not isinstance(data, dict):
        return None
    contexts, checks = data.get("contexts") or [], data.get("checks") or []
    if not isinstance(contexts, list) or not isinstance(checks, list):
        return None
    try:
        return tuple(dict.fromkeys([*contexts, *(c["context"] for c in checks)]))
    except (KeyError, TypeError):
        return None


def rerun_run_ids(check_runs: dict) -> tuple[str, ...]:
    """Workflow run ids, in order and deduplicated, from the `details_url` of each failed check run. A failure with no `/actions/runs/<id>` url yields none."""
    urls = [r.get("details_url") or "" for r in check_runs.get("check_runs") or [] if r.get("conclusion") in land._FAILED_CONCLUSIONS]
    return tuple(dict.fromkeys(m[1] for m in (re.search(r"/actions/runs/(\d+)", u) for u in urls) if m))


def failed_check_run_ids(check_runs: dict) -> frozenset:
    """The `id` of each failed check run. A rerun replaces these, so a failure made only of them is the one already rerun."""
    return frozenset(r.get("id") for r in check_runs.get("check_runs") or [] if r.get("conclusion") in land._FAILED_CONCLUSIONS)


def jobs_argv(run_id: str) -> list[str]:
    return ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs"]


def parse_failed_steps(stdout: str) -> tuple[str, ...] | None:
    """Names of the failed steps in a run's jobs body, in order; None unless the body holds a `jobs` list."""
    data = _loads(stdout)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None
    steps = [s for j in jobs if isinstance(j, dict) for s in j.get("steps") or [] if isinstance(s, dict)]
    return tuple(str(s.get("name")) for s in steps if s.get("conclusion") in land._FAILED_CONCLUSIONS)


def base_ref_argv(ref: str) -> list[str]:
    """`gh pr view` for `ref`'s PR. `HEAD` is no branch name, so it is left out and gh reads the checked-out branch."""
    return ["gh", "pr", "view", *([] if ref == "HEAD" else [ref]), "--json", "baseRefName"]


def changed_paths_argv(ref: str) -> list[str]:
    return ["gh", "pr", "view", *([] if ref == "HEAD" else [ref]), "--json", "files"]


def parse_changed_paths(stdout: str) -> tuple[str, ...] | None:
    """The `path` of each file in `gh pr view --json files` output; None unless the body holds a `files` list."""
    data = _loads(stdout)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return None
    return tuple(f["path"] for f in files if isinstance(f, dict) and isinstance(f.get("path"), str))


def _gh_text(argv: list[str], repo: Path, *, timeout: int) -> str | None:
    """Stdout of `argv`, or None when it cannot run, overruns `timeout`, or exits nonzero."""
    try:
        r = _run(argv, timeout=timeout, cwd=repo)
    except (OSError, ForgeError):
        return None
    return r.stdout or "" if r.returncode == 0 else None


def read_rerun_scope(repo: Path, ref: str, ids: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The failed steps of runs `ids` and the PR's changed paths; None when any read fails or is unreadable."""
    texts = [_gh_text(jobs_argv(i), repo, timeout=READ_TIMEOUT_S) for i in ids]
    paths = parse_changed_paths(_gh_text(changed_paths_argv(ref), repo, timeout=READ_TIMEOUT_S) or "")
    steps = [parse_failed_steps(t or "") for t in texts]
    return None if paths is None or any(s is None for s in steps) else (tuple(n for s in steps for n in s or ()), paths)


def _required_for_base(repo: Path, base: str) -> tuple[str, ...] | None:
    root = "repos/{owner}/{repo}"
    ruleset = parse_ruleset_contexts(_gh_text(["gh", "api", f"{root}/rules/branches/{base}"], repo, timeout=READ_TIMEOUT_S) or "")
    if ruleset != ():  # names, or None for an unreadable ruleset: its contexts are unknown, so protection alone must not stand in
        return ruleset
    protection_argv = ["gh", "api", f"{root}/branches/{base}/protection/required_status_checks"]
    return parse_protection_contexts(_gh_text(protection_argv, repo, timeout=READ_TIMEOUT_S) or "")


def read_required_for_base(repo: Path, base: str) -> tuple[str, ...] | None:
    """Required check names for branch `base`, no PR needed: the rulesets', else classic protection's. () means both were read and require nothing; None means a call failed or a body was unreadable."""
    return _required_for_base(repo, base)


def _read_required(repo: Path, ref: str) -> tuple[str, ...] | None:
    """Required check names for the base of `ref`'s PR: the rulesets', else classic branch protection's. () means both were read and require nothing; None means a call failed or a body was unreadable."""
    base = parse_base_ref(_gh_text(base_ref_argv(ref), repo, timeout=READ_TIMEOUT_S) or "")
    return None if base is None else _required_for_base(repo, base)


_PR_FIELDS = ("number", "state", "url", "headRefName", "headRefOid", "baseRefName", "body")


def pr_view_argv(number: int) -> list[str]:
    return ["gh", "pr", "view", str(number), "--json", ",".join(_PR_FIELDS)]


def parse_pr_view(stdout: str) -> dict | None:
    """The `_PR_FIELDS` as a dict; None unless every one is present, `number` an int, the rest strings and all but `body` non-empty."""
    data = _loads(stdout)
    if not isinstance(data, dict) or any(f not in data for f in _PR_FIELDS):
        return None
    number, body = data["number"], data["body"]
    named = [data[f] for f in _PR_FIELDS if f not in ("number", "body")]
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(body, str):
        return None
    return {f: data[f] for f in _PR_FIELDS} if all(isinstance(v, str) and v for v in named) else None


def read_pr(repo: Path, number: int) -> dict | None:
    """`parse_pr_view` of PR `number`; None when gh fails or its output is unreadable."""
    return parse_pr_view(_gh_text(pr_view_argv(number), repo, timeout=READ_TIMEOUT_S) or "")


def required_for_pr(repo: Path, number: int) -> tuple[str, ...] | None:
    """Required check names for the PR's base branch; () means none required, None means unreadable."""
    pr = read_pr(repo, number)
    return None if pr is None else read_required_for_base(repo, pr["baseRefName"])


_STATUS_STATES = {"success": "success", "failure": "failure", "error": "failure", "pending": "pending"}


def _run_state(run: dict) -> str:
    """Pending until completed; `neutral` is skipped; an unknown conclusion is pending, never success."""
    conclusion = run.get("conclusion") if run.get("status") == "completed" else None
    if conclusion == "success":
        return "success"
    if conclusion in ("skipped", "neutral"):
        return "skipped"
    return "failure" if conclusion in land._FAILED_CONCLUSIONS else "pending"


def parse_check_rows(check_runs, statuses) -> tuple[dict, ...] | None:
    """Rows `{name, state, url}`; None when a body or a row's name is unreadable, or a body holds fewer rows than its `total_count`."""
    runs = check_runs.get("check_runs") if isinstance(check_runs, dict) else None
    legacy = statuses.get("statuses") if isinstance(statuses, dict) else None
    if not isinstance(runs, list) or not isinstance(legacy, list):
        return None
    if any(isinstance(b.get("total_count"), int) and b["total_count"] != len(rows) for b, rows in ((check_runs, runs), (statuses, legacy))):
        return None
    run_rows = [(r.get("name"), _run_state(r), r.get("html_url")) for r in runs if isinstance(r, dict)]
    status_rows = [(s.get("context"), _STATUS_STATES.get(s.get("state"), "pending"), s.get("target_url")) for s in legacy if isinstance(s, dict)]
    rows = [*run_rows, *status_rows]
    if len(rows) != len(runs) + len(legacy) or not all(isinstance(name, str) and name for name, _, _ in rows):
        return None
    return tuple({"name": name, "state": state, "url": url} for name, state, url in rows)


def pr_check_rows(repo: Path, sha: str) -> tuple[dict, ...] | None:
    """Check runs and commit statuses of `sha` as `parse_check_rows` rows; () means none exist, None means unreadable."""
    bodies = []
    for argv, key in zip(land.rest_checks_argvs(sha), ("check_runs", "statuses")):
        text = _gh_text(argv, repo, timeout=READ_TIMEOUT_S)
        body = None if text is None else land.merge_pages(text, key)
        if body is None:
            return None
        bodies.append(body)
    return parse_check_rows(*bodies)


def squash_merge_argv(number: int) -> list[str]:
    return ["gh", "pr", "merge", str(number), "--squash", "--delete-branch"]


def merge_pr(repo: Path, number: int) -> tuple[bool, str]:
    """Squash-merge PR `number`. A non-zero exit is judged by the PR's server state (`merge_verdict`), since gh's
    `--delete-branch` cleanup can fail after the merge landed; any other refusal is `(False, <the forge's own reason>)`."""
    try:
        r = _run(squash_merge_argv(number), timeout=WRITE_TIMEOUT_S, cwd=repo)
        code, text = r.returncode, (r.stdout or "").strip() or (r.stderr or "").strip()
    except (OSError, ForgeError) as exc:
        code, text = 1, str(exc)
    pr = None if code == 0 else read_pr(repo, number)
    return merge_verdict(code, text, None if pr is None else {"state": _PR_STATES.get(pr["state"], "unknown")})


def wait_checks(repo: Path, timeout_s: float, sleep=time.sleep, now=time.monotonic, *, ref: str = "HEAD") -> tuple[bool, str]:
    required = _read_required(repo, ref)
    started = now()

    def suites() -> tuple[dict, ...]:
        """The check suites of `ref`'s head, or none when they cannot be read: the poll then follows the check runs alone."""
        try:
            head = _run(["git", "-C", str(repo), "rev-parse", ref], timeout=READ_TIMEOUT_S)
            return read_check_suites(repo, head.stdout.strip()) if head.returncode == 0 else ()
        except ForgeError:
            return ()

    # Edge bend (A2): a count of consecutive unreadable polls, reset by any readable one.
    errors = 0
    # Edge bend (A2): `rerun_of` holds the failed check run ids once gh accepted every rerun, and a newer failure is final.
    # `rerun_tries` counts refused rerun calls, which gh returns while a sibling job still runs.
    # `stale_polls` counts polls that still show only the rerun failures. Both stop at POLL_ERROR_LIMIT.
    # `accepted` holds the run ids gh took a rerun for, so a retry after a partial refusal never reruns one twice.
    rerun_of: frozenset | None = None
    accepted: frozenset = frozenset()
    rerun_tries = stale_polls = 0

    def rerun(ids: tuple[str, ...], failed: frozenset) -> tuple[int, str]:
        nonlocal rerun_of, rerun_tries, accepted
        todo = [i for i in ids if i not in accepted]
        refused = [i for i in todo if _gh_text(["gh", "run", "rerun", i, "--failed"], repo, timeout=WRITE_TIMEOUT_S) is None]
        accepted = accepted | frozenset(todo) - frozenset(refused)
        rerun_tries += bool(refused)
        rerun_of = None if refused else failed
        return land.PENDING_RC, (f"rerun refused for runs {', '.join(refused)}, retrying ({rerun_tries}/{land.POLL_ERROR_LIMIT})"
                                 if refused else f"failed runs rerun once: {', '.join(ids)}")

    def poll() -> tuple[int, str]:
        nonlocal errors, stale_polls
        ok, value = _read_checks(repo, ref)
        errors = 0 if ok else errors + 1
        if ok:
            bound = {"elapsed_s": now() - started, "timeout_s": timeout_s}
            result = land.check_poll_result(*value, required, suites=suites() if land.needs_suites(*value, required) else (), **bound)
            if result[1].startswith(land.WORKFLOW_ISSUE):
                return result  # a startup failure is final: no rerun is tried
            failed = failed_check_run_ids(value[0])
            if result[0] == 0 or not failed:
                return result
            if rerun_of is not None:
                stale = failed <= rerun_of and stale_polls < land.POLL_ERROR_LIMIT
                stale_polls += stale
                return (land.PENDING_RC, "rerun not yet registered: " + result[1]) if stale else result
            ids = rerun_run_ids(value[0])
            if not ids or rerun_tries >= land.POLL_ERROR_LIMIT:
                return result
            scope = read_rerun_scope(repo, ref, ids)  # unknown: an unreadable read falls back to the one rerun
            if scope is not None and not land.rerun_scope(*scope):
                return result[0], f"{result[1]}; not rerun, the change touches the failing suites: {', '.join(scope[0])}"
            return rerun(ids, failed)
        result = land.unreadable_poll(errors, value)
        if land.is_pending(result[0]):
            sleep(land.poll_backoff_s(errors))  # on top of the 15s between polls: a rate limit needs room
        return result
    return land.await_checks(poll, timeout_s, sleep, now)
