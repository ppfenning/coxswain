"""Git for the landing model: the initiative branch, the phase merge into it, and its refresh onto the parent.

One rule set at every level: a refresh is a forward merge of the parent into the child, and nothing here rebases,
resets or force-moves a branch the landing record names. Every merge is built on a detached scratch worktree, so no
working checkout is touched, and the branch is moved last, with a compare-and-swap on its old tip.

The guard: a branch is never moved to a tip that lacks a sha the record says is on it.

A conflict is aborted and named, unless the caller gives `merge_into` a resolver (the new model's merge agent): the
conflicted merge is then left in its scratch worktree, markers and all, for the resolver to answer with each file's
merged text. What it answers is committed there and meets the same guard and checks before the branch moves.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from harness import bounded_proc
from harness.worktree import registry_lock

# Mechanical commits: the lander joins branches, it never authors.
IDENTITY = ("-c", "user.email=lander@invalid", "-c", "user.name=lander")
# Never a recorded resolution: a conflict the merge agent settled once must not settle the same conflict silently on
# another merge (an old-model phase that must go to a rectify task, a new-model one the agent must see).
NO_RERERE = ("-c", "rerere.enabled=false")

Checks = Callable[[Path], tuple[bool, str]]
_MARKERS = ("<<<<<<< ", ">>>>>>> ", "<<<<<<<\n", ">>>>>>>\n")


@dataclass(frozen=True)
class Resolved:
    """A resolver's answer to a conflicted merge: each conflicted file's whole merged text, and who resolved it."""

    files: Mapping[str, str]
    note: str


# The scratch worktree holding the conflicted merge, and the conflicted paths -> the merged files, or why not.
Resolve = Callable[[Path, tuple[str, ...]], Resolved | str]
# Why a resolver left a conflict unsettled: it said no, or its answer did not settle it (`refused`); it raised
# (`raised`); or the harness could not commit what it answered (`fault`). "" when no resolver was asked.
Unsettled = Literal["", "refused", "raised", "fault"]


@dataclass(frozen=True)
class Merge:
    """The outcome of one merge onto a branch. `status` is merged, up_to_date, conflict, checks_failed, guard, busy (another
    worker holds the branch lease) or error."""

    status: str
    tip: str = ""  # the branch's tip afterwards (unchanged unless merged)
    merged: str = ""  # the sha that was merged in
    paths: tuple[str, ...] = field(default_factory=tuple)  # conflicted paths
    detail: str = ""
    resolved_by: str = ""  # a conflict a resolver settled: its note; "" for a clean merge
    unsettled: Unsettled = ""  # a conflict a resolver was asked about and did not settle: why (`Unsettled`)

    @property
    def ok(self) -> bool:
        return self.status in ("merged", "up_to_date")


def git(repo: Path | str, *args: str) -> tuple[bool, str]:
    """Run git in `repo`. (ok, stdout stripped, or stderr when it failed)."""
    done = bounded_proc.completed(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr or done.stdout).strip()


def rev(repo: Path | str, ref: str) -> str | None:
    ok, out = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out if ok and out else None


def branch_ref(repo: Path | str, branch: str) -> str | None:
    """`branch` as a ref git can read: the local branch, else its copy on origin, else None."""
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if rev(repo, ref):
            return ref
    return None


def is_ancestor(repo: Path | str, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer)[0]


def missing_from(repo: Path | str, tip: str, shas: Sequence[str]) -> list[str]:
    """The recorded shas that `tip` does not hold: what a move to `tip` would drop."""
    return [sha for sha in shas if sha and not is_ancestor(repo, sha, tip)]


def ensure_branch(repo: Path | str, branch: str, parent_ref: str) -> tuple[str | None, bool, str]:
    """Cut `branch` at `parent_ref` when it does not exist. (its tip, whether this call created it, why not).

    A branch that exists only on origin is brought local at that tip. An existing branch is never moved."""
    local = rev(repo, f"refs/heads/{branch}")
    if local:
        return local, False, ""
    remote = rev(repo, f"refs/remotes/origin/{branch}")
    start = remote or rev(repo, parent_ref)
    if start is None:
        return None, False, f"cannot cut {branch}: {parent_ref} is not a commit"
    ok, out = git(repo, "branch", "--no-track", branch, start)
    if not ok:
        return None, False, f"cannot cut {branch}: {out}"
    return start, remote is None, ""


def _scratch(repo: Path | str, at: str) -> tuple[Path | None, str]:
    path = Path(tempfile.mkdtemp(prefix="lander-"))
    with registry_lock(repo):
        ok, out = git(repo, "worktree", "add", "--detach", str(path), at)
    return (path, "") if ok else (None, out)


def _drop(repo: Path | str, path: Path) -> None:
    with registry_lock(repo):
        git(repo, "worktree", "remove", "--force", str(path))


def _move(repo: Path | str, branch: str, new: str, old: str) -> tuple[bool, str]:
    """Move a local branch from `old` to `new`, and only then: the compare-and-swap that makes two mergers safe."""
    return git(repo, "update-ref", "-m", "lander: merge", f"refs/heads/{branch}", new, old)


def marked(text: str) -> bool:
    """Pure. Whether `text` still holds a conflict marker line (`<<<<<<< ` or `>>>>>>> ` at a line's start)."""
    return any(line.startswith(_MARKERS) or line in ("<<<<<<<", ">>>>>>>") for line in text.splitlines(keepends=True))


def answer_problem(files: Mapping[str, str], conflicted: Sequence[str]) -> str | None:
    """Pure. Why a resolver's files do not settle the conflict (a conflicted file left out, one named that did not
    conflict, a marker left in), or None when they do."""
    if missing := sorted(set(conflicted) - set(files)):
        return f"the resolution left out {', '.join(missing)}"
    if extra := sorted(set(files) - set(conflicted)):
        return f"the resolution named files that do not conflict: {', '.join(extra)}"
    if left := sorted(p for p, text in files.items() if marked(text)):
        return f"the resolution left conflict markers in {', '.join(left)}"
    return None


def strays(conflicted: Sequence[str], before: Mapping[str, str], after: Mapping[str, str],
           modified: Sequence[str]) -> list[str]:
    """Pure. The paths other than `conflicted` whose index entry changed between `before` and `after` (path -> its
    `ls-files --stage` entries), or that are modified in the worktree: what a resolver touched that it was not asked
    about. Empty when only the conflicted paths moved."""
    mine = set(conflicted)
    staged = {p for p in set(before) | set(after) if p not in mine and before.get(p) != after.get(p)}
    return sorted(staged | {p for p in modified if p not in mine})


def _index(scratch: Path) -> tuple[dict[str, str], list[str]] | None:
    """Edge. The scratch's index (path -> its stage entries) and the tracked paths modified in its worktree."""
    listed, entries = git(scratch, "ls-files", "--stage", "-z")
    diffed, modified = git(scratch, "diff", "--name-only", "-z")
    if not (listed and diffed):
        return None
    index: dict[str, str] = {}
    for entry in entries.split("\0"):
        meta, tab, path = entry.partition("\t")
        if tab:
            index[path] = f"{index.get(path, '')}{meta};"
    return index, [p for p in modified.split("\0") if p]


def _merging(scratch: Path) -> tuple[str | None, str | None]:
    return rev(scratch, "HEAD"), rev(scratch, "MERGE_HEAD")


def _stages(scratch: Path, path: str) -> tuple[str, str, str]:
    """Edge. An unmerged file's text at index stages 1 (the common base), 2 (the branch) and 3 (the source); "" for a
    stage it has none of (a file one side added or deleted)."""
    def stage(n: int) -> str:  # not `git`: it strips, and a first line's indent is part of the line
        done = bounded_proc.completed(["git", "-C", str(scratch), "show", f":{n}:{path}"], capture_output=True,
                                      text=True, errors="replace")
        return done.stdout if done.returncode == 0 else ""
    return stage(1), stage(2), stage(3)


def guard_problem(files: Mapping[str, str], stages: Mapping[str, tuple[str, str, str]]) -> str | None:
    """Pure. L5's guards on a resolver's merged files (`lane_merge.has_markers`, `lane_merge.lost_lines`): a conflict
    marker left in, the diff3 style's `|||||||` included, or a line either side added (each file's index stages: base,
    branch, source) that the merged text dropped. None when every file passes both."""
    from harness.lane_merge import has_markers, lost_lines  # lane_merge reaches this module through epic_branches

    if marked_in := sorted(p for p, text in files.items() if has_markers(text)):
        return f"the resolution left conflict markers in {', '.join(marked_in)}"
    lost = {p: lost_lines(*stages[p], text) for p, text in files.items() if p in stages}
    if dropped := sorted(p for p, lines in lost.items() if lines):
        return (f"the resolution dropped lines a side added in {', '.join(dropped)}: "
                + "; ".join(f"{p}: {', '.join(repr(line) for line in lost[p][:3])}" for p in dropped))[:400]
    return None


def _settle(scratch: Path, paths: tuple[str, ...], resolve: Resolve) -> tuple[str | None, str, Unsettled]:
    """Edge. Ask `resolve` about the conflicted merge in `scratch`, then write and commit its answer there.
    (its note, "", "") once committed; (None, why not, `Unsettled`) when it did not settle the conflict or could not
    be committed. The answer must pass L5's guards against the conflict's index stages (`guard_problem`), read before
    the resolver is asked. Before the commit the scratch must still be the merge the resolver was given, and its index
    and worktree must differ from that merge only in the conflicted paths: anything else it staged or changed is
    refused."""
    before, merging = _index(scratch), _merging(scratch)
    if before is None:
        return None, "the conflicted merge's index could not be read", "fault"
    stages = {p: _stages(scratch, p) for p in paths}
    try:
        answer = resolve(scratch, paths)
    except Exception as exc:  # a resolver that breaks leaves a conflict, never a half-merged scratch
        return None, f"the resolver failed: {type(exc).__name__}: {exc}", "raised"
    if isinstance(answer, str):
        return None, answer, "refused"
    if problem := answer_problem(answer.files, paths) or guard_problem(answer.files, stages):
        return None, problem, "refused"
    if _merging(scratch) != merging:
        return None, "the resolver moved the merge it was given (HEAD or MERGE_HEAD changed); refused", "refused"
    for path, text in answer.files.items():
        (scratch / path).write_text(text, encoding="utf-8")
    added, out = git(scratch, "add", "--", *paths)
    if not added:
        return None, f"the resolved merge could not be staged: {out}", "fault"
    after = _index(scratch)
    if after is None:
        return None, "the resolved merge's index could not be read", "fault"
    if touched := strays(paths, before[0], after[0], after[1]):
        return None, f"the resolution touched files that did not conflict: {', '.join(touched)}; refused", "refused"
    committed, out = git(scratch, *IDENTITY, *NO_RERERE, "commit", "--no-verify", "--no-edit")
    return (answer.note or "resolved", "", "") if committed else (None, f"the resolved merge could not be committed: {out}", "fault")


def merge_into(repo: Path | str, *, branch: str, source: str, message: str, recorded: Sequence[str] = (),
               checks: Checks | None = None, resolve: Resolve | None = None) -> Merge:
    """Merge `source` into local `branch` with a merge commit, on a scratch worktree, then move the branch.

    `source` already on the branch is `up_to_date`. A conflict aborts and names the paths. `checks` runs on the merged
    tree before the branch moves; a failure leaves the branch where it was. `recorded` are the shas the landing
    record says the branch holds: a merge that would leave one out is refused (`guard`) and the branch is unchanged.

    With `resolve`, a conflict is first left in the scratch for it (`Resolve`): its answer is committed as the merge,
    and meets the guard and `checks` like any merge (`resolved_by` says who settled it). A resolver that cannot settle
    it leaves a `conflict`, its reason in the detail."""
    old = rev(repo, f"refs/heads/{branch}")
    incoming = rev(repo, source)
    if old is None:
        return Merge("error", detail=f"no local branch {branch}")
    if incoming is None:
        return Merge("error", tip=old, detail=f"{source} is not a commit")
    if is_ancestor(repo, incoming, old):
        return Merge("up_to_date", tip=old, merged=incoming)
    scratch, why = _scratch(repo, old)
    if scratch is None:
        return Merge("error", tip=old, merged=incoming, detail=why)
    try:
        ok, out = git(scratch, *IDENTITY, *NO_RERERE, "merge", "--no-ff", "--no-edit", "-m", message, incoming)
        resolved_by = ""
        if not ok:
            _, unmerged = git(scratch, "diff", "--name-only", "--diff-filter=U")
            paths = tuple(p for p in unmerged.splitlines() if p)
            unsettled: Unsettled = ""
            note, why = None, ""
            if paths and resolve is not None:
                note, why, unsettled = _settle(scratch, paths, resolve)
            if note is None:
                git(scratch, "merge", "--abort")
                if paths:
                    return Merge("conflict", tip=old, merged=incoming, paths=paths, unsettled=unsettled,
                                 detail=f"conflict in {', '.join(paths)}" + (f": {why}" if why else ""))
                return Merge("error", tip=old, merged=incoming, detail=out)
            resolved_by = note
        new = rev(scratch, "HEAD")
        if new is None:
            return Merge("error", tip=old, merged=incoming, detail="the merge left no commit")
        dropped = missing_from(repo, new, recorded)
        if dropped:
            return Merge("guard", tip=old, merged=incoming,
                         detail=f"refused: the merge would drop recorded commit(s) {', '.join(s[:12] for s in dropped)}")
        if checks is not None:
            passed, output = checks(scratch)
            if not passed:
                return Merge("checks_failed", tip=old, merged=incoming, detail=output)
        moved, out = _move(repo, branch, new, old)
        if not moved:
            return Merge("error", tip=old, merged=incoming, detail=f"{branch} moved while it was being merged: {out}")
        return Merge("merged", tip=new, merged=incoming, resolved_by=resolved_by)
    finally:
        _drop(repo, scratch)


def conflict_commit(repo: Path | str, *, branch: str, source: str, message: str) -> Merge:
    """Commit the conflicted merge of `source` into local `branch`, conflict markers and all, and move the branch.

    This is the base a rectify task builds on: its builder returns a patch, so the conflict has to be in the tree it
    starts from. The branch then holds `source`, so once the markers are resolved the merge the other way is clean."""
    old = rev(repo, f"refs/heads/{branch}")
    incoming = rev(repo, source)
    if old is None or incoming is None:
        return Merge("error", tip=old or "", detail=f"no local branch {branch}" if old is None else f"{source} is not a commit")
    scratch, why = _scratch(repo, old)
    if scratch is None:
        return Merge("error", tip=old, merged=incoming, detail=why)
    try:
        ok, _ = git(scratch, *IDENTITY, *NO_RERERE, "merge", "--no-ff", "--no-edit", "-m", message, incoming)
        _, unmerged = git(scratch, "diff", "--name-only", "--diff-filter=U")
        paths = tuple(p for p in unmerged.splitlines() if p)
        if not ok:
            added, out = git(scratch, "add", "--all")
            committed, out = git(scratch, *IDENTITY, "commit", "--no-verify", "-m", message) if added else (False, out)
            if not committed:
                git(scratch, "merge", "--abort")
                return Merge("error", tip=old, merged=incoming, detail=out)
        new = rev(scratch, "HEAD")
        if new is None or new == old:
            return Merge("up_to_date", tip=old, merged=incoming)
        moved, out = _move(repo, branch, new, old)
        if not moved:
            return Merge("error", tip=old, merged=incoming, detail=out)
        return Merge("conflict" if paths else "merged", tip=new, merged=incoming, paths=paths)
    finally:
        _drop(repo, scratch)


def ahead_behind(repo: Path | str, branch: str, parent_ref: str) -> tuple[int, int] | None:
    """(commits `branch` has over the parent, commits it lacks), None when either is not a commit."""
    ok, out = git(repo, "rev-list", "--left-right", "--count", f"{branch}...{parent_ref}")
    parts = out.split()
    return (int(parts[0]), int(parts[1])) if ok and len(parts) == 2 else None


def safe_to_delete(repo: Path | str, branch: str, kept_tip: str, recorded: Sequence[str]) -> list[str]:
    """The recorded shas only `branch` still holds: deleting it would lose them. Empty means it may go."""
    tip = rev(repo, f"refs/heads/{branch}")
    if tip is None:
        return []
    return [sha for sha in recorded if sha and is_ancestor(repo, sha, tip) and not is_ancestor(repo, sha, kept_tip)]


def on_scratch(repo: Path | str, at: str, check: Checks) -> tuple[bool, str]:
    """Run `check` on a detached scratch worktree of `at`, and remove the worktree whatever happens."""
    scratch, why = _scratch(repo, at)
    if scratch is None:
        return False, why
    try:
        return check(scratch)
    finally:
        _drop(repo, scratch)
