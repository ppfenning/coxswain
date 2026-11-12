"""The lander: the worker that owns landing, apart from the chair's build tick.

It reads the land queue and performs two kinds of item. A `phase_merge` merges one built phase into its initiative
branch, locally: that is "phase complete". An `initiative_land` takes a whole initiative to its parent: refresh onto
the parent, gate checks, push, the ONE pull request, the wait for its checks, the merge, and the bookkeeping. No phase
ever gets a pull request.

The lander is a script, not an agent. It never resolves a conflict and never edits code: a conflict or a red check is
recorded on the item and sent back to build as a rectify task. Everything it does can be run again: each step reads
what is already true (the record, the branch, the forge) before it acts.

Every effect goes through `Ports`, so the whole worker runs in tests against a real repo, a real store and a fake forge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import landing_record as lr
from core.landing import (
    TemplateError,
    initiative_pr_reference,
    initiative_pr_title,
    resolve_on_blocked,
    resolve_push_initiative,
)
from harness import landing_git as lg
from harness import landing_ops as ops
from harness import landing_store as ls

LAND_CLAIM_SECONDS = 3600  # an initiative land waits on CI; its claim is renewed while it works
QUEUE_ALARM_SECONDS = 3600  # an item still open this long after it was queued is told to the chair, once
PERSON_GATE_NOTE = "the pull request is open and its merge waits for a person"


@dataclass
class Ports:
    """Everything the lander touches outside its own logic."""

    conn: Any  # the store connection
    worker: str
    now: Callable[[], str]
    tasks: Callable[[str], list[dict[str, Any]]]  # an initiative's task rows: id, phase, state, needs, title
    meta: Callable[[str], Mapping[str, Any]]  # {"initiative": {...}, "cartridge": {...}, "epic_issue": ..., "gate": "full"}
    forge: Any  # push, find_open_prs, open_pr, wait_checks, merge, pr_merged, delete_branch
    checks: Callable[[Path, Path, str], tuple[bool, str]]  # (repo, tree, "level" | "ticket") -> passed, output
    mark_merged: Callable[[str, str, str], None]  # initiative, task, the initiative tip that holds it
    mark_done: Callable[[str, str, str], str | None]  # initiative, task, pr url -> why it was refused, or None
    file_rectify: Callable[[str, str, str, str], str | None] = lambda *_: None  # initiative, phase, title, body -> task id
    split_blocked: Callable[[str, Sequence[str]], str | None] = lambda *_: None  # initiative, phases -> follow-up id
    notify: Callable[[str, Mapping[str, Any]], None] = lambda *_: None
    # The initiative's checkout on this machine. The record's own `repo` is the path on the host that cut the branch.
    repo: Callable[[Mapping[str, Any]], Path] | None = None
    # Bring one branch up to origin's copy, forward only: why it must not be moved here (it diverged), else None.
    fetch: Callable[[Path, str], str | None] = lambda *_: None
    log: Callable[[str], None] = print
    wait_checks_s: float = 1800.0


@dataclass(frozen=True)
class Outcome:
    """What one item came to. `status`: done, waiting (left queued), failed, skipped."""

    status: str
    item: str
    detail: str = ""
    events: tuple[str, ...] = field(default_factory=tuple)


def _repo(ports: Ports, record: Mapping[str, Any]) -> Path:
    found = ports.repo(record) if ports.repo is not None else Path(str(record.get("repo") or "")).expanduser()
    if not str(found).strip(".") or not found.is_absolute():
        # Never the current directory: an empty path would run git in whatever checkout the process happens to be in.
        raise LookupError(f"{record.get('initiative')}: its repository is not known on this machine")
    return found


def _parent_ref(repo: Path, parent: str) -> str:
    """The parent as it stands on origin after a fetch, else the local branch (a repo with no origin)."""
    lg.git(repo, "fetch", "--quiet", "origin", parent)
    return f"origin/{parent}" if lg.rev(repo, f"refs/remotes/origin/{parent}") else parent


def _level_checks(ports: Ports, repo: Path, record: Mapping[str, Any], phase: str) -> lg.Checks | None:
    """Level checks for the merged tree, only when the merge joins the phase with work it has not seen."""
    if lg.is_ancestor(repo, str(record["branch"]), ops.phase_branch(str(record["initiative"]), phase)):
        return None
    return lambda tree: ports.checks(repo, tree, "level")


# -- phase_merge --------------------------------------------------------------------------------------------------

def merge_phase(ports: Ports, initiative: str, phase: str) -> Outcome:
    """Merge one built phase into its initiative branch and settle what follows from it."""
    ident = ls.item_id(ls.PHASE_MERGE, initiative, phase)
    record = ls.get_record(ports.conn, initiative)
    if record is None:
        return Outcome("skipped", ident, f"{initiative} has no landing record")
    repo, tasks = _repo(ports, record), ports.tasks(initiative)
    # The phase was built on another host as often as not: its branch, and the initiative's, come from origin first.
    diverged = ports.fetch(repo, ops.phase_branch(initiative, phase))
    if diverged:
        return Outcome("waiting", ident, diverged)
    early = resolve_push_initiative((ports.meta(initiative).get("cartridge")) or {}) == "early"

    def publish(branch: str) -> str | None:
        pushed, why = ports.forge.push(repo, branch)
        return None if pushed else f"push of {branch} failed: {why}"

    done = ops.merge_phase(ports.conn, repo, initiative=initiative, phase=phase, tasks=tasks, worker=ports.worker,
                           now=ports.now(), checks=_level_checks(ports, repo, record, phase),
                           fetch=lambda branch: ports.fetch(repo, branch), publish=publish if early else None)
    if done.status in ("busy", "not_ready"):
        return Outcome("waiting", ident, done.detail)
    if done.status == "error":
        return Outcome("failed", ident, done.detail)
    if done.status == "conflicted":
        return Outcome("failed", ident, done.detail, tuple(_send_back(ports, record, phase, done)))
    events = [f"phase {phase} of {initiative} merged into {record['branch']}"]
    for task in done.merged_tasks:
        ports.mark_merged(initiative, task, done.merge_sha)
    if done.status == "merged" and done.detail:
        events.append(done.detail)
    if done.assembled and ls.enqueue(ports.conn, ls.INITIATIVE_LAND, initiative, repo=str(record.get("repo") or ""),
                                     parent=str(record.get("parent") or ""), now=ports.now(), reason="last phase merged"):
        events.append(f"{initiative} is assembled: queued to land on {record.get('parent')}")
    return Outcome("done", ident, events[0], tuple(events))


def _send_back(ports: Ports, record: Mapping[str, Any], phase: str, done: ops.PhaseMerge) -> list[str]:
    """A phase merge that conflicted or failed its checks goes back to build as a rectify task in that phase.

    The conflicted merge is committed on the phase branch first, markers and all: a builder returns a patch, so the
    conflict has to be in the tree it starts from, and everything the phase already built stays where it is."""
    initiative, branch = str(record["initiative"]), str(record["branch"])
    repo = _repo(ports, record)
    if done.cause == "guard":
        ports.notify("needs_chair", {"initiative": initiative, "phase": phase, "reason": done.detail})
        return [f"phase {phase} of {initiative}: {done.detail}"]
    if done.cause == "conflict":
        marked = lg.conflict_commit(repo, branch=ops.phase_branch(initiative, phase), source=branch,
                                    message=f"{initiative}: merge {branch} into phase {phase} (conflicted, to rectify)")
        if marked.status == "error":
            ports.notify("needs_chair", {"initiative": initiative, "phase": phase, "reason": marked.detail})
            return [f"phase {phase} of {initiative}: could not prepare the rectify base: {marked.detail}"]
        title = f"Rectify phase {phase}: resolve its merge with {branch}"
        body = rectify_body(branch, phase, done.paths, "")
    else:
        title = f"Rectify phase {phase}: its checks fail once merged with {branch}"
        merged = lg.merge_into(repo, branch=ops.phase_branch(initiative, phase), source=branch,
                               message=f"{initiative}: merge {branch} into phase {phase}")
        if not merged.ok:
            ports.notify("needs_chair", {"initiative": initiative, "phase": phase, "reason": merged.detail})
            return [f"phase {phase} of {initiative}: could not prepare the rectify base: {merged.detail}"]
        body = rectify_body(branch, phase, (), done.detail)
    task = ports.file_rectify(initiative, phase, title, body)
    if task is None:
        ports.notify("needs_chair", {"initiative": initiative, "phase": phase, "reason": done.detail})
        return [f"phase {phase} of {initiative} is conflicted and no rectify task could be filed: {done.detail}"]
    return [f"phase {phase} of {initiative} sent back to build: rectify task {task} ({done.detail})"]


def rectify_body(branch: str, phase: str, paths: Sequence[str], check_output: str) -> str:
    """The body of a rectify task. It names what to fix and says, first, that nothing already built is to be redone."""
    keep = (
        f"The work of phase `{phase}` is already built, reviewed and on this phase's branch, and the tip of `{branch}` "
        "has been merged into it. Start from this tree. Change only what the problem below needs: do not rebuild, "
        "rewrite or remove work that is already here."
    )
    if paths:
        listed = "\n".join(f"- `{p}`" for p in paths)
        return (
            f"{keep}\n\n**Problem**\n\nMerging `{branch}` into this phase conflicted. The files below hold conflict markers "
            f"(`<<<<<<<`, `=======`, `>>>>>>>`), with both sides present:\n\n{listed}\n\n**Wanted**\n\nResolve each conflict so "
            "both sides' intent holds, remove every marker, and keep the checks passing.\n\n**Checks**\n\nNo conflict marker "
            "is left in the tree, and the phase's checks pass."
        )
    return (
        f"{keep}\n\n**Problem**\n\nThis phase merges cleanly with `{branch}`, but the checks fail on the merged tree:\n\n"
        f"```\n{check_output.strip()[:4000]}\n```\n\n**Wanted**\n\nFix what the merge broke, so the checks pass on this tree.\n\n"
        "**Checks**\n\nThe checks above pass."
    )


# -- initiative_land ----------------------------------------------------------------------------------------------

def _pr_body(meta: Mapping[str, Any], record: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]], reference: str | None) -> str:
    """The one pull request's body: the initiative, a block per phase with its tasks, and the issue line."""
    initiative = meta.get("initiative") or {}
    parents = lr.phase_parents(tasks)
    lines = [str(initiative.get("summary") or initiative.get("title") or record["initiative"]).strip(), ""]
    for phase in lr.merge_order(parents) or sorted(parents, key=lr.natural_key):
        own = [t for t in lr.phase_tasks(tasks, phase) if t.get("state") != "dropped"]
        if not own:
            continue
        lines.append(f"### {phase}")
        lines += [f"- {t.get('title') or t['id']} (`{t['id']}`)" for t in own]
        lines.append("")
    if reference:
        lines += [reference, ""]
    return "\n".join(lines).rstrip() + "\n"


