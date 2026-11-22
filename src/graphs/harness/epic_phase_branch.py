"""The epic driver, a phase's branch: its worktree, the refs it needs, what is unlanded or blocked, and rebases."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path

from core.landing import need_met
from harness.assembly import assemble
from harness.checks import tail_lines
from harness.cos import pid_alive
from harness.epic_context import Ctx, git_in
from harness.epic_types import PhaseRecord, Quarantine, as_list, as_mapping
from harness.phase_remote import push_branch
from harness.stack_refresh import guarded_refresh, refresh_record
from harness.worktree import create_worktree, prune_registrations, registry_lock, release_stale_holders, remove_worktree
from runner.claude_scratch import files_touched_from_patch


def publish_branch(ctx: Ctx, branch: str) -> str | None:
    """Push `branch` to origin. None on success or with no origin, else a warning; a failed push never stops the build."""
    ok, detail = push_branch(ctx.repo, branch)
    return None if ok else f"push of {branch} failed: {detail}"


def note_push_failure(record: PhaseRecord, warning: str | None) -> None:
    """Add `warning` to the phase record's `remote_push_failed`, a single string joined with '; '."""
    if warning:
        record["remote_push_failed"] = "; ".join(filter(None, [record.get("remote_push_failed"), warning]))


# ── branch topology ─────────────────────────────────────────────────────────
def branch_exists(ctx: Ctx, branch: str) -> bool:
    ok, _ = git_in("-C", str(ctx.repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return ok


def branch_ahead(ctx: Ctx, branch: str) -> bool:
    """True when `branch` holds commits the default ref lacks. A missing branch is not ahead."""
    ok, out = git_in("-C", str(ctx.repo), "rev-list", "--count", f"{ctx.default_ref}..refs/heads/{branch}")
    return ok and out.isdigit() and int(out) > 0


class _Opened(tuple[bool, str, bool]):
    """`(ok, detail, reused)`, with the worktree paths cleared on the way in as `.cleared`.

    A tuple subclass so every caller and test that unpacks the triple keeps working.
    """

    cleared: tuple[str, ...]

    def __new__(cls, ok: bool, detail: str, reused: bool, cleared: Sequence[Path] = ()) -> _Opened:
        opened = super().__new__(cls, (ok, detail, reused))
        opened.cleared = tuple(str(path) for path in cleared)
        return opened


def _run_alive(ctx: Ctx) -> Callable[[str], bool]:
    """A run is alive when it is this one, or its `<run_id>.pid` names a running process."""
    return lambda run_id: run_id == ctx.run_id or pid_alive(ctx.runs_dir / f"{run_id}.pid")


def _registered_on_branch(listing: str, path: Path, branch: str) -> bool:
    """True when `git worktree list --porcelain` text registers `path` (resolved) on `refs/heads/<branch>`."""
    target = path.resolve()
    return any(
        f"branch refs/heads/{branch}" in record.splitlines()
        and any(
            line.startswith("worktree ") and Path(line.removeprefix("worktree ")).resolve() == target
            for line in record.splitlines()
        )
        for record in listing.split("\n\n")
    )


def open_phase_worktree(
    ctx: Ctx, phase: str, base_ref: str, parents: tuple[str, ...] = (), refs: Sequence[str] | None = None
) -> _Opened:
    """Get a worktree on the phase branch. Returns (ok, detail, reused), with `.cleared` on the result.

    Re-entrancy is the point: a second driver run over the same initiative
    builds on the branch the first one left, rather than starting a parallel
    one beside it under a different run id.

    Prunes stale `git worktree` registrations first: a crashed prior run can
    leave a phantom entry pointing at a directory that is already gone, and
    that phantom is what blocked `tools-chair-rename-18` on 2026-09-08.

    A dead run can also leave its worktree in place holding the branch, so a
    reused branch first releases the holders whose run is no longer alive.
    A registered worktree of this run on the branch is reused.

    A new branch under any parent is cut from `base_ref` and assembled from `refs`, the commits it needs;
    `refs` defaults to every parent's branch. A failure keeps the assembly cause.
    """
    prune_registrations(ctx.repo)
    branch = ctx.phase_branch(phase)
    worktree = ctx.phase_worktree(phase)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(ctx, branch):
        cleared = release_stale_holders(ctx.repo, branch, ctx.worktree_root, _run_alive(ctx))
        listed, listing = git_in("-C", str(ctx.repo), "worktree", "list", "--porcelain")
        if listed and _registered_on_branch(listing, worktree, branch):
            return _Opened(True, "", True, cleared)
        with registry_lock(ctx.repo):
            ok, detail = git_in("-C", str(ctx.repo), "worktree", "add", str(worktree), branch)
        return _Opened(ok, detail, True, cleared)
    ok, detail = create_worktree(ctx.repo, worktree, branch=branch, base=base_ref)
    wanted = [ctx.phase_branch(p) for p in parents] if refs is None else list(refs)
    if ok and parents and wanted and wanted != [base_ref]:
        assembled = assemble(ctx.repo, worktree, base_ref, wanted, branch)
        if not assembled.ok:
            # Drop the half-made branch, or the next run reuses it as if it were assembled.
            remove_worktree(ctx.repo, worktree)
            git_in("-C", str(ctx.repo), "branch", "-D", branch)
            return _Opened(False, ASSEMBLY_FAILED + assembled.cause, False)
    return _Opened(ok, detail, False)


ASSEMBLY_FAILED = "parent branches could not be assembled: "


def start_failure(detail: str, verb: str) -> str:
    """The reason for a phase whose worktree did not open; a failed assembly keeps its cause as the reason."""
    if detail.startswith(ASSEMBLY_FAILED):
        return detail
    return f"the phase worktree could not be {verb}: {detail}"


def required_refs(
    items: Sequence[Mapping[str, object]],
    phase: str,
    parents: Collection[str],
    branch_of: Callable[[str], str],
    base_ref: str,
    *,
    cut: bool = False,
    from_pr: bool = False,
) -> list[str]:
    """The refs a phase branch is checked against (`cut` false) or cut from (`cut` true).

    A phase with no parents, or one cut from the pr branch (`from_pr`), needs `base_ref` alone. Under a
    parent, a need that is unbuilt, done or dropped contributes nothing, and a parent no task needs anything
    from contributes its branch. A need that is approved and merged contributes its stored `merged_commit`
    (the parent's branch when none is stored), so the staleness check judges a branch on what its tasks need.
    With `cut`, such a need contributes the parent's current branch tip instead, so an assembly never starts
    from an older head whose conflict a later commit resolved, and a parent whose tasks are all settled
    contributes its whole branch.
    """
    if not parents or from_pr:
        return [base_ref]
    by_id = {str(i["id"]): i for i in items}
    open_phases = {str(i.get("phase")) for i in items if i.get("state") in ("todo", "ready", "in_progress", "blocked")}
    needed = [
        by_id[str(n)] for i in items if i.get("phase") == phase for n in as_list(i.get("needs")) if str(n) in by_id
    ]
    refs: set[str] = set()
    for parent in parents:
        edges = [n for n in needed if n.get("phase") == parent]
        if not edges or (cut and parent not in open_phases):
            refs.add(branch_of(parent))
            continue
        refs.update(
            branch_of(parent) if cut else str(n.get("merged_commit") or branch_of(parent))
            for n in edges
            if n.get("state") == "approved" and n.get("merged")
        )
    return sorted(refs)


def needs_other_phase(items: Sequence[Mapping[str, object]], phase: str) -> bool:
    """Does a task of `phase` need a task of another phase, whatever that task's state?"""
    phase_of = {str(i["id"]): i.get("phase") for i in items}
    return any(
        phase_of.get(str(n)) not in (None, phase)
        for i in items
        if i.get("phase") == phase
        for n in as_list(i.get("needs"))
    )


def needs_missing(ctx: Ctx, phase: str, refs: Sequence[str]) -> bool:
    """Is any commit the phase branch needs absent from it?

    `merge-base --is-ancestor` is the whole question. A parent that merges unrelated work moves its
    head and changes nothing here, since only the commits the branch needs are asked about.
    """
    return any(
        not git_in("-C", str(ctx.repo), "merge-base", "--is-ancestor", ref, ctx.phase_branch(phase))[0] for ref in refs
    )


def branch_action(reused: bool, head_moved: bool, has_own_commits: bool) -> str:
    """Block only a stale branch that has something of its own to lose; recreate the rest."""
    if not (reused and head_moved):
        return "proceed"
    return "block" if has_own_commits else "recreate"


def _task_of(subject: str) -> str | None:
    head, sep, task = subject.partition(": ")
    return task if sep and head.startswith("epic ") else None


def unlanded(cherry_lines: Sequence[str], done_tasks: set[str]) -> list[str]:
    """Subjects of `git cherry -v` `+` lines that are neither a done task's commit nor one of our merges.

    A landing can rewrite a done task's patch, so its patch-id no longer matches main's.
    A task literally named `merge ...` reads as a merge commit.
    """
    subjects = [line.split(" ", 2)[2] if line.count(" ") >= 2 else "" for line in cherry_lines if line.startswith("+ ")]
    return [s for s in subjects if (t := _task_of(s)) is None or not (t in done_tasks or t.startswith("merge "))]


def classify_unlanded(cherry_lines: Sequence[str], task_states: Mapping[str, str]) -> str:
    """What the phase branch's `+` commits mean, by their tasks' work-store states.

    `landed`: none left, or every one is a done task's (already squashed onto main).
    `awaiting_land`: every one is an approved task whose land is still in flight.
    `rebase`: a mix of done and approved. `rebuild`: every one is a ready task's (sent back with
    `route edit --rebuild`, so it builds again), beside any done ones. `block`: any other commit or task state.
    """
    tasks = [_task_of(s) for s in unlanded(cherry_lines, set())]
    states = {task_states.get(t, "") if t is not None else "" for t in tasks}
    if not states or states == {"done"}:
        return "landed"
    if states == {"approved"}:
        return "awaiting_land"
    if states == {"approved", "done"}:
        return "rebase"
    return "rebuild" if states <= {"ready", "done"} else "block"


def blocking_commits(cherry_lines: Sequence[str], task_states: Mapping[str, str]) -> list[str]:
    """One `task <id> state=<state>` per task whose unlanded commit is on the branch, in commit order, or `commit <subject>` when no task owns it."""
    subjects = unlanded(cherry_lines, set())
    found = [f"task {t} state={task_states.get(t) or 'unknown'}" if (t := _task_of(s)) is not None else f"commit {s!r}" for s in subjects]
    return list(dict.fromkeys(found))


def waiting_tasks(cherry_lines: Sequence[str]) -> list[str]:
    """Ids of the tasks owning an unlanded commit, once each, in commit order."""
    ids = (_task_of(s) for s in unlanded(cherry_lines, set()))
    return list(dict.fromkeys(t for t in ids if t is not None))


def awaiting_land_lines(record: Mapping[str, object], run_id: str, repo: Path | str) -> list[str]:
    """The phase line, then one `cox runs land` per waiting task, for an awaiting_land record whose land is due; else nothing.

    The flags mirror the chair's land action (`chair_exec`); this layer does not import it.
    """
    if record.get("status") != "awaiting_land":
        return []
    tasks = [str(t) for t in as_list(record.get("awaiting_tasks"))]
    head = f"phase {record.get('phase')}: awaiting land of {len(tasks)} approved task(s): {', '.join(tasks)}"
    return [head, *(f"cox runs land {run_id} --task {t} --repo {repo} --apply" for t in tasks)]


def blocked_line(record: Mapping[str, object]) -> str | None:
    """`phase <p>: blocked: <reason>` for a blocked or interrupted phase record, else None."""
    status = record.get("status")
    if status not in ("blocked", "interrupted"):
        return None
    return f"phase {record.get('phase')}: {status}: {record.get('reason') or 'no reason was recorded on the phase'}"


def open_in_phase(
    items: Sequence[Mapping[str, object]], phase: str, in_run: Collection[str], land_at: str
) -> list[str]:
    """Ids of `phase` items outside this run whose stored state does not meet `need_met` for `land_at`, in item order."""
    return [
        str(item["id"])
        for item in items
        if item.get("phase") == phase
        and str(item["id"]) not in in_run
        and not need_met(land_at, str(item.get("state")), bool(item.get("merged")))
    ]


def outside_parents_complete(
    items: Sequence[Mapping[str, object]],
    rows: Sequence[Mapping[str, object]] | None,
    parents: Mapping[str, set[str]],
    named: Collection[str],
    land_at: str,
) -> set[str]:
    """Parents of the `named` phases that are not named themselves and have every task met under `land_at`.

    A stored row's state and merged flag win over the item's. A parent with no tasks is not complete.
    """
    by_task = {str(r["task_id"]): r for r in rows or ()}
    view = [
        {**item, "state": row["state"], "merged": row.get("merged")} if (row := by_task.get(str(item["id"]))) else item
        for item in items
    ]
    outside = {p for phase in named for p in parents.get(phase, ())} - set(named)
    return {p for p in outside if any(i.get("phase") == p for i in view) and not open_in_phase(view, p, (), land_at)}


def phase_branch_cherry(ctx: Ctx, phase: str, upstream: str | None = None) -> list[str]:
    """`git cherry -v` of the phase branch against `upstream` (main by default), patch-id semantics; empty if git fails."""
    ok, out = git_in("-C", str(ctx.repo), "cherry", "-v", upstream or ctx.default_ref, ctx.phase_branch(phase))
    return out.splitlines() if ok else []


def recorded_merges(items: Iterable[Mapping[str, object]], phase: str) -> tuple[str, ...]:
    """The `merged_commit` each of `phase`'s tasks recorded, in item order: the shas a refresh must keep on the branch."""
    return tuple(str(i["merged_commit"]) for i in items if i.get("phase") == phase and i.get("merged_commit"))


def locked_merges(ctx: Ctx, items: Iterable[Mapping[str, object]], phase: str) -> tuple[str, ...]:
    with ctx.items_lock:
        return recorded_merges(items, phase)


def rebase_phase_branch(ctx: Ctx, phase: str, base_ref: str, recorded: Iterable[str] = ()) -> tuple[bool, str]:
    """Bring the phase branch up to `base_ref`, or leave it untouched.

    Named for the `stack_rebase` write kind it executes, but it merges `base_ref` in rather than replaying the
    branch: a replay drops (or re-shas) the task merges whose `merged_commit` the store holds, and the next build
    then quarantines on "merged commit ... is not on ...". A refresh that would still lose one of `recorded`
    refuses and leaves the branch where it was.

    A conflict aborts and quarantines the phase with git's own diagnosis. There
    is no path here that resolves one unattended: `stack_rebase` is a write kind
    precisely because rewriting a branch other work is stacked on can silently
    discard a commit, and a driver guessing at a resolution would be doing
    exactly that.
    """
    ok, detail, _ = _rebase_reading_conflicts(ctx, phase, base_ref, recorded)
    return ok, detail


def _rebase_reading_conflicts(
    ctx: Ctx, phase: str, base_ref: str, recorded: Iterable[str] = ()
) -> tuple[bool, str, tuple[str, ...]]:
    """`rebase_phase_branch`, plus the conflicting paths git listed before the abort cleared them."""
    branch = ctx.phase_branch(phase)
    result = guarded_refresh(ctx.phase_worktree(phase), branch, base_ref, recorded)
    if not result.ok:
        return False, result.reason, result.paths
    return True, f"merged {base_ref} into {branch}", ()


def _branch_has(ctx: Ctx, ref: str, branch: str) -> bool:
    return git_in("-C", str(ctx.repo), "merge-base", "--is-ancestor", ref, branch)[0]


def _branch_head(ctx: Ctx, branch: str) -> str:
    ok, out = git_in("-C", str(ctx.repo), "rev-parse", branch)
    return out if ok else ""


def refresh_level(
    ctx: Ctx, record: PhaseRecord, phase: str, recorded: Iterable[str] = ()
) -> PhaseRecord | None:
    """Bring a reused phase branch up to the initiative branch; the record to return when it blocks.

    The initiative branch is the phase's only parent. It moves when a sibling phase merges (or when the lander takes
    the initiative's own parent forward), and a reused phase merges that tip in before any of its tasks builds. It is
    always a forward merge: no phase is rebased, recreated or restacked, and a merge that would drop one of
    `recorded` is refused. Without an initiative branch (land_at task) a phase is never refreshed."""
    if ctx.initiative_branch is None or not record.get("reused_branch"):
        return None
    branch, onto = ctx.phase_branch(phase), ctx.initiative_branch
    if _branch_has(ctx, onto, branch):
        return None
    before = _branch_head(ctx, branch)
    merged = guarded_refresh(ctx.phase_worktree(phase), branch, onto, recorded)
    if not merged.ok:
        record["status"] = "blocked"
        record["reason"] = merged.reason
        quarantine: Quarantine = {"id": phase, "phase": phase, "grain": "phase", "reason": merged.reason}
        record.setdefault("quarantined", []).append(quarantine)
        git_in("-C", str(ctx.phase_worktree(phase)), "checkout", "--detach", "-q")
        return record
    after = _branch_head(ctx, branch)
    if after != before:
        record["refreshed"] = refresh_record(branch, onto, before, after)
        print(f"  {branch} brought up to {onto}")
        # The branch was published before it moved: origin gets the refreshed tip, or another host builds on the old one.
        note_push_failure(record, publish_branch(ctx, branch))
    return None


# ── one task's build, applied and measured in a worktree the harness owns ────


def trace_evidence(calls: Sequence[object], task: str, patch: str) -> list[dict[str, str]]:
    """Evidence rows the harness itself observed, per docs/design/validator-reach.md §1.

    Commands are selected by `task_id` — stamped onto the call ledger by
    `ClaudeCodeRunner.run`'s own `task` kwarg — never by `files_touched`, which two
    tasks (or a fix-loop retry) can share. `ctx.runner.calls` accumulates across every
    task and retry, so the LAST matching `role: "build"` call is the one whose patch
    this record actually applied. One `command` row per `commands_run` entry with
    `source == "trace"`, in trace order — a `self_report` entry is a claim, not an
    observation, and is never folded in.

    A resumed task (`--resume-from`) makes no build call in THIS run, so no ledger
    entry matches at all — `files_touched` then falls back to the reconciled `patch`
    itself, still exactly one row, never empty for a non-empty patch.
    """
    builds = [m for m in map(as_mapping, calls) if m.get("role") == "build" and m.get("task_id") == task]
    call: Mapping[str, object] = builds[-1] if builds else {}
    commands = [
        {
            "check": "command",
            "source": "trace",
            "output": f"{entry.get('command', '')}\n{tail_lines(str(entry.get('output') or ''))}",
        }
        for entry in map(as_mapping, as_list(call.get("commands_run")))
        if entry.get("source") == "trace"
    ]
    files = as_list(call.get("files_touched")) or files_touched_from_patch(patch)
    return [*commands, {"check": "files_touched", "source": "trace", "output": "\n".join(map(str, files))}]
