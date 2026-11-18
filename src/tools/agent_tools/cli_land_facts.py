"""`cox runs land`, the facts: the land record, the branches, what a phase or task already has on main."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Collection, Sequence
from pathlib import Path

from agent_tools.cli_common import _read_text_or_none
from agent_tools.cli_land_steps import _generated_tree, _git_out
from agent_tools.cli_lazy import bounded_run, chair_facts, forge, forge_github, land, phase_status, route, run_store
from agent_tools.cli_runs_fetch import _branch_exists


def _land_record(runs_dir: Path, run_id: str, task: str | None) -> tuple[dict | None, str, int]:
    """The one record file for `task` (any task when None). A named task with no file under the land's own run takes the
    newest earlier run's, the order `_land_phase_record` uses; several files in one run stay ambiguous."""
    tasks_root = runs_dir / run_id / "tasks"
    matches = sorted(tasks_root.glob(f"*/{task}.json" if task else "*/*.json"))
    if not matches and task:
        matches = next((m for r in _earlier_runs(run_id) if (m := sorted((runs_dir / r / "tasks").glob(f"*/{task}.json")))), [])
    if len(matches) != 1:
        return None, str(tasks_root.resolve()), len(matches)
    path = matches[0]
    record = json.loads(path.read_text(encoding="utf-8"))
    record.setdefault("run", path.parents[2].name)
    record.setdefault("task", path.stem)
    record.setdefault("phase", path.parent.name)
    return record, str(path), 1


def _choose_record(stored: dict | None, filed: dict | None) -> tuple[dict | None, str | None]:
    """The store record wins over the file record; the label is `store` or `file`, None when neither exists."""
    if stored is not None:
        return stored, "store"
    if filed is not None:
        return filed, "file"
    return None, None


def _land_load(runs_dir: Path, run_id: str, task: str | None) -> tuple[dict | None, str, int, str | None]:
    """Edge: `(record, where, count, source)`, the store first and the file when the store says None.

    The store is asked by phase when exactly one file names the task, whose parent dir is the phase and stem the task.
    With no file and a task given, it is asked which phases hold the task; exactly one phase names a record
    that has no file yet, and `where` is the path that file would have.
    """
    filed, where, count = _land_record(runs_dir, run_id, task)
    if count == 0 and task:
        phases = run_store.task_record_phases(runs_dir, run_id, task)
        held = run_store.task_record(runs_dir, run_id, phases[0], task) if len(phases) == 1 else None
        if held is not None:
            path = runs_dir / run_id / "tasks" / phases[0] / f"{task}.json"
            return {"run": run_id, "task": task, "phase": phases[0], **held}, str(path), 1, "store"
        return None, where, count, None
    path = Path(where)
    filed_run = path.parents[2].name if count == 1 else run_id  # an earlier run's record is read from that run
    stored = run_store.task_record(runs_dir, filed_run, path.parent.name, path.stem) if count == 1 else None
    if stored is not None:
        stored = {"run": filed_run, "task": path.stem, "phase": path.parent.name, **stored}
    record, source = _choose_record(stored, filed)
    return record, where, count, source


def _initiative_of_phase(work_root: Path, phase: str, run_id: str | None = None) -> str | None:
    """The initiative whose `work/<initiative>/<phase>/` holds a ticket: the run's own (`x-3` is `x`) when it does,
    else the one initiative that does, else `None`. The phase record may not name it, and a phase name such as `fix`
    repeats across initiatives."""
    own = chair_facts.run_initiative(run_id) if run_id else None
    if own and any(p.name != "initiative.md" for p in (work_root / own / phase).glob("*.md")):
        return own
    found = {p.parent.parent.name for p in work_root.glob(f"*/{phase}/*.md") if p.name != "initiative.md"}
    return found.pop() if len(found) == 1 else None