def _other_land_on_parent(ports: Ports, mine: Mapping[str, Any]) -> str | None:
    """Another initiative land holding the same parent branch of the same repo, claimed before this one: it goes first."""
    now = ports.now()
    for other in ls.list_items(ports.conn, states=("claimed",)):
        same = other["kind"] == ls.INITIATIVE_LAND and other["repo"] == mine["repo"] and other["parent"] == mine["parent"]
        if same and other["id"] != mine["id"] and str(other["lease_until"]) >= now and (
            (str(other["claimed_at"]), str(other["id"])) < (str(mine["claimed_at"]), str(mine["id"]))
        ):
            return str(other["initiative"])
    return None


def land_initiative(ports: Ports, initiative: str) -> Outcome:
    """Take one assembled initiative to its parent. Safe to run again at any point."""
    ident = ls.item_id(ls.INITIATIVE_LAND, initiative)
    now = ports.now()
    if not ls.claim(ports.conn, ident, ports.worker, now=now, lease_until=ops.later(now, LAND_CLAIM_SECONDS)):
        return Outcome("waiting", ident, "another worker holds this land")

    def close(status: str, detail: str, events: Sequence[str] = ()) -> Outcome:
        if status == "done":
            ls.finish(ports.conn, ident, ports.worker, now=ports.now())
        elif status == "waiting":
            ls.release(ports.conn, ident, ports.worker, now=ports.now(), error=detail)
        else:
            ls.fail(ports.conn, ident, ports.worker, now=ports.now(), error=detail)
        return Outcome(status, ident, detail, tuple(events))

    record = ls.get_record(ports.conn, initiative)
    if record is None:
        return close("failed", f"{initiative} has no landing record")
    first = _other_land_on_parent(ports, ls.get_item(ports.conn, ident) or {})
    if first is not None:
        return close("waiting", f"{first} is landing on {record.get('parent')} first")
    tasks = ports.tasks(initiative)
    state = lr.initiative_state(record, tasks)
    if state == "landed":
        return close("done", f"{initiative} already landed", _finish_landed(ports, record, tasks, None))
    if state not in ("assembled", "pr_open"):
        return close("waiting", f"{initiative} is {state}, not assembled")
    repo, branch, parent = _repo(ports, record), str(record["branch"]), str(record["parent"])
    meta = ports.meta(initiative)
    events: list[str] = []
    auto = str(meta.get("gate") or "full") == "full"
    merged, _ = ports.forge.pr_merged(repo, branch) if (record.get("pr") or {}).get("url") else (False, "")
    # An open pull request the lander itself will merge is refreshed when its parent has moved under it. One that
    # waits for a person is left as that person last saw it.
    behind = auto and not merged and (lg.ahead_behind(repo, branch, _parent_ref(repo, parent)) or (0, 0))[1] > 0
    if not merged and (state == "assembled" or not (record.get("pr") or {}).get("url") or behind):
        opened = _open_pr(ports, record, tasks, meta, repo, branch, parent)
        if isinstance(opened, Outcome):
            return close(opened.status, opened.detail, opened.events)
        record, new_events = opened
        events += new_events
    url = str((record.get("pr") or {}).get("url") or "")
    if not merged:
        merged, _ = ports.forge.pr_merged(repo, branch)
    if not merged:
        if not auto:
            return close("waiting", f"{PERSON_GATE_NOTE}: {url}", events)
        green, detail = ports.forge.wait_checks(repo, ports.wait_checks_s, ref=branch)
        if not green:
            if "pending" in detail or "timed out" in detail:
                return close("waiting", f"checks on {url} have not finished: {detail}", events)
            return close("failed", f"checks failed on {url}: {detail}", [*events, *_send_back_initiative(ports, record, "checks", detail)])
        ok, detail = ports.forge.merge(repo, {"kind": "merge", "squash": True, "delete_branch": True, "branch": branch,
                                             "default_branch": parent, "subject": str((record.get("pr") or {}).get("title") or initiative)})
        if not ok:
            return close("failed", f"merge of {url} failed: {detail}", events)
        events.append(f"merged {url} into {parent}")
    events += _finish_landed(ports, record, tasks, url)
    return close("done", f"{initiative} landed on {parent}", events)


