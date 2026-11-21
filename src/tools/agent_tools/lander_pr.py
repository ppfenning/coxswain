"""The new model's one pull request, from the lander's side: its push and opening at the tip the suite passed at
(`open_at`, `push_and_open`), its title and body (`pr_text`), its draft or ready mark while its head is stale or fresh
(`mark_open_pr`), and the tip the gate judges (`gate_tip`).

Split out of `lander` to keep it under the module limit. It takes `lander_base`'s ports and never imports `lander`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import lander_review
from agent_tools.lander_base import (
    LandMeta,
    Outcome,
    Ports,
    Row,
    fetch_origin,
    ports_repo,
    publish_initiative,
    record_text,
)
from core import landing_record as lr
from core.jsontypes import is_mapping
from core.landing import TemplateError, initiative_pr_reference, initiative_pr_title
from harness import landing_git as lg
from harness import landing_store as ls

__all__ = ["gate_tip", "mark_open_pr", "open_at", "pr_body", "pr_text", "push_and_open"]

def pr_body(meta: LandMeta, record: lr.LandingRecord, tasks: Sequence[Row], reference: str | None) -> str:
    """The one pull request's body: the initiative, a block per phase with its tasks, and the issue line."""
    initiative = meta.get("initiative") or {}
    parents = lr.phase_parents(tasks)
    lines = [str(initiative.get("summary") or initiative.get("title") or record_text(record, "initiative")).strip(), ""]
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


def gate_tip(repo: Path, branch: str) -> str | None:
    """Edge. The initiative branch's tip the new model's gate judges (`lander_review`): origin's copy, as the review
    and the suite read it; this clone's own when there is no origin. None when origin cannot be read."""
    origin_ref, unreadable = fetch_origin(repo, branch)
    if unreadable:
        return None
    return lg.rev(repo, origin_ref) if origin_ref else lg.rev(repo, f"refs/heads/{branch}")


def open_at(ports: Ports, record: lr.LandingRecord, tasks: Sequence[Row], meta: LandMeta, repo: Path,
             refs: tuple[str, str], note: str, approved: str) -> tuple[lr.LandingRecord, list[str]] | Outcome:
    """The new model's push and pull request, under the branch's lease, so no phase merge lands meanwhile: only when
    the branch and origin's copy are both still at `approved`, the tip the suite passed at (`lander_review.
    refresh_step`). No ticket checks run here: the suite at that very tip is the gate. A moved branch is pushed when
    only the parent merge moved it (else this clone goes back to origin's copy, which the next pass fetches), the move
    is recorded for the chair to queue the review and the suite again (`lander_review.moved`), and the land waits."""
    branch, initiative = refs[0], record_text(record, "initiative")
    ident = ls.item_id(ls.INITIATIVE_LAND, initiative)
    local, origin = lg.rev(repo, f"refs/heads/{branch}"), gate_tip(repo, branch)
    moves = int((record.get("review_tip") or {}).get("moves") or 0)
    step = lander_review.refresh_step(approved, local, origin, moves)
    if step.action == "open":
        return push_and_open(ports, record, tasks, meta, repo, refs, note, approved)
    if step.push and (dropped := lg.missing_from(repo, branch, lr.recorded_shas(record))):
        return Outcome("failed", ident, f"refused to push {branch}: it lacks recorded commit(s) {', '.join(s[:12] for s in dropped)}")
    if step.push and (refused := publish_initiative(ports, repo, branch)):
        return Outcome("waiting", ident, refused)
    if not step.push and local and origin and local != origin:
        lg.git(repo, "update-ref", "-m", "lander: origin moved; the next pass fetches it", f"refs/heads/{branch}", origin, local)
    tip = str((local if step.push else origin) or approved)
    return Outcome("waiting", ident, step.reason, tuple(lander_review.moved(ports.conn, record, step, tip, ports.now(), ports.notify)))