def _initiative_title(work_root: Path, initiative: str) -> str:
    """Edge. `work_root/<initiative>/initiative.md`'s own `title:`; "" when the file is missing, unreadable,
    or carries no title -- the phase land PR title then falls back to its id-and-phase-only form."""
    try:
        text = (work_root / initiative / "initiative.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    value = route.parse_frontmatter(text)[0].get("title", "")
    return value if isinstance(value, str) else ""


def _land_branches(repo: Path, record: dict, default_branch: str) -> dict[str, list[str]]:
    """Commit subjects ahead of `default_branch`, per candidate branch, with
    merge commits excluded by `git` (`--no-merges`) and patch-equivalent
    commits already on `default_branch` dropped (`--cherry-pick`)."""
    candidates = [f"agents/{record['run']}/{record['task']}", f"epic/{record.get('initiative')}/{record['phase']}"]
    branches: dict[str, list[str]] = {}
    for b in candidates:
        out = bounded_run.completed(["git", "-C", str(repo), "log", "--no-merges", "--cherry-pick", "--right-only", "--format=%s", f"{default_branch}...{b}"],
                             capture_output=True, text=True)
        if out.returncode == 0:
            branches[b] = [line for line in out.stdout.splitlines() if line]
    return branches


def _land_branch_counts(repo: Path, record: dict, default_branch: str, branches: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """Edge. Per candidate branch git resolves, `ahead` and `behind` from `rev-list --left-right --count
    <base>...<branch>` (raw, patch-equivalent commits included) and `unique`, the commits `_land_branches` kept."""
    counts: dict[str, dict[str, int]] = {}
    for b in branches:
        out = bounded_run.completed(["git", "-C", str(repo), "rev-list", "--left-right", "--count", f"{default_branch}...{b}"],
                             capture_output=True, text=True)
        behind, _, ahead = out.stdout.partition("\t")
        if out.returncode == 0 and behind.strip().isdigit() and ahead.strip().isdigit():
            counts[b] = {"ahead": int(ahead), "behind": int(behind), "unique": len(branches[b])}
    return counts


def _phase_done_fact(statuses: Sequence[str], validated: bool) -> bool:
    return phase_status.phase_status(statuses, validated) == "done"


def _land_phase_done(runs_dir: Path, run_id: str, record: dict) -> bool:
    """Edge. Is the task's phase done: its tickets all settled and its manifest's `phase_verdict.goal_met` true
    (the key `phase_validate` writes). A missing initiative, unreadable items or no manifest give False.
    Items come from the files, never the store: the fact only orders the branch choice, and a store read here
    would add a `task_state` call ahead of the lease's own re-read."""
    initiative, phase = record.get("initiative"), record.get("phase")
    if not initiative or not phase:
        return False
    items, _ = _phase_items(runs_dir.parent / "work", initiative, phase, runs_dir=runs_dir, land_mode="files")
    if items is None:
        return False
    try:
        manifest = _land_phase_record(runs_dir, run_id, phase)[0] or {}
    except (OSError, ValueError):
        return False
    validated = (manifest.get("phase_verdict") or {}).get("goal_met") is True
    return _phase_done_fact([str(i.get("status")) for i in items], validated)


def _phase_task_facts(repo: Path, phase_branch: str, items: list[dict], task_records: list[dict]) -> list[dict] | None:
    """Edge. Per approved task, the facts `land.task_gather_status` reads: its `agents/<run>/<task>` commit is an
    ancestor of `phase_branch`, that branch exists, the record carries a non-blank `build.patch`. A task with none
    of those but a `build.files_touched` counts as reachable. None when
    `phase_branch` does not resolve, so a land with no phase branch to compare against plans as it always did."""
    def git_ok(*args: str) -> bool:
        return bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True).returncode == 0
    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{phase_branch}"):
        return None
    approved = {i["id"] for i in items if i.get("status") == "approved"}
    facts = []
    for record in task_records:
        task = record.get("task")
        if task not in approved:
            continue
        branch = f"agents/{record.get('run')}/{task}"
        exists = git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        build = record.get("build") or {}
        has_patch = isinstance(build.get("patch"), str) and bool(build["patch"].strip())
        # No branch and no patch but recorded files: the squash's files check, not this plan, says if they landed.
        deferred = not exists and not has_patch and bool(build.get("files_touched"))
        facts.append({"task": task, "branch_exists": exists, "has_patch": has_patch,
                      "reachable": deferred or (exists and git_ok("merge-base", "--is-ancestor", branch, phase_branch))})
    return facts


def _recover_branches(repo: Path, record: dict, phase_branch: str) -> dict[str, list[str]]:
    """Commit subjects ahead of the phase branch, per recover candidate
    branch — the same `git log --no-merges` shape `_land_branches` uses, but
    against the phase branch instead of the default branch, since recover
    merges INTO the phase branch rather than cherry-picking off it."""
    candidates = [f"agents/{record['run']}/{record['task']}", f"epic/{record.get('initiative')}/{record['phase']}--{record['task']}"]
    branches: dict[str, list[str]] = {}
    for b in candidates:
        out = bounded_run.completed(["git", "-C", str(repo), "log", "--no-merges", "--format=%s", f"{phase_branch}..{b}"], capture_output=True, text=True)
        if out.returncode == 0:
            branches[b] = [line for line in out.stdout.splitlines() if line]
    return branches


def _open_prs_for(repo: Path, branch: str, forge_module=forge_github) -> list[int] | str:
    return forge_module.find_open_prs(repo, branch)


def _phase_adds_nothing(repo: Path, phase_branch: str, default_branch: str) -> bool:
    """Edge. True when merging `phase_branch` into origin's `default_branch` changes nothing, so its work is already there."""
    ref = f"refs/remotes/origin/{default_branch}"
    fetched = bounded_run.completed(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{default_branch}:{ref}"], capture_output=True, text=True)
    tree = _git_out(repo, "merge-tree", "--write-tree", ref, phase_branch) if fetched.returncode == 0 else None
    return tree is not None and tree.splitlines()[0] == _git_out(repo, "rev-parse", f"{ref}^{{tree}}")


def _phase_already_merged(prs: list[dict] | str, branch_exists: bool, adds_nothing: bool) -> bool:
    """Pure. A PR of the phase's PR branch merged, none is open, and the surviving phase branch adds nothing over main; `prs` is a reason string when unread."""
    return branch_exists and adds_nothing and not isinstance(prs, str) and any(p.get("state") == "merged" for p in prs) and not any(p.get("state") == "open" for p in prs)


ALREADY_MERGED_PREFIX = "merge: already merged"


def already_merged_line(pr: dict) -> str:
    """Pure. The one line a phase land prints when it skips the merge because the PR is already merged. `chair_exec.landed` reads the prefix."""
    return f"{ALREADY_MERGED_PREFIX} (PR #{pr.get('number')})"


def _carried_files(items: list[dict], task_records: list[dict]) -> list[str]:
    """Pure. Sorted files of the approved tasks, from each record's `build.files_touched`; the items carry no files."""
    approved = {i["id"] for i in items if i.get("status") == "approved"}
    return sorted({f for r in task_records if r.get("task") in approved for f in (r.get("build") or {}).get("files_touched") or []})


def _main_subjects_touching(repo: Path, default_branch: str, branch: str, files: list[str]) -> list[str]:
    """Edge. Subjects, newest first, of commits on the fetched default branch that touched `files` and left them as `branch` holds them.
    A later commit that changed those files again does not match, so it cannot lend its PR number to this branch."""
    log = _git_out(repo, "log", "--max-count=20", "--format=%H %s", f"refs/remotes/origin/{default_branch}", "--", *files) if files else None
    same = lambda sha: bounded_run.completed(["git", "-C", str(repo), "diff", "--quiet", sha, branch, "--", *files], capture_output=True).returncode == 0  # noqa: E731
    return [line.split(" ", 1)[1] for line in (log or "").splitlines() if " " in line and same(line.split(" ", 1)[0])]


def _branch_has_own_commits(repo: Path, branch: str, default_branch: str) -> bool:
    """Edge. True when `branch` holds a commit the fetched default branch does not reach; a branch level with main holds none."""
    count = _git_out(repo, "rev-list", "--count", f"refs/remotes/origin/{default_branch}..{branch}")
    return count is not None and int(count) > 0


def _parent_merged_pr(repo: Path, initiative: str, parent: str | None, forge_module) -> int | None:
    """Edge. Number of the first merged PR of the parent phase's PR branch, None without a parent or a merged PR."""
    prs = forge.branch_prs(forge_module, repo, f"pr/{initiative}--{parent}") if parent else []
    return None if isinstance(prs, str) else next((p["number"] for p in prs if p.get("state") == "merged"), None)


def _phase_merged_pr(repo: Path, initiative: str, phase: str, default_branch: str, forge_module,
                     files: list[str], parent: str | None) -> dict | None:
    """Edge. The first merged PR of `pr/<initiative>--<phase>` when `_phase_already_merged` holds, else a `carried` dict
    for the PR that put an unclaimed, non-empty branch onto main (see `_main_subjects_touching`, `_parent_merged_pr`); None without a number.
    With no `files` and no `parent` there is nothing to find a carrier by, so only the own-PR case can hold."""
    branch = f"epic/{initiative}/{phase}"
    exists = _branch_exists(repo, f"refs/heads/{branch}")
    prs = forge.branch_prs(forge_module, repo, f"pr/{initiative}--{phase}")
    adds_nothing = exists and _phase_adds_nothing(repo, branch, default_branch)
    if _phase_already_merged(prs, exists, adds_nothing):
        return next(p for p in prs if p.get("state") == "merged")
    unclaimed = not isinstance(prs, str) and not any(p.get("state") in ("merged", "open") for p in prs)
    if not (unclaimed and adds_nothing and (files or parent) and _branch_has_own_commits(repo, branch, default_branch)):
        return None
    subjects = _main_subjects_touching(repo, default_branch, branch, files)
    named = any(land.pr_number_of_subject(s) is not None for s in subjects)
    number = land.carrying_pr(subjects, None if named else _parent_merged_pr(repo, initiative, parent, forge_module))
    return None if number is None else {"number": number, "state": "merged", "carried": True}


def _task_already_on_main(repo: Path, step: dict, record: dict | None) -> int | None:
    """Edge. The PR number that carried a task branch onto the default branch, or None to run the step as planned.
    Only the task form of `cherry_pick` (no `task` key) with files on its record qualifies; the branch must add
    nothing over `from` yet hold a commit the fetched default branch lacks, and a subject must name the PR."""
    files = ((record or {}).get("build") or {}).get("files_touched") or []
    if step.get("kind") != "cherry_pick" or "task" in step or not files:
        return None
    if not (_phase_adds_nothing(repo, step["branch"], step["from"]) and _branch_has_own_commits(repo, step["branch"], step["from"])):
        return None
    return land.carrying_pr(_main_subjects_touching(repo, step["from"], step["branch"], files), None)


def _carried_walk(steps: list[dict], planned: list[dict], number: int) -> tuple[list[dict], list[dict], str] | None:
    """Pure. The closing steps of `steps` and of `planned`, and the running `pr`, for a task main already carries.
    None when the gated steps keep no closing step, so the walk runs as planned."""
    tail = land.landed_tail(steps)
    return (tail, land.landed_tail(planned), f"#{number}") if tail else None


def _carried_pr(merged_pr: dict | None) -> str:
    """Pure. The running `pr` a walk starts from: `#<n>` for a carried phase, `""` otherwise."""
    return f"#{merged_pr['number']}" if merged_pr is not None and merged_pr.get("carried") else ""


def _merged_pr_ref(merged_pr: dict | None) -> str:
    """Pure. The `pr` a mark_done records for a merged PR: its url when the forge returned one, else `#<n>`, else `""`."""
    if merged_pr is None:
        return ""
    return str(merged_pr.get("url") or (f"#{merged_pr['number']}" if merged_pr.get("number") is not None else ""))


def _merged_line(merged_pr: dict) -> str:
    """Pure. The report line for a phase land that skips the merge: `landed via PR #n` when main carried it, else the already-merged line."""
    return land.landed_via_line(_carried_pr(merged_pr)) if merged_pr.get("carried") else already_merged_line(merged_pr)


def _phase_already_landed(repo: Path, initiative: str, phase: str, default_branch: str, forge_module) -> bool:
    """Edge. True when `_phase_merged_pr` finds a merged PR."""
    return _phase_merged_pr(repo, initiative, phase, default_branch, forge_module, [], None) is not None


def _phase_pr_facts(repo: Path, pr_branch: str, forge_module=forge_github) -> dict | str:
    """Edge. The plain facts `land.phase_resume` decides on, or the reason the open PRs could not be listed.
    The PR head is read as the fetched `origin/<pr_branch>` tip: the forge call returns PR numbers only,
    and a PR from this repo's own branch has exactly that head."""
    local = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{pr_branch}")
    fetched = bounded_run.completed(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{pr_branch}:refs/remotes/origin/{pr_branch}"],
                             capture_output=True, text=True).returncode == 0
    remote = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{pr_branch}") if fetched else None
    prs = _open_prs_for(repo, pr_branch, forge_module)
    if isinstance(prs, str):
        return prs
    return {"local_tip": local, "remote_tip": remote, "pr": prs[0] if prs else None, "pr_head": remote if prs else None}


def _land_resume(repo: Path, cherry_pick: dict, forge_module=forge_github) -> dict:
    """`land.resume_decision` for the `pr/<task>` branch a `cherry_pick` step
    would create. The expected tree is what cherry-picking the one commit onto
    `from` yields, computed by `git merge-tree` without touching the checkout.
    Open PRs are asked for whenever a branch exists; one is adopted whatever the trees."""
    branch, base = cherry_pick["onto"], cherry_pick["from"]
    local = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{tree}}")
    fetched = bounded_run.completed(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                             capture_output=True, text=True).returncode == 0
    remote = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{tree}}") if fetched else None
    if local is None and remote is None:
        return land.resume_decision("", None, None, [])
    prs = _open_prs_for(repo, branch, forge_module)
    if isinstance(prs, str):
        return {"kind": "refuse", "reason": prs}
    if prs:
        return land.resume_decision("", local, remote, prs)
    shas = (_git_out(repo, "rev-list", f"{base}..{cherry_pick['branch']}") or "").split()
    merged = _git_out(repo, "merge-tree", "--write-tree", f"--merge-base={shas[0]}^", base, shas[0]) if len(shas) == 1 else None
    expected = merged.splitlines()[0] if merged else None
    if expected is None:
        # Marked so `_land_leftover` can tell this refusal apart: only here does `land.leftover_gate` decide.
        return {"kind": "refuse", "reason": f"cannot compute the cherry-picked tree of {cherry_pick['branch']} onto {base}",
                "uncomputable_tree": True}
    if _git_out(repo, "cat-file", "-e", f"{expected}:.agent-generate") is not None:
        # The pushed tree is the one after generation and the amend, not the bare cherry-pick.
        expected, why = _generated_tree(repo, branch, base, shas[0], cherry_pick.get("umbrella"))
        if expected is None:
            return {"kind": "refuse", "reason": f"cannot compute the generated tree of {cherry_pick['branch']} onto {base}: {why}"}
    existing = [t for t in (local, remote) if t is not None]
    decision = land.resume_decision(expected, local, remote, [])
    if decision["kind"] != "refuse":
        return decision
    files = sorted({f for t in existing if t != expected
                    for f in (_git_out(repo, "diff-tree", "-r", "--name-only", expected, t) or "").split()})
    refused = {**decision, "reason": decision["reason"] + (f"; files differing: {', '.join(files)}" if files else "")}
    # Carried for `_land_leftover`, so it need not run the generator a second time.
    return {**refused, "local_tree": local, "remote_tree": remote, "expected_tree": expected} if any(t != expected for t in existing) else refused