def _open_pr(ports: Ports, record: dict[str, Any], tasks: Sequence[Mapping[str, Any]], meta: Mapping[str, Any], repo: Path,
             branch: str, parent: str) -> tuple[dict[str, Any], list[str]] | Outcome:
    """Refresh onto the parent, run the gate checks, push, and open the one pull request (or find the one already open)."""
    initiative, ident = str(record["initiative"]), ls.item_id(ls.INITIATIVE_LAND, str(record["initiative"]))
    recorded = lr.recorded_shas(record)
    refreshed = ops.take_parent_forward(ports.conn, repo, initiative=initiative, parent_ref=_parent_ref(repo, parent),
                                        worker=ports.worker, now=ports.now(), fetch=lambda b: ports.fetch(repo, b))
    if refreshed.status == "conflict":
        return Outcome("failed", ident, f"merging {parent} into {branch} conflicted in {', '.join(refreshed.paths)}",
                       tuple(_send_back_initiative(ports, record, "conflict", refreshed.detail, refreshed.paths)))
    if not refreshed.ok:
        return Outcome("failed", ident, refreshed.detail)
    passed, output = lg.on_scratch(repo, branch, lambda tree: ports.checks(repo, tree, "ticket"))
    if not passed:
        return Outcome("failed", ident, f"gate checks failed on {branch}",
                       tuple(_send_back_initiative(ports, record, "checks", output)))
    dropped = lg.missing_from(repo, branch, recorded)
    if dropped:  # guard before any push: nothing the record names may be missing from what goes to origin
        return Outcome("failed", ident, f"refused to push {branch}: it lacks recorded commit(s) {', '.join(s[:12] for s in dropped)}")
    pushed, why = ports.forge.push(repo, branch)
    if not pushed:
        return Outcome("failed", ident, f"push of {branch} failed: {why}")
    try:
        cartridge, own = meta.get("cartridge") or {}, meta.get("initiative") or {"id": initiative}
        title = initiative_pr_title(cartridge, own, parent, meta.get("epic_issue"))
        reference = initiative_pr_reference(cartridge, own, parent, meta.get("epic_issue"))
    except TemplateError as exc:
        return Outcome("failed", ident, str(exc))
    existing = ports.forge.find_open_prs(repo, branch)
    if isinstance(existing, str):
        return Outcome("waiting", ident, f"could not ask the forge about {branch}: {existing}")
    if existing:
        url = str(existing[0]) if not isinstance(existing[0], Mapping) else str(existing[0].get("url") or existing[0].get("number"))
        events = [f"found the open pull request for {branch}: {url}"]
    else:
        ok, url = ports.forge.open_pr(repo, title, _pr_body(meta, record, tasks, reference), head=branch, base=parent)
        if not ok:
            return Outcome("failed", ident, f"pull request for {branch} was not opened: {url}")
        events = [f"opened the pull request for {initiative}: {url}"]
    updated = lr.with_state({**record, "state": "assembled"} if record.get("state") != "pr_open" else record, "pr_open", ports.now(),
                            pr={"url": url, "title": title, "head": branch, "base": parent})
    ls.put_record(ports.conn, updated)
    return updated, events


