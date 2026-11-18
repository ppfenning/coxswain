"""The lander: the worker that owns landing, apart from the chair's build tick.

It reads the land queue and performs two kinds of item. A `phase_merge` merges one built phase into its initiative
branch, locally: that is "phase complete". An `initiative_land` takes a whole initiative to its parent: refresh onto
the parent, gate checks, push, the ONE pull request, its checks (read once per pass, never waited on), the merge, and
the bookkeeping. No phase ever gets a pull request. A pass works one lane per repository, and first checks each
landing record against git (`reconcile`).

The decisions are pure functions (`land_step`, `phase_drift`, `lanes` and their helpers), tested on example inputs.

The lander is a script, not an agent. It never resolves a conflict and never edits code: a conflict or a red check is
recorded on the item and sent back to build as a rectify task. Everything it does can be run again: each step reads
what is already true (the record, the branch, the forge) before it acts.

Every effect goes through `Ports`, so the whole worker runs in tests against a real repo, a real store and a fake forge.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

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
from harness import store_lease

LAND_CLAIM_SECONDS = 3600  # an initiative land waits on CI; its claim is renewed while it works
QUEUE_ALARM_SECONDS = 3600  # an item still open this long after it was queued is told to the chair, once
PERSON_GATE_NOTE = "the pull request is open and its merge waits for a person"
PREPARED_NOTE = "the pull request is open and waits for the rest of its batch"


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


@dataclass(frozen=True)
class Outcome:
    """What one item came to. `status`: done, waiting (left queued), failed, skipped."""

    status: str
    item: str
    detail: str = ""
    events: tuple[str, ...] = field(default_factory=tuple)


# -- the pure core ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LandFacts:
    """What one land knows once its pull request is open. `checks` is one poll of the forge, `(state, detail)` with
    state `green`, `pending`, `rerun` or `failed`; None until it has been read."""

    url: str
    branch: str
    merged: bool = False  # the forge says the pull request has merged
    auto: bool = True  # gate `full`: the lander merges it, not a person
    prepare_only: bool = False  # a batch's first step: stop once the pull request is open
    stacked_on: str | None = None  # the layer below, while it has not landed
    has_ci: bool | None = None  # whether the branch carries CI the forge reports on; None until it has been read
    checks: tuple[str, str] | None = None  # state: green, pending, rerun, failed or error (unreadable past the limit)


@dataclass(frozen=True)
class LandStep:
    """What a land does next. `action`: finish (it merged), wait (left queued for the next pass), read_ci (ask
    whether the branch has CI, then decide again), poll (read the checks once, then decide again), merge, or
    send_back (red checks: an `integrate` task)."""

    action: str
    detail: str = ""
    note: str = ""  # an event to report on the way


def land_step(facts: LandFacts) -> LandStep:
    """Pure. The land's next step. Checks are read once per pass and never waited on: pending is `wait`."""
    if facts.merged:
        return LandStep("finish")
    if facts.stacked_on is not None:
        return LandStep("wait", f"{facts.url} is stacked on {facts.stacked_on}, which has not landed")
    if not facts.auto:
        return LandStep("wait", f"{PERSON_GATE_NOTE}: {facts.url}")
    if facts.prepare_only:
        return LandStep("wait", f"{PREPARED_NOTE}: {facts.url}")
    if facts.has_ci is None:
        return LandStep("read_ci")
    if not facts.has_ci:
        # A branch with no CI workflow has nothing the forge could ever report. Its checks are the gate checks
        # `_open_pr` ran on this branch before the push.
        return LandStep("merge", note=f"no CI on {facts.branch}: the gate checks run before the pull request opened are its checks")
    if facts.checks is None:
        return LandStep("poll")
    state, detail = facts.checks
    if state == "green":
        return LandStep("merge")
    if state == "pending":
        return LandStep("wait", f"checks on {facts.url} have not finished: {detail}")
    if state == "rerun":
        return LandStep("wait", f"checks on {facts.url} failed and were rerun once: {detail}")
    if state == "error":
        # Past the read limit it is not the change that is wrong but the reading (a token, the forge): the land waits,
        # and a person is told once (`tell_unreadable`). Failing it would only queue it again, every pass, unseen.
        return LandStep("wait", f"checks on {facts.url} cannot be read: {detail}")
    return LandStep("send_back", detail)


@dataclass(frozen=True)
class Drift:
    """One merged phase the initiative branch does not hold. `action`: remerge (its tip is here to merge again) or
    hold (it cannot be repaired here: a person decides)."""

    phase: str
    action: str
    reason: str
    tip: str = ""