def _remote_branch_exists(repo: Path, branch: str) -> bool:
    """Edge. Whether `origin` has `branch`. Fails closed: only a repo with no `origin`, or `ls-remote` exiting 2
    (reached the remote, no such ref), says absent. A failed fetch is not absence, and absence leads to a delete."""
    if _git_out(repo, "config", "--get", "remote.origin.url") is None:
        return False
    listed = bounded_run.completed(["git", "-C", str(repo), "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
                            capture_output=True, text=True)
    return listed.returncode != 2


def _branch_checked_out(repo: Path, branch: str) -> bool:
    """Edge. Whether any worktree has `branch` checked out. Its uncommitted state is not the land's to discard."""
    listing = _git_out(repo, "worktree", "list", "--porcelain")
    return listing is None or f"branch refs/heads/{branch}" in listing.splitlines()


def _open_pr_exists(repo: Path, branch: str, forge_module=forge_github) -> bool | str:
    """Edge. Whether an open pull request has `branch` as its head, or the reason the forge could not say."""
    prs = _open_prs_for(repo, branch, forge_module)
    return prs if isinstance(prs, str) else bool(prs)


def _leftover_gate_facts(repo: Path, branch: str, base: str, forge_module=forge_github) -> dict | None:
    """Edge, read-only. The keyword facts `land.leftover_gate` decides on for `branch` against `base`, or None when
    they cannot be read: the forge could not list open PRs, or a worktree has the branch checked out."""
    prs = _open_prs_for(repo, branch, forge_module)
    if isinstance(prs, str) or _branch_checked_out(repo, branch):
        return None
    local_tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    ancestor = (bounded_run.completed(["git", "-C", str(repo), "merge-base", "--is-ancestor", local_tip, base],
                               capture_output=True, text=True).returncode == 0) if local_tip else None
    count = _git_out(repo, "rev-list", "--count", f"{base}..{local_tip}") if local_tip else None
    return {"local_tip": local_tip, "is_ancestor_of_main": ancestor, "unique_commits": int(count) if count else None,
            "remote_exists": _remote_branch_exists(repo, branch), "open_pr_number": prs[0] if prs else None}


def _land_uncomputable(repo: Path, cherry_pick: dict, decision: dict, forge_module=forge_github) -> dict:
    """Edge, read-only. `land.leftover_gate` over the `_land_resume` refusal that could not compute the cherry-picked
    tree. A `recreate` becomes a `fresh` carrying `old_tip` and the refusal's reason, for `_land_recreate` to act on
    under the repo lease; a `refuse` carries the gate's reason; no decision, or unreadable facts, leave it unchanged."""
    branch = cherry_pick["onto"]
    facts = _leftover_gate_facts(repo, branch, cherry_pick["from"], forge_module)
    gated = land.leftover_gate(branch, **facts) if facts is not None else None
    if gated is None:
        return decision
    if gated["kind"] == "refuse":
        return {"kind": "refuse", "reason": gated["reason"]}
    return {"kind": "fresh", "old_tip": gated["old_tip"], "reason": decision["reason"]}


def _land_leftover(repo: Path, cherry_pick: dict, decision: dict, forge_module=forge_github) -> dict:
    """Edge, read-only. A `_land_resume` refusal over a `pr/<initiative>--<phase>` whose local or pushed tree differs,
    that has no open PR and that no worktree has checked out, is a leftover. `land.leftover_branch_decision` then
    turns it into a `fresh` carrying its `backup_refs`, both tips and the refusal's reason, for `_land_rebuild` to
    act on under the repo lease. The refusal that could not compute the tree goes to `_land_uncomputable` instead.
    Any other decision, or a remote the fetch and `ls-remote` disagree about, comes back unchanged."""
    if decision["kind"] == "refuse" and decision.get("uncomputable_tree"):
        return _land_uncomputable(repo, cherry_pick, decision, forge_module)
    if decision["kind"] != "refuse" or "expected_tree" not in decision:
        return decision
    branch = cherry_pick["onto"]
    open_pr = _open_pr_exists(repo, branch, forge_module)
    local_tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    remote_tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}") if decision["remote_tree"] else None
    remote_exists = _remote_branch_exists(repo, branch)
    if (isinstance(open_pr, str) or (local_tip is None) != (decision["local_tree"] is None)
            or remote_exists != (remote_tip is not None) or _branch_checked_out(repo, branch)):
        return decision
    leftover = land.leftover_branch_decision(branch, remote_exists, open_pr, decision["local_tree"], decision["expected_tree"],
                                             local_commit=local_tip, remote_commit=remote_tip, remote_tree=decision["remote_tree"])
    if leftover["kind"] != "back_up_and_rebuild":
        return decision
    return {**leftover, "kind": "fresh", "reason": decision["reason"]}


