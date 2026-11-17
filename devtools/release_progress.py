"""How far a release already got, decided from facts the edge read off origin.

Pure: no subprocess, filesystem or clock. `cli.py` gathers each fact (ls-remote, ancestry, `gh pr list`, the formula
at a ref) and acts on the answer, so a rerun of `devtools release` skips what is done and resumes at the first step
that is not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from agent_tools.release_version import release_key

# Every per-repo step a repo that has merged its release branch back has already run: its cut, bump and tag.
CUT_KINDS = frozenset({"release_branch", "bump_pyproject", "bump_cargo", "bump_version", "commit_index",
                       "push", "pr_create", "wait_checks", "merge", "tag", "rejoin", "tag_self", "merge_back"})


def merged_back(branch_head: str | None, tag_sha: str | None, tag_in_default: bool) -> bool:
    """True when the repo's cut is over: origin lists no `release/<version>`, lists `v<version>`, and that tag's
    commit is already on the default branch. A tag with neither a branch nor a path into the default branch is not
    a finished cut, so the plan's tag-collision refusal still guards it."""
    return branch_head is None and tag_sha is not None and tag_in_default


def resuming(listed: Collection[str], tagged: Collection[str], finished: Collection[str], umbrella: str) -> set[str]:
    """The repos whose release this run resumes rather than cuts.

    `listed`: origin lists `release/<version>`. `tagged`: origin lists `v<version>`. `finished`: `merged_back` holds.
    A repo is resuming when it is listed or finished; while the umbrella's branch is still listed, a tagged repo
    with no branch has merged back too."""
    still_cutting = set(tagged) - set(listed) if umbrella in listed else set()
    return set(listed) | set(finished) | still_cutting


def skips_cut_step(step: Mapping, finished: Collection[str]) -> bool:
    """True when `step` belongs to a repo whose cut is over (`finished`), so a rerun prints it done and runs nothing."""
    return step.get("kind") in CUT_KINDS and step.get("component") in finished


def commit_count(text: str) -> int | None:
    """The number `git rev-list --count` printed; None for anything else."""
    stripped = text.strip()
    return int(stripped) if stripped.isdigit() else None


def holds(count_text: str) -> bool:
    """True when `git rev-list --count <default>..<sha>` printed 0: the default branch already holds `sha`."""
    return commit_count(count_text) == 0


def open_pull_requests(text: str) -> int | None:
    """How many pull requests `gh pr list --json number` lists; empty output is none; None when it is not a list."""
    if not text.strip():
        return 0
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return len(payload) if isinstance(payload, list) else None


@dataclass(frozen=True)
class MergeBack:
    """What origin says of one repo's merge-back.

    `branch_head`: origin's `release/<version>`, None when not listed. `merged`: that head is an ancestor of
    `origin/<default>`. `open_prs`: open pull requests from the branch into the default branch (None: unreadable).
    `checked_out`: the checkout's current branch."""
    branch: str
    default: str
    branch_head: str | None
    merged: bool = False
    open_prs: int | None = 0
    checked_out: str = ""


Action = Literal["done", "merge", "open_and_merge", "fail"]


@dataclass(frozen=True)
class Next:
    action: Action
    detail: str
    # The checkout sits on the release branch a finished merge-back left behind: put it back on the default branch.
    restore: bool = False


def merge_back_next(facts: MergeBack) -> Next:
    """The merge-back's next move. A branch origin no longer lists, or whose head the default branch already holds,
    is done: a rerun opens no pull request (`gh` refuses one with no commits) and merges nothing twice."""
    restore = facts.checked_out == facts.branch
    if facts.branch_head is None:
        return Next("done", f"{facts.branch} already merged back and deleted", restore)
    if facts.merged:
        return Next("done", f"{facts.branch} already merged into {facts.default} at {facts.branch_head}", restore)
    if facts.open_prs is None:
        return Next("fail", f"could not read the open pull requests from {facts.branch}")
    return Next("merge", "") if facts.open_prs else Next("open_and_merge", "")


_SDIST_NAME = re.compile(r'^\s*url\s+"[^"]*/[A-Za-z0-9_.]+-(\d[^"/]*?)\.tar\.gz"')
_RESOURCE = re.compile(r"^\s*resource\s")


def formula_version(text: str | None) -> str | None:
    """The version of the sdist the formula's own `url` names (`coxswain-<version>.tar.gz`), before any `resource`."""
    for line in (text or "").splitlines():
        if _RESOURCE.match(line):
            return None
        found = _SDIST_NAME.match(line)
        if found:
            return found.group(1)
    return None


def _at_least(found: str | None, version: str) -> bool:
    have, want = release_key(found) if found else None, release_key(version)
    return have is not None and want is not None and have >= want


@dataclass(frozen=True)
class Tap:
    """What the tap clone and its origin say of this version's formula PR.

    `base_formula`: Formula/cox.rb at `origin/<default>`. `branch_head`: origin's `release/<version>`, None when not
    listed. `branch_formula`: the formula at that branch. `open_prs`: open pull requests from it (None: unreadable).
    `local_branch`: the tap clone has its own `release/<version>`, which `git checkout -b` would refuse."""
    version: str
    default: str
    branch: str
    base_formula: str | None
    branch_head: str | None = None
    branch_formula: str | None = None
    open_prs: int | None = 0
    local_branch: bool = False


TapAction = Literal["done", "prove", "open_and_prove", "bump", "recut", "fail"]


def tap_next(facts: Tap) -> tuple[TapAction, str]:
    """The tap step's next move.

    `done`: the default branch's formula is at this version or newer (the PR merged, or a later release did); nothing
    is fetched, pushed or proved. `prove`: the branch on origin holds this version and its PR is open, so only the
    install proof runs. `open_and_prove`: the branch holds it with no open PR. `bump`: nothing of this version yet;
    `recut` the same, once the stale local branch an interrupted run left (never pushed) is deleted. `fail`: origin's branch holds another version, or the PR list was unreadable."""
    base = formula_version(facts.base_formula)
    if _at_least(base, facts.version):
        return "done", f"the formula on origin/{facts.default} is already at {base}"
    if facts.branch_head is None:
        return ("recut", f"{facts.branch} was never pushed; the local branch is cut again") if facts.local_branch \
            else ("bump", "")
    on_branch = formula_version(facts.branch_formula)
    if on_branch != facts.version:
        return "fail", (f"origin's {facts.branch} holds the formula at {on_branch or 'no version'}, "
                        f"not {facts.version}; delete that branch to cut it again")
    if facts.open_prs is None:
        return "fail", f"could not read the open pull requests from {facts.branch}"
    if facts.open_prs:
        return "prove", f"{facts.branch} already pushed with its pull request open"
    return "open_and_prove", f"{facts.branch} already pushed"