def merged_entries(record: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Pure. (phase, tip, merge commit) for each phase the record says is merged, in natural order."""
    phases = record.get("phases") or {}
    found = []
    for phase in sorted(phases, key=lr.natural_key):
        entry = phases[phase] or {}
        if entry.get("state") == "merged":
            found.append((str(phase), str(entry.get("tip") or ""), str(entry.get("merge_sha") or "")))
    return found


def phase_drift(record: Mapping[str, Any], on_branch: Mapping[str, bool], known: Mapping[str, bool]) -> list[Drift]:
    """Pure. Each merged phase whose tip or merge commit the initiative branch does not hold, and what to do with it.

    `on_branch[sha]` says whether the branch holds a recorded sha; `known[sha]` whether this clone has the commit at
    all. A phase whose tip is here is merged again (a tip the branch already holds only moves the record). One whose
    tip is nowhere here, or that recorded no tip, is held with the reason."""
    branch = str(record.get("branch") or "")
    drifts = []
    for phase, tip, merge in merged_entries(record):
        missing = [sha for sha in (tip, merge) if sha and not on_branch.get(sha, False)]
        if not missing:
            continue
        said = f"phase {phase} is recorded merged at {(merge or tip)[:12]}, but {branch} does not hold {missing[0][:12]}"
        if not tip:
            drifts.append(Drift(phase, "hold", f"{said}, and no phase tip is recorded to merge again"))
        elif on_branch.get(tip, False) or known.get(tip, False):
            drifts.append(Drift(phase, "remerge", f"{said}; its tip {tip[:12]} is merged again", tip))
        else:
            drifts.append(Drift(phase, "hold", f"{said}, and this clone has no copy of its tip {tip[:12]} to merge again"))
    return drifts


def kept_shas(record: Mapping[str, Any], phases: Sequence[str]) -> list[str]:
    """Pure. The recorded shas a repair must not drop: every one but those of the phases being repaired."""
    entries = record.get("phases") or {}
    doubt = {str(sha) for p in phases for sha in ((entries.get(p) or {}).get("tip"), (entries.get(p) or {}).get("merge_sha")) if sha}
    return [sha for sha in lr.recorded_shas(cast(lr.LandingRecord, record)) if sha not in doubt]


def unheld_shas(record: Mapping[str, Any], holds: Mapping[str, bool]) -> list[str]:
    """Pure. The tips and merge commits of the record's merged phases that the branch (per `holds`) does not hold."""
    return [sha for _, tip, merge in merged_entries(record) for sha in (tip, merge) if sha and not holds.get(sha, False)]


# The parts of a forge's error text that change from one read to the next: times, request ids, hashes, numbers.
_VARIABLE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"  # a timestamp
    r"|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"  # a uuid
    r"|\b[0-9A-Fa-f]+(:[0-9A-Fa-f]+)+\b"  # a request id like 9C1F:2B3A:1A2B3C4
    r"|\b(?=[0-9A-Fa-f]*\d)[0-9A-Fa-f]{7,}\b"  # a hex id or a sha
    r"|\d+(\.\d+)?"  # any other number
)


def reason_key(reason: str) -> str:
    """Pure. A reason with its variable parts replaced by `#`, so the same failure read twice is the same reason."""
    return " ".join(_VARIABLE.sub("#", reason).split())


def tell_unreadable(memo: Mapping[str, Any], state: str) -> tuple[dict[str, Any], str | None]:
    """Pure. Checks unreadable past the limit (`error`) are told to a person once per commit and reason: the reason
    to tell, or None, and the memo that remembers it was told. The memo is fresh for a new commit (`land.poll_memo`).
    Reasons compare by `reason_key`, so a new request id or time in the same error is not news."""
    after = dict(memo)
    reason = str(after.get("unreadable") or "")
    if state != "error" or after.get("told") == reason_key(reason):
        return after, None
    return {**after, "told": reason_key(reason)}, reason


def held_after(held: Mapping[str, Any] | None, reason: str | None, now: str) -> tuple[dict[str, Any] | None, bool]:
    """Pure. The record's `held` once reconciliation found `reason` (None: the record and the branch agree), and
    whether that is news to tell: a new hold, a changed reason, or a hold lifted. An unchanged hold is not told twice."""
    old = str((held or {}).get("reason") or "")
    if reason is None:
        return None, bool(old)
    if reason == old:
        return dict(held or {}), False
    return {"reason": reason, "since": now}, True


@dataclass(frozen=True)
class Lane:
    """One repository's work for a pass: the initiatives to reconcile and the queue items to perform."""

    repo: str
    initiatives: tuple[str, ...]
    items: tuple[Mapping[str, Any], ...]


def lanes(records: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]], where: Mapping[str, str]) -> list[Lane]:
    """Pure. A pass's work split per repository, so one repository's slow checks never hold another's.

    `where[initiative]` is the repository that initiative lands in on this machine; an item of an initiative not in it
    goes by the repo it names. Lanes come in the order their first record or item does, and keep the queue's order."""
    order: list[str] = []
    initiatives: dict[str, list[str]] = {}
    queued: dict[str, list[Mapping[str, Any]]] = {}

    def lane_of(initiative: str, named: Any) -> str:
        key = where.get(initiative) or str(named or "")
        if key not in initiatives:
            order.append(key)
            initiatives[key], queued[key] = [], []
        return key

    for record in records:
        initiative = str(record["initiative"])
        initiatives[lane_of(initiative, record.get("repo"))].append(initiative)
    for item in items:
        queued[lane_of(str(item["initiative"]), item.get("repo"))].append(item)
    return [Lane(key, tuple(initiatives[key]), tuple(queued[key])) for key in order]


def published(record: Mapping[str, Any]) -> bool:
    """Pure. Whether the initiative's branch is the lander's to judge: assembled, or with a pull request open. Before
    that the host that builds it owns the branch, and a phase it has not pushed yet is not a phase lost. A pull request
    URL counts in any state, so an initiative sent back to build (`conflicted`) after its pull request opened is still
    judged: the open pull request must keep every phase merged before it."""
    return record.get("state") in ("assembled", "pr_open") or bool((record.get("pr") or {}).get("url"))