def _git_failure(repo: Path, *args: str) -> str | None:
    """Edge. Run a git command that writes; its stderr when it exits non-zero, else None."""
    done = bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return None if done.returncode == 0 else done.stderr.strip() or f"git {args[0]} exited {done.returncode}"


def _land_rebuild(repo: Path, branch: str, rebuild: dict) -> str | None:
    """Edge. Point each `backup_refs` ref at its tip and push it to origin, a rerun finding the same ref already
    there. Only once every backup is safe, delete the remote `branch` if it still sits at `remote_tip`, then the
    local one if it still sits at `local_tip`. Runs under the repo lease. The reason it stopped, or None."""
    if _branch_checked_out(repo, branch):
        return f"{branch} is now checked out in a worktree"
    has_origin = _git_out(repo, "config", "--get", "remote.origin.url") is not None
    for ref, tip in rebuild["backup_refs"].items():
        failure = _git_failure(repo, "update-ref", ref, tip) or (_git_failure(repo, "push", "--quiet", "origin", f"{ref}:{ref}") if has_origin else None)
        if failure:
            return f"could not back up to {ref}: {failure}"
    local_tip, remote_tip = rebuild["local_tip"], rebuild["remote_tip"]
    if local_tip and _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}") != local_tip:
        return f"{branch} moved off {local_tip[:8]}"
    if remote_tip:
        gone = _git_failure(repo, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{remote_tip}", "origin", f":refs/heads/{branch}")
        if gone:
            return f"could not delete origin/{branch} at {remote_tip[:8]}: {gone}"
    dropped = _git_failure(repo, "update-ref", "-d", f"refs/heads/{branch}", local_tip) if local_tip else None
    return f"{branch} moved off {local_tip[:8]}: {dropped}" if dropped else None


def _land_recreate(repo: Path, branch: str, old_tip: str) -> str | None:
    """Edge. Delete the local `branch` if it still sits at `old_tip`; one already gone is fine. Runs under the repo
    lease. Touches no remote and writes no backup ref. The reason it stopped, or None."""
    if _branch_checked_out(repo, branch):
        return f"{branch} is now checked out in a worktree"
    tip = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if tip is None:
        return None
    if tip != old_tip:
        return f"{branch} moved off {old_tip[:8]}"
    dropped = _git_failure(repo, "update-ref", "-d", f"refs/heads/{branch}", old_tip)
    return f"{branch} moved off {old_tip[:8]}: {dropped}" if dropped else None


def _land_enrich(steps: list[dict], *, path: str, worktree_root: str, task_paths: dict[str, str] | None = None,
                  item_path: str | None = None, workspace: str | None = None,
                  item_id: str | None = None, profile: str | None = None,
                  runs_dir: str | None = None, umbrella: str | None = None,
                  task_items: dict[str, str] | None = None, store_runs_dir: str | None = None,
                  check_paths: bool = False) -> list[dict]:
    """Steps enriched with what only the edge knows: each `mark_done`'s own
    record file path (`task_paths` maps task name to path in phase mode), and
    the configured worktree root for `clean`/`clean_phase`. `item_path` (task
    mode only) is the work item `mark_done` will also try to close out;
    `task_items` (phase mode) is the same, keyed by task name, since a phase
    closes one item per task rather than a single item. A
    `checks` step that names a `branch` (phase mode) gets no filesystem write
    here — a dry run must stay read-only, so the actual worktree is only ever
    created at execution time, inside `_execute_land_step`, under `--apply`.
    A `route_sync` step gets the `workspace` to sync, the item's own `id`
    (`item_id`, else the task name the plan used), and the land's `profile`
    and `runs_dir`, so the step reads the tracker from where the plan did.
    A `cherry_pick` or `squash_phase` step gets the profile's `umbrella_dir`
    as `umbrella` when set, for `.agent-generate`. A `mark_done` with `recorded` False has no record file: it
    gets its store item and `store_runs_dir` instead of a `path`. With `check_paths`, a `mark_done` whose path is
    not a file becomes that store-only step when it has an item, else a `refuse` step naming the task and the path."""
    def enrich(step: dict) -> dict:
        if step["kind"] in ("cherry_pick", "squash_phase") and umbrella is not None:
            return {**step, "umbrella": umbrella}
        if step["kind"] == "route_sync":
            return {**step, "item": item_id or step["item"], "workspace": workspace,
                    "profile": profile, "runs_dir": runs_dir}
        if step["kind"] == "fetch":
            return {**step, "profile": profile, "runs_dir": runs_dir}
        if step["kind"] == "mark_done" and step.get("recorded") is False:
            item = (task_items or {}).get(step["task"])
            return {**step, "item": item, "from": "approved", "to": "done", "runs_dir": store_runs_dir}
        if step["kind"] == "mark_done":
            resolved = (task_paths or {}).get(step["task"], path)
            item = item_path or (task_items or {}).get(step["task"])
            if check_paths and not Path(resolved).is_file():  # a phase record that lives only in the store
                if item is None:
                    return {"kind": "refuse", "reason": f"{step['task']}: record {resolved} does not exist and the task has no approved item; nothing merged"}
                return {**step, "recorded": False, "item": item, "from": "approved", "to": "done", "runs_dir": store_runs_dir}
            marked = {**step, "path": resolved}
            return {**marked, "item": item, "from": "approved", "to": "done"} if item else marked
        if step["kind"] == "mark_merged":
            return {**step, "path": (task_paths or {}).get(step["task"], path), "runs_dir": store_runs_dir,
                    "item": item_path or (task_items or {}).get(step["task"])}
        if step["kind"] == "phase_done":
            return {**step, "runs_dir": runs_dir or store_runs_dir}
        if step["kind"] in ("clean", "clean_phase"):
            return {**step, "worktree_root": worktree_root}
        return step
    return [enrich(s) for s in steps]


def _with_check_cap(steps: list[dict], cores: int, capacity: int | None) -> list[dict]:
    """Each `checks` step carrying the host's `cores` and the lane `capacity`, for `_execute_land_step` to run its checks with."""
    return [{**s, "cores": cores, "capacity": capacity} if s["kind"] == "checks" else s for s in steps]


def _backfill_phase_task_files(runs_dir: Path, run_id: str, phase: str) -> None:
    """Store mode only: write any task record the store holds for this run and phase, then for each earlier run of
    the same initiative (newest first), that has no file yet under `runs/<run>/tasks/<phase>/`, so
    `_land_phase_record`'s own directory read finds it. Never overwrites a file already on disk."""
    phase_dir = runs_dir / run_id / "tasks" / phase
    for source_run in (run_id, *_earlier_runs(run_id)):
        for task_id in run_store.run_task_ids(runs_dir, source_run):
            path = phase_dir / f"{task_id}.json"
            if path.exists():
                continue
            record = run_store.task_record(runs_dir, source_run, phase, task_id)
            if record is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**record, "run": source_run}), encoding="utf-8")