def _send_back_initiative(ports: Ports, record: Mapping[str, Any], cause: str, detail: str, paths: Sequence[str] = ()) -> list[str]:
    """An initiative that conflicts with its parent, or fails its gate, goes back to build in a final `integrate` phase.

    The `integrate` branch is cut from the initiative tip, so it holds every merged phase; for a conflict the
    conflicted merge of the parent is committed on it first. Nothing already merged is rebuilt."""
    initiative, branch, parent = str(record["initiative"]), str(record["branch"]), str(record["parent"])
    repo, phase = _repo(ports, record), lr.INTEGRATE_PHASE
    integrate = ops.phase_branch(initiative, phase)
    tip, _, why = lg.ensure_branch(repo, integrate, branch)
    if tip is None:
        ports.notify("needs_chair", {"initiative": initiative, "reason": why})
        return [f"{initiative}: could not cut {integrate}: {why}"]
    lg.merge_into(repo, branch=integrate, source=branch, message=f"{initiative}: bring {integrate} up to {branch}")
    if cause == "conflict":
        marked = lg.conflict_commit(repo, branch=integrate, source=_parent_ref(repo, parent),
                                    message=f"{initiative}: merge {parent} into {integrate} (conflicted, to rectify)")
        if marked.status == "error":
            ports.notify("needs_chair", {"initiative": initiative, "reason": marked.detail})
            return [f"{initiative}: could not prepare the integrate base: {marked.detail}"]
        title, body = f"Integrate with {parent}: resolve the conflicts", rectify_body(parent, phase, paths or marked.paths, "")
    else:
        title, body = f"Integrate with {parent}: the gate checks fail", rectify_body(parent, phase, (), detail)
    task = ports.file_rectify(initiative, phase, title, body)
    if task is None:
        ports.notify("needs_chair", {"initiative": initiative, "reason": detail})
        return [f"{initiative} cannot land and no integrate task could be filed: {detail}"]
    current = ls.get_record(ports.conn, initiative) or dict(record)
    stored = str(current.get("state") or "building")
    if lr.initiative_move_allowed(stored, "conflicted"):
        ls.put_record(ports.conn, lr.with_state(current, "conflicted", ports.now(), error=detail))
    return [f"{initiative} sent back to build: integrate task {task}"]