def live(record: Mapping[str, Any]) -> bool:
    """Pure. A record the lander still works: not landed, and landing as an initiative (not per task, not an epic)."""
    return record.get("state") != "landed" and record.get("land_at") not in ("task", "epic")


def ports_repo(ports: Ports, record: Mapping[str, Any]) -> Path:
    found = ports.repo(record) if ports.repo is not None else Path(str(record.get("repo") or "")).expanduser()
    if not str(found).strip(".") or not found.is_absolute():
        # Never the current directory: an empty path would run git in whatever checkout the process happens to be in.
        raise LookupError(f"{record.get('initiative')}: its repository is not known on this machine")
    return found


def parent_ref_of(repo: Path, parent: str) -> str:
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
    repo, tasks = ports_repo(ports, record), ports.tasks(initiative)
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
    repo = ports_repo(ports, record)
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
        body = ops.rectify_body(branch, phase, done.paths, "")
    else:
        title = f"Rectify phase {phase}: its checks fail once merged with {branch}"
        merged = lg.merge_into(repo, branch=ops.phase_branch(initiative, phase), source=branch,
                               message=f"{initiative}: merge {branch} into phase {phase}")
        if not merged.ok:
            ports.notify("needs_chair", {"initiative": initiative, "phase": phase, "reason": merged.detail})
            return [f"phase {phase} of {initiative}: could not prepare the rectify base: {merged.detail}"]
        body = ops.rectify_body(branch, phase, (), done.detail)
    task = ports.file_rectify(initiative, phase, title, body)
    if task is None:
        ports.notify("needs_chair", {"initiative": initiative, "phase": phase, "reason": done.detail})
        return [f"phase {phase} of {initiative} is conflicted and no rectify task could be filed: {done.detail}"]
    return [f"phase {phase} of {initiative} sent back to build: rectify task {task} ({done.detail})"]


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


def land_initiative(ports: Ports, initiative: str, *, merge: bool = True, refresh_when_behind: bool = True) -> Outcome:
    """Take one assembled initiative to its parent. Safe to run again at any point.

    A batch drives it in two steps. `merge=False` stops once the pull request is open on a refreshed branch (the
    outcome is `waiting`, with `PREPARED_NOTE`). `refresh_when_behind=False` then merges it as it stands: the batch
    has already checked the members together on the parent, so a parent that moved by a sibling of the batch is not
    a reason to refresh and wait for the checks again."""
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
    if record.get("held"):
        return close("waiting", held_note(record))
    record, stack_events, below = _restack(ports, record)
    first = _other_land_on_parent(ports, ls.get_item(ports.conn, ident) or {})
    if first is not None:
        return close("waiting", f"{first} is landing on {record.get('parent')} first")
    tasks = ports.tasks(initiative)
    state = lr.initiative_state(record, tasks)
    if state == "landed":
        return close("done", f"{initiative} already landed", _finish_landed(ports, record, tasks, None))
    if state not in ("assembled", "pr_open"):
        return close("waiting", f"{initiative} is {state}, not assembled")
    repo, branch, parent = ports_repo(ports, record), str(record["branch"]), str(record["parent"])
    meta = ports.meta(initiative)
    events: list[str] = list(stack_events)
    auto = str(meta.get("gate") or "full") == "full"
    stacked = bool(meta.get("epic_stack"))
    merged, _ = ports.forge.pr_merged(repo, branch) if (record.get("pr") or {}).get("url") else (False, "")
    # An open pull request the lander itself will merge is refreshed when its parent has moved under it. One that
    # waits for a person is left as that person last saw it.
    behind = (auto and refresh_when_behind and not merged
              and (lg.ahead_behind(repo, branch, parent_ref_of(repo, parent)) or (0, 0))[1] > 0)
    if not merged and (state == "assembled" or not (record.get("pr") or {}).get("url") or behind):
        opened = _open_pr(ports, record, tasks, meta, repo, branch, parent)
        if isinstance(opened, Outcome):
            return close(opened.status, opened.detail, opened.events)
        record, new_events = opened
        events += new_events
    url = str((record.get("pr") or {}).get("url") or "")
    if not merged:
        merged, _ = ports.forge.pr_merged(repo, branch)
    facts = LandFacts(url=url, branch=branch, merged=merged, auto=auto, prepare_only=not merge, stacked_on=below)
    step = land_step(facts)
    while step.action in ("read_ci", "poll"):
        facts = (replace(facts, has_ci=ports.forge.has_ci(repo, branch)) if step.action == "read_ci"
                 else replace(facts, checks=_poll_checks(ports, repo, initiative, branch)))
        step = land_step(facts)
    if step.note:
        events.append(step.note)
    if step.action == "wait":
        return close("waiting", step.detail, events)
    if step.action == "send_back":
        return close("failed", f"checks failed on {url}: {step.detail}",
                     [*events, *_send_back_initiative(ports, record, "checks", step.detail)])
    if step.action == "merge":
        unheld = _unheld_at_merge(repo, ls.get_record(ports.conn, initiative) or record)
        if unheld:
            return close("waiting", unheld, events)
        # Into an epic branch an initiative merges with a merge commit, so the layers above it retarget cleanly; the
        # squash happens once, from the epic branch to its parent.
        ok, detail = ports.forge.merge(repo, {"kind": "merge", "squash": not stacked, "delete_branch": True, "branch": branch,
                                             "default_branch": parent, "subject": str((record.get("pr") or {}).get("title") or initiative)})
        if not ok:
            return close("failed", f"merge of {url} failed: {detail}", events)
        events.append(f"merged {url} into {parent}")
    events += _finish_landed(ports, record, tasks, url)
    return close("done", f"{initiative} landed on {parent}", events)