def _earlier_runs(run_id: str) -> list[str]:
    """`<initiative>-<n>`'s earlier runs, newest first: a task approved in run 2 and still unlanded when run 3 lands
    the phase has its record only under run 2 (2026-10-06, the-store-holds-every-initiative-and-ticket). Tagging the
    written record with its own run keeps the gather on `agents/<that run>/<task>`. Empty for an unnumbered id."""
    prefix, _, number = run_id.rpartition("-")
    return [f"{prefix}-{n}" for n in range(int(number) - 1, 0, -1)] if prefix and number.isdigit() else []


def _land_phase_record(runs_dir: Path, run_id: str, phase: str) -> tuple[dict | None, list[dict], dict[str, str], str]:
    """The phase record at `runs/<run>:<phase>.json` and every task record
    filed under it, keyed by task name for `mark_done`'s own file path."""
    phase_path = runs_dir / f"{run_id}:{phase}.json"
    phase_record = next((m for m in run_store.phase_manifests(runs_dir, run_id) if m.get("run_id") == f"{run_id}:{phase}"), None)
    if phase_record is None and phase_path.exists():  # a file whose body carries no run_id
        phase_record = json.loads(phase_path.read_text(encoding="utf-8"))
    if phase_record is None:
        return None, [], {}, str(phase_path.resolve())
    phase_record.setdefault("run", run_id)
    phase_record.setdefault("phase", phase)
    task_records, task_paths = [], {}
    for p in sorted((runs_dir / run_id / "tasks" / phase).glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        r.setdefault("run", run_id); r.setdefault("task", p.stem); r.setdefault("phase", phase)
        task_records.append(r)
        task_paths[r["task"]] = str(p)
    for p in _land_phase_fallback_paths(runs_dir, run_id, phase):
        if p.stem in task_paths:
            continue
        r = json.loads(p.read_text(encoding="utf-8"))
        r.setdefault("task", p.stem); r.setdefault("phase", phase); r.setdefault("run", p.parent.parent.parent.name)
        task_records.append(r)
        task_paths[p.stem] = str(p)
    return phase_record, task_records, task_paths, str(phase_path)


def _land_phase_fallback_paths(runs_dir: Path, run_id: str, phase: str) -> list[Path]:
    """Every earlier run of `run_id`'s own initiative that carries a `tasks/<phase>/` directory,
    newest first, so the caller's own-run-wins, newest-earlier-run-wins search order falls out of
    a plain skip-if-already-seen loop over this list."""
    m = re.search(r"-(\d+)$", run_id)
    if m is None:
        return []
    initiative, own_n = chair_facts.run_initiative(run_id), int(m.group(1))
    sibling_re = re.compile(rf"{re.escape(initiative)}-(\d+)$")
    matches = (sibling_re.fullmatch(d.name) for d in runs_dir.iterdir() if d.is_dir()) if runs_dir.is_dir() else []
    earlier_ns = sorted({int(sm.group(1)) for sm in matches if sm and int(sm.group(1)) < own_n}, reverse=True)
    paths = []
    for n in earlier_ns:
        paths.extend(sorted((runs_dir / f"{initiative}-{n}" / "tasks" / phase).glob("*.json")))
    return paths


def _phase_items(work_root: Path, initiative: str, phase: str, runs_dir: Path | None = None,
                  land_mode: str = "files") -> tuple[list[dict] | None, str]:
    """The phase's own tickets from the work store (`work/<initiative>/<phase>/*.md`), each
    carrying its own `file` path for `mark_done` to close out, or `None` with the directory
    searched when there is nothing there to read — a phase can never be waved through by a
    missing ticket file. `status` comes from `route.work_item`'s parsed `state` in files mode;
    in store mode it comes from the store's own `work_items` row instead (`run_store.task_state_of`),
    `None` when the store holds no row for that task id, which `land.phase_landable` already
    refuses by name."""
    phase_dir = work_root / initiative / phase
    paths = sorted(phase_dir.glob("*.md")) if phase_dir.is_dir() else []
    if not paths:
        return None, str(phase_dir)
    items = []
    for p in paths:
        text = _read_text_or_none(p)
        if text is None:
            continue
        fields = route.parse_frontmatter(text)[0]
        item = route.work_item(fields, initiative=initiative, phase_dir=phase, stem=p.stem)
        status = run_store.task_state_of(runs_dir, initiative, item["id"]) if land_mode == "store" else item["state"]
        items.append({"id": item["id"], "status": status, "file": str(p)})
    return items, str(phase_dir)


def _natural_key(name: str) -> list[int | str]:
    """Pure. Sort key that orders `phase-2` before `phase-10`."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _sole_parent(phase_of: dict[str, str], needs: dict[str, list[str]], phase: str) -> str | None:
    """Pure. The one other phase that tasks of `phase` need tasks of, whatever those tasks' state; None for none or several.
    `waves.phase_parents` drops done needs, and a parent that already landed has all of its tasks done."""
    parents = {phase_of[n] for t, ns in needs.items() if phase_of.get(t) == phase for n in ns if n in phase_of} - {phase}
    return next(iter(parents)) if len(parents) == 1 else None


def _parent_phase(work_root: Path, initiative: str, phase: str) -> str | None:
    """Edge. The phase `phase` stacks on, read from the `needs` of its tickets; see `_sole_parent`."""
    root = work_root / initiative
    paths = sorted(root.glob("*/*.md")) if root.is_dir() else []
    items = [route.work_item(route.parse_frontmatter(text)[0], initiative=initiative, phase_dir=p.parent.name, stem=p.stem)
             for p in paths if (text := _read_text_or_none(p)) is not None]
    return _sole_parent({str(i["id"]): str(i["phase"]) for i in items}, {str(i["id"]): [str(n) for n in i["needs"] or []] for i in items}, phase)


def _initiative_land_facts(work_root: Path, initiative: str, runs_dir: Path, land_mode: str) -> tuple[dict[str, str], dict[str, list[str]], set[str]]:
    """Edge. Every ticket's phase and needs across the initiative, as `_parent_phase` reads them, and the ids whose
    state is done or dropped: from the store's `work_items` rows in store mode, the ticket files otherwise."""
    root = work_root / initiative
    paths = sorted(root.glob("*/*.md")) if root.is_dir() else []
    items = [route.work_item(route.parse_frontmatter(text)[0], initiative=initiative, phase_dir=p.parent.name, stem=p.stem)
             for p in paths if (text := _read_text_or_none(p)) is not None]
    rows = run_store.work_items(runs_dir, initiative) if land_mode == "store" else []
    state = {str(i["id"]): (run_store.task_state(rows, initiative, str(i["id"])) if land_mode == "store" else i["state"]) for i in items}
    return ({str(i["id"]): str(i["phase"]) for i in items}, {str(i["id"]): [str(n) for n in i["needs"] or []] for i in items},
            {t for t, s in state.items() if s in ("done", "dropped")})


def _parent_land_refusal(tasks: Sequence[str], facts: tuple[dict[str, str], dict[str, list[str]], set[str]],
                         carried: Collection[str] = ()) -> list[dict] | None:
    """Pure. The one `refuse` step for a land of `tasks` whose parent phase has not landed, else None; see `land.parent_land_wait`."""
    phase_of, needs, landed = facts
    wait = land.parent_land_wait(tasks, phase_of, needs, landed, carried)
    return None if wait is None else [{"kind": "refuse", "reason": wait}]


def _land_parent_refused(a: argparse.Namespace, steps: list[dict]) -> int:
    """Edge. Report a parent-land refusal the way the land reports its others: the step listing on a dry run, a
    `land: refusing` line on apply. Exit 2."""
    print(f"land: refusing, {steps[0]['reason']}" if a.apply else json.dumps(steps, indent=2))
    return 2


def _ticket_items(work_root: Path, initiative: str, runs_dir: Path, land_mode: str, current: tuple[str, list[dict]]) -> tuple[dict[str, str], list[str]]:
    """Approved task id to ticket path across every phase of the initiative (`current` is already read), and the phases in natural order."""
    root = work_root / initiative
    phases = sorted((d.name for d in root.iterdir() if d.is_dir()), key=_natural_key) if root.is_dir() else []
    found = [i for p in phases for i in (current[1] if p == current[0] else _phase_items(work_root, initiative, p, runs_dir, land_mode)[0] or [])]
    return {i["id"]: i["file"] for i in found if i["status"] == "approved"}, phases


def _ticket_present(repo: Path, head: str, records: list[dict]) -> set[str]:
    """Edge. Ids of `records` whose work is in `head`: a `Task: <id>` trailer on it, else the task's agent branch an ancestor of it."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return bounded_run.completed(["git", "-C", str(repo), *args], capture_output=True, text=True)
    log = git("log", "--format=%B", head)
    trailers = set(re.findall(r"^Task: (.+)$", log.stdout, re.M)) if log.returncode == 0 else set()
    return {r["task"] for r in records if r["task"] in trailers or git("merge-base", "--is-ancestor", f"agents/{r.get('run')}/{r['task']}", head).returncode == 0}


def _phase_task_items(items: list[dict]) -> dict[str, str]:
    """Task id to ticket path, `approved` items only: `mark_done` moves an item approved to done, and a
    `done` item that `phase_landable` also admits would be refused by the store's `--expect approved`."""
    return {i["id"]: i["file"] for i in items if i["status"] == "approved"}


def _phase_needing_land(runs_dir: Path, run_id: str) -> str | None:
    """The one phase under `runs/<run_id>/tasks/*/` holding more than one
    task record — phase mode's default trigger when neither `--phase` nor
    `--task` is given. `None` when no single phase qualifies, leaving task
    mode's own record count to speak (including its "found 0" refusal)."""
    tasks_root = runs_dir / run_id / "tasks"
    if not tasks_root.is_dir():
        return None
    candidates = [d.name for d in tasks_root.iterdir() if d.is_dir() and len(list(d.glob("*.json"))) > 1]
    return candidates[0] if len(candidates) == 1 else None


def _last_phase_with_manifest(runs_dir: Path, run_id: str) -> str | None:
    """Edge. The last phase, by name, that holds task records under `run_id` and has a phase record; None when none does."""
    tasks_root = runs_dir / run_id / "tasks"
    held = sorted(d.name for d in tasks_root.iterdir() if d.is_dir() and any(d.glob("*.json"))) if tasks_root.is_dir() else []
    return next((p for p in reversed(held) if _land_phase_record(runs_dir, run_id, p)[0] is not None), None)