def pr_text(meta: LandMeta, record: lr.LandingRecord, tasks: Sequence[Row], parent: str) -> tuple[str, str] | str:
    """The pull request's title and body (without the gate's line), or why the cartridge's templates cannot make them."""
    initiative = record_text(record, "initiative")
    try:
        cartridge: Mapping[str, object] = meta.get("cartridge") or {}
        own: Mapping[str, object] = meta.get("initiative") or {"id": initiative}
        title = initiative_pr_title(cartridge, own, parent, meta.get("epic_issue"))
        return title, pr_body(meta, record, tasks, initiative_pr_reference(cartridge, own, parent, meta.get("epic_issue")))
    except TemplateError as exc:
        return str(exc)


def mark_open_pr(ports: Ports, initiative: str, gate: lander_review.Decision, head: str) -> list[str]:
    """An open new-model pull request made a draft while its head has no passing suite, ready once it has, and its
    body naming the sha the suite last passed at (`lander_review.freshness`, `mark_pr`)."""
    record = ls.get_record(ports.conn, initiative) or {}
    fresh = lander_review.freshness(gate, head, record.get("pr") or {})
    if not fresh.report or not (record.get("pr") or {}).get("url"):
        return []
    text = pr_text(ports.meta(initiative), record, ports.tasks(initiative), record_text(record, "parent"))
    if isinstance(text, str):
        return [f"{initiative}: the pull request's body cannot be made: {text}"]
    return lander_review.mark_pr(ports.conn, ports.forge, ports_repo(ports, record), record, fresh, text, ports.now())


def push_and_open(ports: Ports, record: lr.LandingRecord, tasks: Sequence[Row], meta: LandMeta, repo: Path,
                   refs: tuple[str, str], note: str, passed: str = "") -> tuple[lr.LandingRecord, list[str]] | Outcome:
    """Push the initiative branch (refused when it lacks a recorded sha) and open its pull request, or find it open.
    On the new model `passed` is the tip the suite passed at, which the record keeps (`lander_review.opened`)."""
    (branch, parent), initiative = refs, record_text(record, "initiative")
    ident, recorded = ls.item_id(ls.INITIATIVE_LAND, initiative), lr.recorded_shas(record)
    dropped = lg.missing_from(repo, branch, recorded)
    if dropped:  # guard before any push: nothing the record names may be missing from what goes to origin
        return Outcome("failed", ident, f"refused to push {branch}: it lacks recorded commit(s) {', '.join(s[:12] for s in dropped)}")
    pushed, why = ports.forge.push(repo, branch)
    if not pushed:
        return Outcome("failed", ident, f"push of {branch} failed: {why}")
    text = pr_text(meta, record, tasks, parent)
    if isinstance(text, str):
        return Outcome("failed", ident, text)
    title, base_body = text
    existing = ports.forge.find_open_prs(repo, branch)
    if isinstance(existing, str):
        return Outcome("waiting", ident, f"could not ask the forge about {branch}: {existing}")
    if existing:
        first = existing[0]
        url = str(first.get("url") or first.get("number")) if is_mapping(first) else str(first)
        events = [f"found the open pull request for {branch}: {url}"]
    else:
        ok, url = ports.forge.open_pr(repo, title, base_body + (f"\n{note}\n" if note else ""), head=branch, base=parent)
        if not ok:
            return Outcome("failed", ident, f"pull request for {branch} was not opened: {url}")
        events = [f"opened the pull request for {initiative}: {url}"]
    pr: lr.PullRequest = {"url": url, "title": title, "head": branch, "base": parent}
    if passed:  # the new model: where the suite passed is kept, and its moves are counted afresh
        fresh, review_tip = lander_review.opened(record, passed)
        pr, record = {**pr, **fresh}, {**record, "review_tip": review_tip}
    updated = lr.with_state({**record, "state": "assembled"} if record.get("state") != "pr_open" else record, "pr_open", ports.now(), pr=pr)
    ls.put_record(ports.conn, updated)
    return updated, events