def held_note(record: Mapping[str, Any]) -> str:
    """Pure. Why a held initiative waits, for its queue item."""
    return f"{record.get('initiative')} is held: {(record.get('held') or {}).get('reason') or 'its record and its branch disagree'}"


def _poll_checks(ports: Ports, repo: Path, initiative: str, branch: str) -> tuple[str, str]:
    """Read the branch's checks once. What the poll keeps between passes (failed reads in a row, the reruns made of
    this commit) goes on the record as `checks_poll`, so the bounds hold across passes and a rerun is made once."""
    current = ls.get_record(ports.conn, initiative) or {}
    state, detail, memo = ports.forge.poll_checks(repo, ref=branch, memo=current.get("checks_poll"))
    memo, tell = tell_unreadable(memo, state)
    if current and memo != current.get("checks_poll"):
        ls.put_record(ports.conn, {**current, "checks_poll": memo, "updated_at": ports.now()})
    if tell is not None:
        ports.notify("needs_chair", {"initiative": initiative,
                                     "reason": f"the checks of {branch} at {str(memo.get('head') or '')[:12]} cannot be read: {tell}"})
    return state, detail


def _unheld_at_merge(repo: Path, record: Mapping[str, Any]) -> str | None:
    """The guard right before the merge: why it waits, when the branch that would merge (origin's, else this clone's
    for a forge that merges locally) lacks a commit the record says a phase merged with. None when it holds them all."""
    branch = str(record["branch"])
    origin_ref, unreadable = _fetch_origin(repo, branch)
    if unreadable:
        return f"origin's {branch} could not be read before the merge: {unreadable}"
    ref = origin_ref or f"refs/heads/{branch}"
    shas = [sha for _, tip, merge in merged_entries(record) for sha in (tip, merge) if sha]
    missing = unheld_shas(record, {sha: lg.is_ancestor(repo, sha, ref) for sha in shas})
    if not missing:
        return None
    name = ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")
    return f"{name} lacks recorded commit(s) {', '.join(s[:12] for s in missing)}: the merge waits until it holds them"


def _restack(ports: Ports, record: lr.LandingRecord) -> tuple[lr.LandingRecord, list[str], str | None]:
    """The stack step of a land. (the record, events, the initiative this one is still stacked on or None).

    When the record's parent is another initiative's branch, that initiative is the layer below. While it has not
    landed this one waits on it. Once it has, this one moves down onto that initiative's own parent: the record's
    parent changes, the open pull request is retargeted, and the new parent is merged forward by the ordinary
    refresh. Nothing is rebased."""
    parent = str(record.get("parent") or "")
    below = next((r for r in ls.list_records(ports.conn)
                  if r.get("branch") == parent and r.get("initiative") != record.get("initiative")), None)
    if below is None:
        return record, [], None
    if below.get("state") != "landed":
        return record, [], str(below["initiative"])
    new_parent, initiative = str(below.get("parent") or ""), str(record["initiative"])
    updated: lr.LandingRecord = {**record, "parent": new_parent, "updated_at": ports.now()}
    if isinstance(updated.get("pr"), Mapping):
        updated["pr"] = {**cast(lr.PullRequest, updated["pr"]), "base": new_parent}
    ls.put_record(ports.conn, updated)
    events = [f"{initiative}: {below['initiative']} landed; its parent is now {new_parent}"]
    if (record.get("pr") or {}).get("url"):
        ok, why = ports.forge.retarget(ports_repo(ports, record), str(record["branch"]), new_parent)
        events.append(f"{initiative}: pull request retargeted to {new_parent}" if ok
                      else f"{initiative}: pull request not retargeted to {new_parent}: {why}")
    return updated, events, None


def land_epics(ports: Ports) -> list[str]:
    """With `epic_stack` on, open the epic branch's one pull request once every member initiative has landed.

    The epic's record is kept under `epic:<id>`. The pull request is opened once and left for the gate that merges it."""
    events: list[str] = []
    by_epic: dict[str, list[Mapping[str, Any]]] = {}
    for record in ls.list_records(ports.conn):
        if record.get("epic") and not str(record["initiative"]).startswith("epic:"):
            by_epic.setdefault(str(record["epic"]), []).append(record)
    for epic, members in sorted(by_epic.items()):
        key = f"epic:{epic}"
        if ls.get_record(ports.conn, key) is not None or any(m.get("state") != "landed" for m in members):
            continue
        meta = ports.meta(str(members[0]["initiative"]))
        branch, parent = meta.get("epic_branch"), meta.get("epic_parent")
        if not meta.get("epic_stack") or not branch or not parent:
            continue
        repo = ports_repo(ports, members[0])
        title = str(meta.get("epic_title") or epic)
        body = "\n".join([title, "", *(f"- {m['initiative']}: {m.get('landed_pr') or ''}".rstrip() for m in members), ""])
        ok, url = ports.forge.open_pr(repo, title, body, head=str(branch), base=str(parent))
        if not ok:
            events.append(f"epic {epic}: pull request for {branch} was not opened: {url}")
            continue
        now = ports.now()
        ls.put_record(ports.conn, {"initiative": key, "repo": str(members[0].get("repo") or ""), "branch": branch,
                                   "parent": parent, "state": "pr_open", "land_at": "epic", "phases": {},
                                   "pr": {"url": url, "title": title, "head": branch, "base": parent},
                                   "created_at": now, "updated_at": now})
        events.append(f"epic {epic}: every initiative has landed; opened {url} from {branch} to {parent}")
    return events