def _finish_landed(ports: Ports, record: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]], url: str | None) -> list[str]:
    """The bookkeeping after the pull request merged: every task done, the record landed, the branches cleaned.

    Each part reads what is already so: a task already done is skipped, a landed record is left, a missing branch is
    not an error. So a land that died anywhere after the merge finishes cleanly when it runs again."""
    initiative = str(record["initiative"])
    pr = url or str((record.get("pr") or {}).get("url") or "")
    events, refused = [], []
    for task in tasks:
        if task.get("state") == "approved":
            why = ports.mark_done(initiative, str(task["id"]), pr)
            if why is not None:
                refused.append(f"{task['id']}: {why}")
    if refused:
        events.append(f"{initiative}: not marked done: {'; '.join(refused)}")
    current = ls.get_record(ports.conn, initiative) or dict(record)
    if current.get("state") != "landed":
        stored = str(current.get("state") or "building")
        if not lr.initiative_move_allowed(stored, "landed"):
            current = {**current, "state": "assembled"}
        ls.put_record(ports.conn, lr.with_state(current, "landed", ports.now(), landed_pr=pr))
        events.append(f"{initiative} recorded landed")
    if not refused:
        events += _clean(ports, current)
    return events


def _clean(ports: Ports, record: Mapping[str, Any]) -> list[str]:
    """Delete the landed initiative's branches, here and on origin. Only ever called after the forge says it merged."""
    initiative, repo = str(record["initiative"]), _repo(ports, record)
    ok, listed = lg.git(repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/epic/{initiative}/")
    phases = [b for b in listed.splitlines() if b] if ok else []
    landed_tip = lg.rev(repo, f"refs/heads/{record['branch']}")
    # The guard before any delete: a phase branch goes only when the branch that landed holds its tip. One that holds
    # more (a blocked phase's approved work, a build that finished after the land) is kept and named.
    kept = [b for b in phases if landed_tip is None or not lg.is_ancestor(repo, b, landed_tip)]
    doomed = [b for b in phases if b not in kept]
    if landed_tip is not None:
        doomed.append(str(record["branch"]))
    gone = []
    for branch in doomed:
        deleted, _ = lg.git(repo, "branch", "-D", branch)
        if deleted:
            gone.append(branch)
        ports.forge.delete_branch(repo, branch)
    events = [f"{initiative}: deleted {len(gone)} branch(es)"] if gone else []
    if kept:
        events.append(f"{initiative}: kept {', '.join(kept)}: not in what landed")
    return events


# -- blocked phases -----------------------------------------------------------------------------------------------

def settle_blocked(ports: Ports, record: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """Apply `on_blocked` to an initiative with a phase whose task is quarantined past repair.

    `hold`: the initiative waits, and the operator is told once, with the choices. `split`: the blocked phase and
    every phase that depends on it move to a follow-up initiative, and this one lands without them; it is refused,
    and held instead, when a merged phase depends on the blocked one."""
    initiative = str(record["initiative"])
    meta = ports.meta(initiative)
    states = lr.phase_states(record, tasks)
    blocked = [p for p in states if any(t.get("state") == "quarantined" for t in lr.phase_tasks(tasks, p))
               and states[p] not in ("merged",)]
    if not blocked:
        return []
    policy = resolve_on_blocked(meta.get("cartridge") or {}, (meta.get("initiative") or {}).get("on_blocked"))
    parents = lr.phase_parents(tasks)
    events: list[str] = []
    current = dict(record)
    for phase in blocked:
        entry = (current.get("phases") or {}).get(phase) or {}
        if policy == "split":
            moving, why = lr.blocked_split(parents, states, phase)
            follow = ports.split_blocked(initiative, moving) if moving else None
            if follow is not None:
                events.append(f"{initiative}: blocked phase {phase} and its dependents ({', '.join(moving)}) moved to {follow}")
                continue
            why = why or "the split could not be made"
        else:
            why = "on_blocked is hold"
        if entry.get("state") != "blocked" or not entry.get("told"):
            stored = str(entry.get("state") or "open")
            fields = {"told": True, "error": why}
            if lr.phase_move_allowed(stored, "blocked"):
                fields["state"] = "blocked"
            current = lr.with_phase(current, phase, ports.now(), **fields)
            ls.put_record(ports.conn, current)
            ports.notify("blocked", {"initiative": initiative, "phase": phase, "reason": why,
                                     "choices": ["split the phase into a follow-up initiative", "drop the phase", "fix the task"]})
            events.append(f"{initiative}: phase {phase} is blocked ({why}); the operator has been told")
    return events


# -- the sweep and the loop ---------------------------------------------------------------------------------------

def sweep(ports: Ports) -> list[str]:
    """The reconciler: queue what an event missed. It never does the work itself.

    Every built phase of a live record gets a `phase_merge` item, every assembled initiative an `initiative_land`,
    and an initiative with a blocked phase has its `on_blocked` policy applied."""
    events: list[str] = []
    for record in ls.list_records(ports.conn):
        if record.get("state") == "landed" or record.get("land_at") == "task":
            continue
        initiative = str(record["initiative"])
        tasks = ports.tasks(initiative)
        if not tasks:
            continue
        events += settle_blocked(ports, record, tasks)
        current = ls.get_record(ports.conn, initiative) or record
        for phase in lr.mergeable_phases(current, tasks):
            if ops.queue_phase(ports.conn, ls.get_record(ports.conn, initiative) or current, phase, now=ports.now(), reason="sweep"):
                events.append(f"sweep: queued phase {phase} of {initiative}")
        if lr.initiative_state(current, tasks) in ("assembled", "pr_open") and ls.enqueue(
            ports.conn, ls.INITIATIVE_LAND, initiative, repo=str(current.get("repo") or ""),
            parent=str(current.get("parent") or ""), now=ports.now(), reason="sweep",
        ):
            events.append(f"sweep: queued {initiative} to land")
    return events


def stale_items(items: Sequence[Mapping[str, Any]], now: str, limit: int = QUEUE_ALARM_SECONDS) -> list[Mapping[str, Any]]:
    """Pure. The open queue items older than `limit` seconds, oldest first: approved work that is waiting and not moving.

    A land whose pull request waits for a person is not stalled, however long the person takes."""
    return sorted(
        (
            i for i in items
            if i["state"] in ("queued", "claimed", "failed") and i.get("queued_at")
            and ops.later(str(i["queued_at"]), limit) <= now
            and not str(i.get("last_error") or "").startswith(PERSON_GATE_NOTE)
        ),
        key=lambda i: (str(i["queued_at"]), str(i["id"])),
    )


def queue_alarm(ports: Ports, limit: int = QUEUE_ALARM_SECONDS) -> list[str]:
    """Tell the chair, once per queueing, about each item that has waited past `limit`: what it is, since when, and
    the last error. This is the stall alarm's view of landing: a lander that dies, or an item nothing can finish,
    shows up here instead of as silence."""
    events: list[str] = []
    for item in stale_items(ls.list_items(ports.conn), ports.now(), limit):
        initiative, ident = str(item["initiative"]), str(item["id"])
        record = ls.get_record(ports.conn, initiative)
        told = dict((record or {}).get("queue_alarms") or {})
        if record is None or told.get(ident) == item["queued_at"]:
            continue
        reason = f"{ident} has waited since {item['queued_at']} ({item['state']}, {item.get('attempts') or 0} attempt(s))"
        if item.get("last_error"):
            reason += f": {item['last_error']}"
        ports.notify("stalled", {"initiative": initiative, "phase": str(item.get("phase") or ""), "reason": reason})
        ls.put_record(ports.conn, {**record, "queue_alarms": {**told, ident: item["queued_at"]}})
        events.append(f"alarm: {reason}")
    return events


def perform(ports: Ports, item: Mapping[str, Any]) -> Outcome:
    if item["kind"] == ls.PHASE_MERGE:
        return merge_phase(ports, str(item["initiative"]), str(item["phase"]))
    return land_initiative(ports, str(item["initiative"]))


def run_once(ports: Ports, *, phase_cap: int = 2, land_cap: int = 1) -> list[Outcome]:
    """One pass: sweep, then perform what can be claimed, oldest first, within the two caps.

    Phase merges and initiative lands have separate caps and neither counts against the build cap. An item that is
    waiting stays queued for the next pass."""
    for line in [*sweep(ports), *queue_alarm(ports)]:
        ports.log(line)
    items = ls.claimable(ls.list_items(ports.conn), ports.now())
    outcomes: list[Outcome] = []

    def do(item: Mapping[str, Any]) -> Outcome:
        outcome = perform(ports, item)
        outcomes.append(outcome)
        for line in outcome.events or ((f"{outcome.item}: {outcome.status}: {outcome.detail}",) if outcome.detail else ()):
            ports.log(line)
        return outcome

    for item in [i for i in items if i["kind"] == ls.PHASE_MERGE][:max(phase_cap, 0)]:
        do(item)
    # Read again: a phase merged just now may have assembled its initiative, which then lands in this same pass.
    worked = 0
    for item in [i for i in ls.claimable(ls.list_items(ports.conn), ports.now()) if i["kind"] == ls.INITIATIVE_LAND]:
        if worked >= land_cap:
            break
        # A land left waiting (a pull request a person has yet to merge, checks still running, another land ahead
        # of it) did no work, so it does not use the cap: the next initiative gets its turn.
        if do(item).status != "waiting":
            worked += 1
    return outcomes
