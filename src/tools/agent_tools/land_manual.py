"""`land: manual` repos: a person merges the PR, and the task finishes once origin shows the merge.

The setting lives in the repo's cartridge overlay, `<repo>/.agent/cartridge.yaml`, as a top-level `land:` or as
`repos.<directory name>.land`, beside `base:`.

What reaches the forge is the PR branch the land builds, `pr/<initiative>--<phase>` or `pr/<task>`, never the build
branch `agents/<run>/<task>`, which only moves between the chair and the build hosts. The forge squashes the PR branch,
so its tip is not an ancestor of the base afterwards. A merge done on the forge never moves local refs either, so the
check fetches first and reads only `origin/` refs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from agent_tools.base_branch import OVERLAY, Run, run_git

MANUAL = "manual"
REMEDY = "the human merges the PR; the task finishes once the merge shows on origin"
RECHECK_S = 300
HATCH = "if the PR is merged, check `git branch -r --merged origin/<base>` then run `cox route edit <task> --state done`"


def land_setting(overlay: Mapping[str, Any] | None, repo_name: str) -> str | None:
    """`repos.<repo_name>.land`, else the top-level `land`, of a parsed overlay; None when neither is a non-empty string."""
    overlay = overlay or {}
    repos = overlay.get("repos")
    entry = repos.get(repo_name) if isinstance(repos, Mapping) else None
    for value in (entry.get("land") if isinstance(entry, Mapping) else None, overlay.get("land")):
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def is_manual(repo: str) -> bool:
    """Edge. True when the repo's overlay sets `land: manual`; False when the file is absent, unreadable or silent."""
    path = Path(repo).expanduser()
    try:
        overlay = yaml.safe_load((path / OVERLAY).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    return land_setting(overlay if isinstance(overlay, Mapping) else None, path.name) == MANUAL


def pr_branches(initiative: str, phase: str, task: str, build_branch: str | None) -> list[str]:
    """The branches a human-merged PR can come from: the phase PR branch, the task PR branch, then the task record's own."""
    return list(dict.fromkeys(b for b in (f"pr/{initiative}--{phase}", f"pr/{task}", build_branch) if b))


def merge_targets(base: str, initiative: str, phase: str) -> list[str]:
    """The branches a PR lands on: the base, and the phase branch."""
    return list(dict.fromkeys([base, f"epic/{initiative}/{phase}"]))


def merged_verdict(fetched: Mapping[str, str], merged: Mapping[str, str | None]) -> tuple[str, str]:
    """("merged" | "unmerged" | "unverifiable", reason). `fetched` maps each branch to its fetch error, "" when it fetched.

    `merged` maps each fetched branch to the target that holds it, None when no target does. No branch on origin cannot be
    verified: the PR branch may be deleted after the merge, so the answer is a person's, not "not merged"."""
    hit = next(((b, t) for b, t in merged.items() if t), None)
    if hit is not None:
        return "merged", f"origin/{hit[0]} is merged into origin/{hit[1]}"
    present = [b for b, err in fetched.items() if not err and b in merged]
    if present:
        return "unmerged", f"origin/{', origin/'.join(present)} is not merged into any target"
    why = "; ".join(f"{b}: {err}" for b, err in fetched.items() if err)
    return "unverifiable", f"no PR branch on origin to check ({why[:200]})"


def _contained(repo: str, branch: str, target: str, run: Run) -> bool:
    """Edge. True when `origin/<branch>` is an ancestor of `origin/<target>`, or merges into it with no change (a squash)."""
    b, t = f"refs/remotes/origin/{branch}", f"refs/remotes/origin/{target}"
    if run(["git", "-C", repo, "merge-base", "--is-ancestor", b, t])[0] == 0:
        return True
    code, out = run(["git", "-C", repo, "merge-tree", "--write-tree", t, b])
    tree = run(["git", "-C", repo, "rev-parse", f"{t}^{{tree}}"])
    return code == 0 and tree[0] == 0 and out.split()[:1] == tree[1].split()[:1]


def merged_on_origin(repo: str, branches: list[str], targets: list[str], run: Run = run_git) -> tuple[str, str]:
    """Edge. Fetch the PR branches and the targets from origin, then ask git whether a branch is in a target.

    Only `refs/remotes/origin/` names are compared."""
    fetched = {}
    for n in dict.fromkeys([*branches, *targets]):
        code, out = run(["git", "-C", repo, "fetch", "origin", f"+refs/heads/{n}:refs/remotes/origin/{n}"])
        fetched[n] = "" if code == 0 else (out.strip().splitlines() or [f"exit {code}"])[0]
    live = [t for t in targets if not fetched[t]]
    merged = {b: next((t for t in live if _contained(repo, b, t, run)), None) for b in branches if not fetched[b]}
    return merged_verdict({b: fetched[b] for b in branches}, merged)


def record_branch(runs_dir: Path, run: str, task: str) -> str | None:
    """Edge. The `branch` of the task record `runs_dir/<run>/tasks/*/<task>.json`; None when absent or unreadable."""
    for path in sorted((runs_dir / run / "tasks").glob(f"*/{task}.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        branch = record.get("branch") if isinstance(record, dict) else None
        if isinstance(branch, str) and branch:
            return branch
    return None


def checked_recently(stamp: Path, window: float = RECHECK_S) -> bool:
    """Edge. True when `stamp` was touched less than `window` seconds ago."""
    try:
        return time.time() - stamp.stat().st_mtime < window
    except OSError:
        return False


def mark_checked(stamp: Path) -> None:
    """Edge. Touch `stamp`; an unwritable folder only costs the throttle."""
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