def _open_pr(ports: Ports, record: lr.LandingRecord, tasks: Sequence[Mapping[str, Any]], meta: Mapping[str, Any], repo: Path,
             branch: str, parent: str) -> tuple[lr.LandingRecord, list[str]] | Outcome:
    """Refresh onto the parent, run the gate checks, push, and open the one pull request (or find the one already open)."""
    initiative, ident = str(record["initiative"]), ls.item_id(ls.INITIATIVE_LAND, str(record["initiative"]))
    recorded = lr.recorded_shas(record)
    refreshed = ops.take_parent_forward(ports.conn, repo, initiative=initiative, parent_ref=parent_ref_of(repo, parent),
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
    repo, phase = ports_repo(ports, record), lr.INTEGRATE_PHASE
    integrate = ops.phase_branch(initiative, phase)
    tip, _, why = lg.ensure_branch(repo, integrate, branch)
    if tip is None:
        ports.notify("needs_chair", {"initiative": initiative, "reason": why})
        return [f"{initiative}: could not cut {integrate}: {why}"]
    lg.merge_into(repo, branch=integrate, source=branch, message=f"{initiative}: bring {integrate} up to {branch}")
    if cause == "conflict":
        marked = lg.conflict_commit(repo, branch=integrate, source=parent_ref_of(repo, parent),
                                    message=f"{initiative}: merge {parent} into {integrate} (conflicted, to rectify)")
        if marked.status == "error":
            ports.notify("needs_chair", {"initiative": initiative, "reason": marked.detail})
            return [f"{initiative}: could not prepare the integrate base: {marked.detail}"]
        title, body = f"Integrate with {parent}: resolve the conflicts", ops.rectify_body(parent, phase, paths or marked.paths, "")
    else:
        title, body = f"Integrate with {parent}: the gate checks fail", ops.rectify_body(parent, phase, (), detail)
    task = ports.file_rectify(initiative, phase, title, body)
    if task is None:
        ports.notify("needs_chair", {"initiative": initiative, "reason": detail})
        return [f"{initiative} cannot land and no integrate task could be filed: {detail}"]
    current = ls.get_record(ports.conn, initiative) or cast(lr.LandingRecord, dict(record))
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
    current: lr.LandingRecord = ls.get_record(ports.conn, initiative) or cast(lr.LandingRecord, dict(record))
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
    initiative, repo = str(record["initiative"]), ports_repo(ports, record)
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

def settle_blocked(ports: Ports, record: lr.LandingRecord, tasks: Sequence[Mapping[str, Any]]) -> list[str]:
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
    current = cast(lr.LandingRecord, dict(record))
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

def sweep(ports: Ports, initiatives: Collection[str] | None = None) -> list[str]:
    """Queue what an event missed. It never does the work itself.

    Every built phase of a live record gets a `phase_merge` item, every assembled initiative an `initiative_land`,
    and an initiative with a blocked phase has its `on_blocked` policy applied. With `initiatives`, only those: a lane
    sweeps its own, so a record is only ever read and written by the lane that owns it."""
    events: list[str] = []
    for record in ls.list_records(ports.conn):
        if not live(record) or (initiatives is not None and str(record["initiative"]) not in initiatives):
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


def queue_alarm(ports: Ports, limit: int = QUEUE_ALARM_SECONDS, initiatives: Collection[str] | None = None) -> list[str]:
    """Tell the chair, once per queueing, about each item that has waited past `limit`: what it is, since when, and
    the last error. This is the stall alarm's view of landing: a lander that dies, or an item nothing can finish,
    shows up here instead of as silence. With `initiatives`, only their items (a lane's own)."""
    events: list[str] = []
    items = [i for i in ls.list_items(ports.conn) if initiatives is None or str(i["initiative"]) in initiatives]
    for item in stale_items(items, ports.now(), limit):
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


# -- batches ------------------------------------------------------------------------------------------------------

def changed_paths(repo: Path, branch: str, parent_ref: str) -> frozenset[str] | None:
    """The files the initiative branch changes against its parent (from their merge base); None when git cannot say."""
    ok, out = lg.git(repo, "diff", "--name-only", f"{parent_ref}...{branch}")
    return frozenset(p for p in out.splitlines() if p) if ok else None


def disjoint_batches(items: Sequence[Mapping[str, Any]], paths: Mapping[str, frozenset[str] | None], size: int) -> list[list[Mapping[str, Any]]]:
    """Pure. The queue's lands grouped for landing together, in queue order.

    Members of a batch share a repo and a parent and change no file in common. An item goes into the first batch of
    its repo and parent that it does not overlap and that has room; else it starts one. An item whose files are not
    known overlaps everything. With `size` 1 every batch is one item, which is landing one at a time."""
    batches: list[list[Mapping[str, Any]]] = []
    for item in items:
        mine = paths.get(str(item["id"]))
        home = next(
            (b for b in batches
             if len(b) < size and mine is not None and (b[0]["repo"], b[0]["parent"]) == (item["repo"], item["parent"])
             and all(paths.get(str(o["id"])) is not None and not (mine & paths[str(o["id"])]) for o in b)),  # type: ignore[operator]
            None,
        )
        if home is None:
            batches.append([item])
        else:
            home.append(item)
    return batches


def _check_together(ports: Ports, repo: Path, parent_ref: str, branches: Sequence[str]) -> tuple[bool, str]:
    """Merge every member onto the parent in a scratch tree and run the gate checks there, once, for the batch."""
    def check(tree: Path) -> tuple[bool, str]:
        for branch in branches:
            ok, out = lg.git(tree, *lg.IDENTITY, "merge", "--no-ff", "--no-edit", "-m", f"batch: {branch}", branch)
            if not ok:
                lg.git(tree, "merge", "--abort")
                return False, f"{branch} does not merge with the rest of the batch: {out.splitlines()[0] if out else 'conflict'}"
        return ports.checks(repo, tree, "ticket")

    return lg.on_scratch(repo, parent_ref, check)


def land_batch(ports: Ports, items: Sequence[Mapping[str, Any]]) -> list[Outcome]:
    """Land several initiatives that change no file in common, each through its own pull request.

    Each is refreshed onto the same parent and its pull request opened, so their checks run side by side. The members
    are then merged together on the parent in a scratch tree and the gate checks run there once. When that passes,
    each pull request is merged in queue order without refreshing again. A member that is not ready (a person merges
    it, its checks are red, it conflicts) drops out and is handled as a single land; when the members do not pass
    together, only the first is landed and the rest wait for the next pass."""
    if len(items) < 2:
        return [land_initiative(ports, str(i["initiative"])) for i in items]
    prepared, outcomes = [], []
    for item in items:
        outcome = land_initiative(ports, str(item["initiative"]), merge=False)
        if outcome.status == "waiting" and outcome.detail.startswith(PREPARED_NOTE):
            prepared.append(item)
        else:
            outcomes.append(outcome)  # landed already, waiting on a person or a stack, sent back, or failed
    if len(prepared) < 2:
        return [*outcomes, *(land_initiative(ports, str(i["initiative"])) for i in prepared)]
    records = [ls.get_record(ports.conn, str(i["initiative"])) or {} for i in prepared]
    repo, parent = ports_repo(ports, records[0]), str(records[0].get("parent") or "")
    passed, why = _check_together(ports, repo, parent_ref_of(repo, parent), [str(r["branch"]) for r in records])
    if not passed:
        first = land_initiative(ports, str(prepared[0]["initiative"]))
        note = f"batch of {len(prepared)} on {parent} does not pass together ({why.splitlines()[0] if why else 'checks failed'}); landing one at a time"
        return [*outcomes, Outcome(first.status, first.item, first.detail, (note, *first.events))]
    names = ", ".join(str(i["initiative"]) for i in prepared)
    for n, item in enumerate(prepared):
        outcome = land_initiative(ports, str(item["initiative"]), refresh_when_behind=False)
        lead = (f"batch on {parent}: {names} change no file in common and pass together",) if n == 0 else ()
        outcomes.append(Outcome(outcome.status, outcome.item, outcome.detail, (*lead, *outcome.events)))
    return outcomes


def _land_paths(ports: Ports, items: Sequence[Mapping[str, Any]]) -> dict[str, frozenset[str] | None]:
    """Each queued land's changed files, for grouping; None for one whose record or repository cannot be read."""
    found: dict[str, frozenset[str] | None] = {}
    for item in items:
        record = ls.get_record(ports.conn, str(item["initiative"]))
        try:
            repo = ports_repo(ports, record) if record else None
        except LookupError:
            repo = None
        found[str(item["id"])] = (
            changed_paths(repo, str(record["branch"]), parent_ref_of(repo, str(record.get("parent") or "")))
            if record and repo is not None else None
        )
    return found


def perform(ports: Ports, item: Mapping[str, Any]) -> Outcome:
    if item["kind"] == ls.PHASE_MERGE:
        return merge_phase(ports, str(item["initiative"]), str(item["phase"]))
    return land_initiative(ports, str(item["initiative"]))


# -- reconciliation -----------------------------------------------------------------------------------------------

def _origin_drift(repo: Path, record: Mapping[str, Any], origin_ref: str) -> list[Drift]:
    """The record's merged phases against origin's copy of the initiative branch, as last fetched."""
    shas = [sha for _, tip, merge in merged_entries(record) for sha in (tip, merge) if sha]
    on_branch = {sha: lg.is_ancestor(repo, sha, origin_ref) for sha in shas}
    known = {sha: on_branch[sha] or lg.rev(repo, sha) is not None for sha in shas}
    return phase_drift(record, on_branch, known)


def _fetch_origin(repo: Path, branch: str) -> tuple[str | None, str]:
    """Fetch origin's `branch` into its remote-tracking ref. (the ref, '') when fetched; (None, '') when origin has
    no such branch; (None, why) when origin could not be read."""
    ref = f"refs/remotes/origin/{branch}"
    if not lg.git(repo, "remote", "get-url", "origin")[0]:
        return None, ""  # a repository with no origin: this clone is all there is
    ok, out = lg.git(repo, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{ref}")
    if ok:
        return ref, ""
    return None, "" if "couldn't find remote ref" in out.lower() else out.splitlines()[0] if out else "fetch failed"


@dataclass(frozen=True)
class Judgement:
    """What one reconciliation found. `reason` holds the initiative (None: the record and origin agree). `judged`
    False means the judgement waits for another pass, and an existing hold stands as it is."""

    events: tuple[str, ...] = ()
    reason: str | None = None
    judged: bool = True


def _repair(ports: Ports, repo: Path, record: Mapping[str, Any], repairs: Sequence[Drift], origin_ref: str) -> Judgement:
    """Under the branch lease: merge each repairable phase into origin's branch as it stands, push it (a plain push,
    which origin refuses unless it is a fast-forward), and only then rewrite the record. A merge that fails holds,
    with why; a push origin refuses puts the local branch back and waits for the next pass."""
    initiative, branch = str(record["initiative"]), str(record["branch"])
    start, local = lg.rev(repo, origin_ref), lg.rev(repo, f"refs/heads/{branch}")
    if start is None:
        return Judgement((f"reconcile: {initiative} waits: {origin_ref} is not a commit",), judged=False)
    if local is None:
        moved, why = lg.git(repo, "branch", "--no-track", branch, start)
    elif local != start and lg.is_ancestor(repo, local, start):
        moved, why = lg.git(repo, "update-ref", f"refs/heads/{branch}", start, local)
    elif local != start and lg.is_ancestor(repo, start, local):
        # This clone is ahead: a repair (or a merge) made here and never pushed, say by a lander that died between
        # its merge and its push. Build on it, and the push below publishes it as a fast-forward.
        start, moved, why = local, True, ""
    elif local != start:
        return Judgement((f"reconcile: {initiative} waits: {branch} here has diverged from origin's, so it is not repaired here",),
                         judged=False)
    else:
        moved, why = True, ""
    if not moved:
        return Judgement((f"reconcile: {initiative} waits: {branch} could not be brought to origin's tip: {why}",), judged=False)
    kept = kept_shas(record, [d.phase for d in repairs])
    for drift in repairs:
        merged = lg.merge_into(repo, branch=branch, source=drift.tip, recorded=kept,
                               message=f"{initiative}: merge phase {drift.phase} again (the record said merged; {branch} did not hold it)")
        if not merged.ok:
            lg.git(repo, "update-ref", f"refs/heads/{branch}", start)
            return Judgement(reason=f"{drift.reason}, but that merge failed: {merged.detail}")
    tip = lg.rev(repo, f"refs/heads/{branch}") or start
    pushed, why = lg.git(repo, "push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
    if not pushed:
        lg.git(repo, "update-ref", f"refs/heads/{branch}", start, tip)
        return Judgement((f"reconcile: {initiative} waits: origin refused the repaired {branch}: {why.splitlines()[0] if why else 'push failed'}",),
                         judged=False)
    lg.git(repo, "update-ref", origin_ref, tip)
    current = ls.get_record(ports.conn, initiative) or cast(lr.LandingRecord, dict(record))
    for drift in repairs:
        current = lr.with_phase(current, drift.phase, ports.now(), merge_sha=tip, repaired_at=ports.now())
    ls.put_record(ports.conn, current)
    return Judgement(tuple(f"reconcile: {initiative}: {d.reason}; {branch} is pushed at {tip[:12]}" for d in repairs))


def _judge(ports: Ports, repo: Path, initiative: str) -> Judgement:
    """Under the branch lease: the record, read again, against origin's branch, fetched now."""
    record = ls.get_record(ports.conn, initiative)
    if record is None or not published(record):
        return Judgement(judged=False)
    branch = str(record["branch"])
    origin_ref, unreadable = _fetch_origin(repo, branch)
    if origin_ref is None:
        # Not on origin: not pushed yet, so not the lander's to judge. Unreadable: try again next pass.
        return Judgement((f"reconcile: {initiative} waits: origin could not be read: {unreadable}",) if unreadable else (), judged=False)
    drifts = _origin_drift(repo, record, origin_ref)
    if drifts:
        for drift in drifts:  # a tip may still be on origin's copy of its phase branch
            phase = ops.phase_branch(initiative, drift.phase)
            lg.git(repo, "fetch", "--quiet", "origin", f"+refs/heads/{phase}:refs/remotes/origin/{phase}")
        drifts = _origin_drift(repo, record, origin_ref)
    holds = [d.reason for d in drifts if d.action == "hold"]
    repairs = [d for d in drifts if d.action == "remerge"]
    if not repairs:
        return Judgement(reason="; ".join(holds) or None)
    repaired = _repair(ports, repo, record, repairs, origin_ref)
    if not repaired.judged:
        return repaired
    return Judgement(repaired.events, "; ".join([*holds, *([repaired.reason] if repaired.reason else [])]) or None)


def reconcile(ports: Ports, initiative: str) -> list[str]:
    """Check what the landing record says merged against origin's initiative branch, and act on what disagrees.

    Only a published initiative is judged: its record is assembled or has a pull request open, and its branch is on
    origin. Before that the builder's host owns the branch, and a phase not pushed yet is not a phase lost. The whole
    judgement runs under the initiative's branch lease, after a fetch, and is made again on what that fetch brought.

    A phase recorded merged whose tip this clone has, or can fetch from origin's phase branch, is merged into origin's
    branch again and pushed; its merge commit is rewritten on the record only once the push is taken. One that cannot
    be repaired holds the initiative, with the reason, and the chair is told once: nothing merges into it or lands
    from it until the record and origin agree again, which lifts the hold on the pass that sees it. A busy lease waits."""
    record = ls.get_record(ports.conn, initiative)
    if record is None or not live(record) or not merged_entries(record) or not published(record):
        return []
    try:
        repo = ports_repo(ports, record)
    except LookupError:
        return []
    lease = store_lease.acquire(ports.conn, ops.branch_lease(initiative), ports.worker, ports.now(), ops.CLAIM_SECONDS)
    if not lease.ok:
        return [f"reconcile: {initiative} waits: {lease.holder or 'another worker'} is moving {record['branch']}"]
    try:
        judged = _judge(ports, repo, initiative)
        events = list(judged.events)
        if not judged.judged:
            return events
        current = ls.get_record(ports.conn, initiative) or record
        held, news = held_after(current.get("held"), judged.reason, ports.now())
        if news:
            ls.put_record(ports.conn, {**current, "held": held, "updated_at": ports.now()})
            if held is None:
                events.append(f"reconcile: {initiative}: the record and origin's {record['branch']} agree again; the hold is lifted")
            else:
                ports.notify("needs_chair", {"initiative": initiative, "reason": f"held: {held['reason']}"})
                events.append(f"reconcile: {initiative} is held: {held['reason']}")
        return events
    finally:
        store_lease.release(ports.conn, ops.branch_lease(initiative), ports.worker, int(lease.epoch or 0))


# -- the pass -----------------------------------------------------------------------------------------------------

def plan_pass(ports: Ports) -> list[Lane]:
    """The shared start of a pass, which writes no initiative's record: the epics' pull requests, then the pass's
    work split into one lane per repository. Everything per initiative (the sweep, the alarm, reconciliation, the
    items) happens in the lane that owns it, so a record has one writer in a pass while lanes run side by side."""
    for line in land_epics(ports):
        ports.log(line)
    records = [r for r in ls.list_records(ports.conn) if live(r)]
    where: dict[str, str] = {}
    for record in records:
        try:
            where[str(record["initiative"])] = str(ports_repo(ports, record))
        except LookupError:
            continue
    return lanes(records, ls.claimable(ls.list_items(ports.conn), ports.now()), where)


def run_lane(ports: Ports, lane: Lane, *, phase_cap: int = 2, land_cap: int = 1, land_batch_size: int = 1) -> list[Outcome]:
    """One repository's share of a pass: sweep its initiatives, tell the chair of its stalled items, reconcile, then
    perform its items, oldest first, within the two caps. The caps count per repository, so no repository waits on
    another's turn.

    An item that is waiting stays queued for the next pass. With `land_batch_size` above 1, lands on one parent that
    change no file in common are landed together (`land_batch`); the land cap then counts batches."""
    mine = set(lane.initiatives) | {str(i["initiative"]) for i in lane.items}
    for line in [*sweep(ports, mine), *queue_alarm(ports, initiatives=mine)]:
        ports.log(line)
    for initiative in lane.initiatives:
        for line in reconcile(ports, initiative):
            ports.log(line)
    outcomes: list[Outcome] = []

    def report(outcome: Outcome) -> Outcome:
        outcomes.append(outcome)
        status = (f"{outcome.item}: {outcome.status}: {outcome.detail}",) if outcome.detail else ()
        # The events say what was done; a land that did not finish also says where it stopped, and why.
        for line in (*outcome.events, *(status if outcome.status != "done" else ())) if outcome.events else status:
            ports.log(line)
        return outcome

    def claimable(kind: str) -> list[Mapping[str, Any]]:
        return [i for i in ls.claimable(ls.list_items(ports.conn), ports.now()) if i["kind"] == kind and str(i["initiative"]) in mine]

    # A phase merge left waiting (its branch has diverged from origin, a parent phase is not merged) did no work, so it
    # does not use the cap, as a waiting land does not: the oldest items would otherwise sit at the head of the queue
    # every pass and no later phase would ever be reached.
    worked = 0
    for item in claimable(ls.PHASE_MERGE):
        if worked >= max(phase_cap, 0):
            break
        if report(perform(ports, item)).status != "waiting":
            worked += 1
    # Read again: a phase merged just now may have assembled its initiative, which then lands in this same pass.
    lands = claimable(ls.INITIATIVE_LAND)
    worked = 0
    for batch in disjoint_batches(lands, _land_paths(ports, lands) if land_batch_size > 1 else {}, max(land_batch_size, 1)):
        if worked >= land_cap:
            break
        results = land_batch(ports, batch) if len(batch) > 1 else [perform(ports, batch[0])]
        for outcome in results:
            report(outcome)
        # A land left waiting (a pull request a person has yet to merge, checks still running, another land ahead
        # of it) did no work, so it does not use the cap: the next one gets its turn. A batch uses the cap once.
        if any(o.status != "waiting" for o in results):
            worked += 1
    return outcomes


def run_once(ports: Ports, *, phase_cap: int = 2, land_cap: int = 1, land_batch_size: int = 1,
             run_lanes: Callable[[Sequence[Lane]], list[Outcome]] | None = None) -> list[Outcome]:
    """One pass: plan it, then run its lanes. `run_lanes` runs them side by side (`cox lander` gives each repository
    a thread of its own); without it they run here, one after another."""
    planned = plan_pass(ports)
    if run_lanes is not None:
        return run_lanes(planned)
    caps = {"phase_cap": phase_cap, "land_cap": land_cap, "land_batch_size": land_batch_size}
    return [outcome for lane in planned for outcome in run_lane(ports, lane, **caps)]
