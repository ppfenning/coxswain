"""The epic driver's triage: amending, holding, moving, combining and dropping tasks, and the attempt cap."""

from __future__ import annotations

import difflib
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Literal, NamedTuple, cast

from core import workstore
from core.jsontypes import is_mapping
from core.landing import DEFAULT_LAND_AT
from harness import rescue_select
from harness.base_check import Expected, expected_merges, gaps
from harness.base_sync import holds as base_holds
from harness.cause_rule import TRANSIENT_AUTH
from harness.checks import HARNESS_FAULT_PREFIX
from harness.epic_batch import INTERRUPTED_KIND, quarantine_task
from harness.epic_branches import ATTEMPT_CAP
from harness.epic_context import Ctx, git_in
from harness.epic_execute import execute_cleared
from harness.epic_records import (
    Execution,
    epic_log,
    fence_reason,
    frontmatter_key_lines,
    mirror_write,
    read_store_rows,
    repair_file_state,
    store_authoritative,
    store_first,
    store_fresh,
)
from harness.epic_types import EpicItem, Quarantine, as_list, as_mapping
from harness.gate import apply_arm_for
from harness.phase_remote import Kind, fetch_branch
from harness.resume import write_atomic
from harness.stale_base import missing_paths, ticket_paths


def _frontmatter_block(body: str) -> str | None:
    """The text between the first pair of `---` fences, or None without one."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[: index + 1])
    return None


def ticket_amend_ramp(old_body: str, new_body: str) -> str:
    """`ramp` for a `ticket_amend` proposal, per docs/design/triage.md §2.

    Purely additive — only appended or repeated lines, frontmatter untouched —
    rides the kind's streak (`"eligible"`); any removed line, or any change to
    the frontmatter block, forces `"gated"` whatever the streak. `difflib`
    decides; no model call.
    """
    if _frontmatter_block(old_body) != _frontmatter_block(new_body):
        return "gated"
    removed = any(line.startswith("- ") for line in difflib.ndiff(old_body.splitlines(), new_body.splitlines()))
    return "gated" if removed else "eligible"


def amended_body(old_body: str, action: str) -> str:
    """`old_body`, a blank line, then `action`, with trailing whitespace trimmed."""
    return f"{old_body}\n\n{action}".rstrip()


def is_append_only(old_body: str, new_body: str) -> bool:
    """True only when `new_body` is `old_body` byte for byte plus non-blank text after it."""
    return new_body.startswith(old_body) and bool(new_body[len(old_body):].strip())


def _read_ticket(item: Mapping[str, object]) -> tuple[bytes, dict[str, object]] | None:
    """The item's work file as raw bytes and parsed item; None when it has no file or the file cannot be read."""
    if not item.get("path"):
        return None
    try:
        return Path(str(item["path"])).read_bytes(), dict(workstore.read_item(str(item["path"])))
    except (OSError, ValueError, workstore.WorkStoreError):
        return None


def _kept_append_only(before: Mapping[str, object], after: Mapping[str, object]) -> bool:
    """True when `after` is `before` with text appended to the body and every other field unchanged."""
    old, new = str(before.get("body") or ""), str(after.get("body") or "")
    return (
        is_append_only(old, new)
        and ticket_amend_ramp(old, new) == "eligible"
        and {**before, "body": ""} == {**after, "body": ""}
    )


def apply_amend_directly(
    ctx: Ctx,
    proposal: Mapping[str, object],
    item: EpicItem,
    *,
    old_body: str,
    new_body: str,
    phase: str,
    quarantined: list[Quarantine],
    by_id: Mapping[str, EpicItem],
) -> tuple[EpicItem | None, str | None]:
    """Apply an append-only `ticket_amend` outside the gate, then judge the file the arm wrote, not the body predicted.

    `(rebuilt item, None)` when the file holds the old body with text appended. `(None, reason)` when the apply failed
    or wrote something else; the file is then restored to the bytes read before. `(None, None)` when the gate decides:
    the ramp is not eligible, the check is false, the cartridge names no model arm, or there is no readable ticket file.
    """
    before = _read_ticket(item)
    arm = apply_arm_for("ticket_amend", ctx.cartridge)
    if (
        proposal.get("ramp") != "eligible"
        or not is_append_only(old_body, new_body)
        or arm in (None, "pr", "shell", "workstore")
        or before is None
        or str(before[1].get("body") or "").strip() != old_body.strip()
    ):
        return None, None
    applied, detail = execute_cleared(
        ctx, proposal, slot="other", subject=str(item["id"]), phase=phase, state=Execution({}, {}, {}, quarantined),
        by_id=by_id,
    )
    after = _read_ticket(item)
    if applied and after is not None and _kept_append_only(before[1], after[1]):
        return {**item, "body": str(after[1].get("body") or "")}, None
    restored = ""
    if after is not None and after[0] != before[0]:
        try:
            Path(str(item["path"])).write_bytes(before[0])
            restored = "; the ticket file was restored"
        except OSError:
            restored = "; the ticket file could not be restored"
    why = detail if not applied else "the ticket file does not hold the old body with text appended"
    return None, f"{why}{restored}"


# ── the chair's triage request, and the effect of each triage class ─────────
#
# The chair flags a quarantined task for triage in its store row (`TRIAGE_FLAG`) and sets it back to ready. The epic
# triages a flagged task before any rebuild, records the verdict on the row (`TRIAGE_RECORDS`) and takes the flag off,
# then applies the class's effect: rebuild on an amended body, drop, combine into a sibling, or hold for a person with
# the mark (`HOLD_REASON`, state blocked). docs/design/triage.md §1 holds the class table.

_HOLD_REASON_MAX = 600
_CLOSED_FOR_COMBINE = frozenset({"done", "dropped", "approved"})


def read_queue_rows(ctx: Ctx) -> dict[str, dict[str, object]] | None:
    """Task id to the initiative's store queue row, `extra` included; {} with no store, None when the read fails.

    None is not "nothing flagged": the caller cannot tell which task the chair asked triage for."""
    if ctx.store is None:
        return {}
    try:
        from harness import store_queue  # not module level: see `mirror_read`

        return {str(r["task_id"]): dict(r) for r in store_queue.read(ctx.store.conn, ctx.initiative_id)}
    except Exception as exc:
        epic_log.warning("queue rows of %s not read, so no flag for triage can be seen: %s: %s", ctx.initiative_id, type(exc).__name__, exc)
        return None


def row_extra(row: Mapping[str, object] | None) -> dict[str, object]:
    extra = (row or {}).get("extra")
    return dict(as_mapping(extra))


def _update_row(ctx: Ctx, task: str, change: Callable[[dict[str, object]], dict[str, object]]) -> str | None:
    """Write `change(row)` onto one task's queue row as the store holds it now; None when written, else why not.

    A stale leader, a missing row, a `change` that raises or a store error writes nothing."""
    if ctx.store is None:
        return "no store"
    stale = fence_reason(ctx)
    if stale is not None:
        return stale
    try:
        from harness import store_queue  # not module level: see `mirror_read`

        row = next((r for r in store_queue.read(ctx.store.conn, ctx.initiative_id) if r["task_id"] == task), None)
        if row is None:
            return f"no store row for {task}"
        store_queue.upsert(ctx.store.conn, {"initiative": ctx.initiative_id, "task_id": task, **change(dict(row))})
    except Exception as exc:
        reason = f"store row of {task} not written: {type(exc).__name__}: {exc}"
        epic_log.warning("%s", reason)
        return reason
    return None


def record_triage(ctx: Ctx, task: str, entry: Mapping[str, object]) -> None:
    """The chair's flag comes off the row and the verdict is kept, so a request is answered once and a repeat is seen."""

    def change(row: dict[str, object]) -> dict[str, object]:
        extra = {k: v for k, v in row_extra(row).items() if k != workstore.TRIAGE_FLAG}
        records = [r for r in as_list(extra.get(workstore.TRIAGE_RECORDS)) if is_mapping(r)]
        return {"extra": {**extra, workstore.TRIAGE_RECORDS: [*records, dict(entry)]}}

    if ctx.store is not None and (why := _update_row(ctx, task, change)) is not None:
        epic_log.warning("triage of %s not recorded on its row: %s", task, why)


def drop_stale_triage_flag(ctx: Ctx, task: str) -> None:
    """The chair's flag comes off a row whose request predates the task's fresh start; nothing else on the row changes."""

    def change(row: dict[str, object]) -> dict[str, object]:
        return {"extra": {k: v for k, v in row_extra(row).items() if k != workstore.TRIAGE_FLAG}}

    if ctx.store is not None and (why := _update_row(ctx, task, change)) is not None:
        epic_log.warning("stale triage request on %s not removed from its row: %s", task, why)


def _move_task(ctx: Ctx, item: EpicItem, state: str) -> str | None:
    """Move one task outside the gate; None when moved, else why not. Never called holding `items_lock`.

    Under work_state store the row moves first, compare-and-set on the state the item holds, then the file's state line;
    under files the file's line and its mirror row. The item, the driver's own copy, follows."""
    stale = fence_reason(ctx)
    if stale is not None:
        return stale
    if store_authoritative(ctx):
        refused = store_first(ctx, item, state)
        if refused is not None:
            return refused
        if item.get("path") and not repair_file_state(item, state):
            epic_log.warning("store moved %s to %s; file state line not written, regenerate-states repairs it", item.get("id"), state)
    else:
        if item.get("path") and not repair_file_state(item, state):
            return f"the state line of {item.get('id')} could not be written"
        mirror_write(ctx, item, state)
    with ctx.items_lock:
        item["state"] = state
    return None


def hold_task(ctx: Ctx, item: EpicItem, reason: str) -> tuple[str, str | None]:
    """Hold a task for a person: `(the mark's text, None)` when held, else `(text, why not)`.

    Under work_state store the row's hold reason is written FIRST, so a blocked row never lacks its mark; then state
    blocked and the file's line. Under files only the state line is written."""
    text = workstore.hold_text(reason)
    text = text if len(text) <= _HOLD_REASON_MAX else text[: _HOLD_REASON_MAX - 1] + "…"
    if store_authoritative(ctx):
        why = _update_row(ctx, str(item["id"]), lambda row: {"extra": {**row_extra(row), workstore.HOLD_REASON: text}})
        if why is not None:
            return text, why
    return text, _move_task(ctx, item, "blocked")


def _rewrite_file_body(path: Path, body: str) -> None:
    """Replace the text after a work file's frontmatter with `body`; the frontmatter bytes are kept."""
    lines = path.read_text(encoding="utf-8").split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None) if lines[0].strip() == "---" else None
    if end is None:
        raise ValueError(f"{path}: no frontmatter block")
    write_atomic(path, "\n".join(lines[: end + 1]) + f"\n\n{body.strip()}\n")


def _rewrite_file_needs(path: Path, needs: Sequence[str]) -> None:
    """Replace the `needs` key of a work file's frontmatter; every other line is kept as it was."""
    lines = path.read_text(encoding="utf-8").split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if lines[0].strip() != "---" or end is None:
        raise ValueError(f"{path}: no frontmatter block")
    front = lines[1:end]
    at = next((i for i, line in enumerate(front) if line.startswith("needs:")), None)
    if at is None:
        front = [*front, *frontmatter_key_lines("needs", needs)]
    else:
        stop = next((i for i in range(at + 1, len(front)) if not front[i].startswith((" ", "-"))), len(front))
        front = [*front[:at], *frontmatter_key_lines("needs", needs), *front[stop:]]
    write_atomic(path, "\n".join(["---", *front, *lines[end:]]))


def _closes_cycle(items: Sequence[Mapping[str, object]]) -> bool:
    """Whether the needs among `items` (a need outside them ignored) contain a cycle."""
    needs = {str(i["id"]): [str(n) for n in as_list(i.get("needs"))] for i in items}
    state: dict[str, int] = {}  # 1 on the path, 2 finished

    def visit(node: str) -> bool:
        state[node] = 1
        for need in needs.get(node, ()):
            if need in needs and (state.get(need) == 1 or (state.get(need) is None and visit(need))):
                return True
        state[node] = 2
        return False

    return any(state.get(node) is None and visit(node) for node in needs)


def combined_needs(items: Sequence[Mapping[str, object]], source: str, into: str) -> dict[str, list[str]] | None:
    """Pure. The needs a combine of `source` into `into` writes: each task that needs `source` needs `into` instead,
    and `into` takes on `source`'s own needs, so the folded scope still waits for what it waited for. In order,
    deduplicated, never naming the task itself or `source`. None when the move would close a cycle among the tasks
    that remain."""
    changed: dict[str, list[str]] = {}
    for item in items:
        task = str(item["id"])
        needs = [str(n) for n in as_list(item.get("needs"))]
        if task == source or source not in needs:
            continue
        moved = dict.fromkeys(into if n == source else n for n in needs)
        changed[task] = [n for n in moved if n != task]
    by_id = {str(i["id"]): i for i in items}
    inherited = [str(n) for n in as_list((by_id.get(source) or {}).get("needs"))]
    own = changed.get(into, [str(n) for n in as_list((by_id.get(into) or {}).get("needs"))])
    grown = [n for n in dict.fromkeys([*own, *inherited]) if n not in (into, source)]
    if grown != own or into in changed:
        changed[into] = grown
    projected = [{**i, "needs": changed.get(str(i["id"]), list(as_list(i.get("needs"))))} for i in items if str(i["id"]) != source]
    return None if _closes_cycle(projected) else changed


def dropped_needs(items: Sequence[Mapping[str, object]], source: str) -> dict[str, list[str]]:
    """Pure. Each task that needs `source`, to its needs without it, in order. The work store refuses a task that is
    not done and needs a dropped one (`validate_dag`); a done or dropped task's needs are no longer read, so it is left
    as it is."""
    return {
        str(item["id"]): [str(n) for n in as_list(item.get("needs")) if str(n) != source]
        for item in items
        if str(item["id"]) != source and item.get("state") not in ("done", "dropped")
        and source in [str(n) for n in as_list(item.get("needs"))]
    }


def _write_needs(ctx: Ctx, item: EpicItem, needs: Sequence[str]) -> str | None:
    """One task's needs: the store row first under work_state store, then the file, then the driver's copy.

    None when written. A store refusal writes nothing; under files a file that cannot be written is a refusal too,
    under store the file is a cache and its failure is logged."""
    task = str(item["id"])
    if store_authoritative(ctx) and (why := _update_row(ctx, task, lambda row: {"needs": list(needs)})) is not None:
        return why
    if item.get("path"):
        try:
            _rewrite_file_needs(Path(str(item["path"])), needs)
        except (OSError, ValueError) as exc:
            if not store_authoritative(ctx):
                return f"the needs of {task} could not be written: {exc}"
            epic_log.warning("needs of %s are in the store; its ticket file was not rewritten: %s", task, exc)
    with ctx.items_lock:
        item["needs"] = list(needs)
    return None


def _move_needs(ctx: Ctx, by_id: Mapping[str, EpicItem], moves: Mapping[str, Sequence[str]]) -> tuple[dict[str, list[str]], str | None]:
    """Write every move in `moves`, all or nothing: `(the needs each moved task had, None)`, or, when one write is
    refused, the ones already written are put back and `({}, why)` comes back."""
    before: dict[str, list[str]] = {}
    for task, needs in moves.items():
        old = [str(n) for n in as_list(by_id[task].get("needs"))]
        why = _write_needs(ctx, by_id[task], needs)
        if why is not None:
            _restore_needs(ctx, by_id, before)
            return {}, f"the needs of {task} were not moved: {why}"
        before[task] = old
    return before, None


def _restore_needs(ctx: Ctx, by_id: Mapping[str, EpicItem], before: Mapping[str, Sequence[str]]) -> None:
    """Put back needs `_move_needs` wrote; a write that fails here is logged, as nothing more can be undone."""
    for task, needs in before.items():
        if (why := _write_needs(ctx, by_id[task], needs)) is not None:
            epic_log.warning("needs of %s not put back to %s: %s", task, list(needs), why)


def drop_task(ctx: Ctx, items: list[EpicItem], item: EpicItem) -> str | None:
    """Drop a task triage found the initiative does not need; None when done, else why not, with nothing changed.

    Every task that needed it stops needing it first, so the board stays a valid DAG; a drop that is then refused
    puts those needs back."""
    task = str(item["id"])
    with ctx.items_lock:
        by_id = {str(i["id"]): i for i in items}
        snapshot = [dict(i) for i in items]
    before, why = _move_needs(ctx, by_id, dropped_needs(snapshot, task))
    if why is not None:
        return why
    if (why := _move_task(ctx, item, "dropped")) is not None:
        _restore_needs(ctx, by_id, before)
        return why
    return None


def _folded_section(item: Mapping[str, object]) -> str:
    """This task's scope as the sibling's body carries it: a heading naming the task, then its own body."""
    task = str(item["id"])
    return f"## Folded in from {task}: {item.get('title') or task}\n\n{str(item.get('body') or '').strip()}".strip()


def _fold_body(ctx: Ctx, target: EpicItem, section: str) -> str | None:
    """Append `section` to the sibling's body: the store row, then the file, then the driver's copy; None when written."""
    into = str(target["id"])
    new_body = f"{str(target.get('body') or '').rstrip()}\n\n{section}".strip()
    if store_authoritative(ctx):
        why = _update_row(ctx, into, lambda row: {"body": f"{str(row.get('body') or '').rstrip()}\n\n{section}".strip()})
        if why is not None:
            return why
    if target.get("path"):
        try:
            _rewrite_file_body(Path(str(target["path"])), new_body)
        except (OSError, ValueError) as exc:
            if not store_authoritative(ctx):
                return f"the body of {into} could not be written: {exc}"
            epic_log.warning("combine: %s's ticket file not rewritten, the store row holds the body: %s", into, exc)
    with ctx.items_lock:
        target["body"] = new_body
    return None


def combine_task(ctx: Ctx, items: list[EpicItem], item: EpicItem, into: str) -> str | None:
    """Fold `item` into its sibling `into`; None when done, else why not, with nothing changed.

    The sibling must be open and in the same phase: phases build apart, so a sibling elsewhere could already be
    building without the scope, and moving needs across phases could stall the phase order. In order, each undone if
    a later step is refused: every task that needed this one needs the sibling instead (and the sibling takes on this
    task's own needs), this task is dropped, then the sibling's body gains this task's title and body."""
    task = str(item["id"])
    with ctx.items_lock:
        by_id = {str(i["id"]): i for i in items}
        target = by_id.get(into)
        snapshot = [dict(i) for i in items]
    if target is None or into == task:
        return f"combine names {into!r}, which is not another task of this initiative"
    if str(target.get("state")) in _CLOSED_FOR_COMBINE:
        return f"combine names {into}, which is {target.get('state')}; scope can only fold into an open task"
    if str(target.get("phase") or "") != str(item.get("phase") or ""):
        return f"combine names {into}, in phase {target.get('phase')}; scope folds only into a sibling in the same phase ({item.get('phase')})"
    moved = combined_needs(snapshot, task, into)
    if moved is None:
        return f"moving the needs on {task} to {into} would close a cycle"
    previous = str(item.get("state") or "ready")
    before, why = _move_needs(ctx, by_id, moved)
    if why is not None:
        return why
    if (why := _move_task(ctx, item, "dropped")) is not None:
        _restore_needs(ctx, by_id, before)
        return why
    if (why := _fold_body(ctx, target, _folded_section(item))) is not None:
        if (undo := _move_task(ctx, item, previous)) is not None:
            epic_log.warning("combine: %s stays dropped although its scope did not fold into %s: %s", task, into, undo)
        _restore_needs(ctx, by_id, before)
        return f"the body of {into} was not written: {why}"
    return None


def amend_in_store(
    ctx: Ctx, item: EpicItem, *, old_body: str, new_body: str
) -> tuple[EpicItem | None, str | None]:
    """Under work_state store, with no model arm for `ticket_amend`, the harness appends the amendment to the row itself.

    `(rebuilt item, None)` when the row held the body triage read and took the new one; `(None, reason)` otherwise.
    The ticket file, a cache of the row, gets the same body; a file it cannot write is logged, not a failure."""
    task = str(item["id"])

    def change(row: dict[str, object]) -> dict[str, object]:
        if str(row.get("body") or "").strip() != old_body.strip():
            raise ValueError("the stored body is not the one triage read")
        return {"body": new_body}

    why = _update_row(ctx, task, change)
    if why is not None:
        return None, why
    if item.get("path"):
        try:
            _rewrite_file_body(Path(str(item["path"])), new_body)
        except (OSError, ValueError) as exc:
            epic_log.warning("amendment of %s is in the store; its ticket file was not rewritten: %s", task, exc)
    return {**item, "body": new_body}, None


def triage_initiative_context(initiative: Mapping[str, object], items: Sequence[Mapping[str, object]], task: str) -> dict[str, object]:
    """What triage reads beside the task: the initiative's goal, its phases with any goal recorded, and every sibling."""
    goal = "\n\n".join(x for x in (str(initiative.get("title") or ""), str(initiative.get("body") or "").strip()) if x)
    recorded = {
        str(p.get("id")): str(p.get("goal") or "") for p in as_list(initiative.get("phases")) if is_mapping(p)
    }
    names = list(dict.fromkeys([
        *(str(p.get("id")) if is_mapping(p) else str(p) for p in as_list(initiative.get("phases"))),
        *(str(i.get("phase")) for i in items),
    ]))
    return {
        "goal": goal,
        "phases": [{"id": p, "goal": recorded.get(p, "")} for p in names],
        "siblings": [
            {"id": str(i["id"]), "phase": str(i.get("phase") or ""), "state": str(i.get("state") or ""),
             "title": str(i.get("title") or ""), "needs": [str(n) for n in as_list(i.get("needs"))]}
            for i in items
            if str(i["id"]) != task
        ],
    }


def newest_attempt_text(attempts: Sequence[Mapping[str, object]]) -> str:
    """One line naming the newest attempt by `ts` (ties keep list order); the reason is cut to 120 characters."""
    newest = sorted(attempts, key=lambda a: str(a.get("ts") or ""))[-1:]
    if not newest:
        return "newest attempt: none recorded"
    a = newest[0]
    reason = " ".join(str(a.get("reason") or "").split())[:120]
    return f"newest attempt: kind {a.get('kind') or 'unrecorded'}, cause {a.get('cause') or 'unrecorded'}, reason {reason}"


def build_attempts(attempts: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """The attempts that count toward the cap: `infra`, `rescue_failed` and `interrupted` are not builds."""
    not_a_build = {"infra", rescue_select.RESCUE_KIND, INTERRUPTED_KIND}
    return [a for a in attempts if a.get("kind") not in not_a_build]


def attempt_cap_reason(attempts: Sequence[Mapping[str, object]]) -> str:
    """The refusal's reason, naming every earlier run so a person has the history."""
    history = "; ".join(f"{a.get('run')}: {a.get('reason')}" for a in attempts)
    return (
        f"attempt cap: {len(attempts)} earlier run(s) quarantined this task — {history}. "
        f"{newest_attempt_text(attempts)}. "
        "Refusing a third run; a person decides."
    )


def triage_failure_hold(failure: str, asked: str) -> tuple[str, str]:
    """Pure. (task id, hold reason) for one failed triage invocation, `<task>: <what failed>` as `invoke_graphs` names it.

    The reason names the failure first, then why triage was asked; a person reads it on the held task."""
    task_id, _, what = failure.partition(":")
    return task_id, f"triage could not run: {what.strip() or 'no detail was recorded'}. It was asked because: {asked}"


def attempt_cap_for(attempts: Sequence[Mapping[str, object]]) -> int:
    """1 when the newest attempt by `ts` (ties keep list order) was caused by the ticket or the code, else `ATTEMPT_CAP`."""
    newest = sorted(attempts, key=lambda a: str(a.get("ts") or ""))[-1:]
    return 1 if any(a.get("cause") in ("ticket", "code") for a in newest) else ATTEMPT_CAP


def _ready_view(items: Sequence[EpicItem]) -> list[EpicItem]:
    """`items`, with a merged-but-unlanded parent presented as `done` for `needs`.

    `ready_tasks` satisfies a `needs` entry only against a literal `done`, and
    nothing in a run ever writes that — `cox runs land` does. Without this, a
    dependent phase's task would never become ready until a human landed the
    parent, even though the parent's code is already sitting on the phase
    branch stack the dependent builds from. Only a task that actually merged
    (`item["merged"]`) is presented this way; an escalated or conflicted
    parent stays `approved`, so its dependent still waits, unchanged. The
    view is read-only and local to this call — `items` itself, and every
    other read anywhere else, still shows `approved`.
    """
    return [
        {**item, "state": "done"} if item.get("state") == "approved" and item.get("merged") else item
        for item in items
    ]


def _merge_on_origin(ctx: Ctx, commit: str, branch: str) -> bool | None:
    """Whether `commit` is on `branch` once it is fetched from origin; None when origin could not be asked.

    The local branch may be stale when another host pushed it. An unknown answer raises one harness fault per run and
    quarantines nothing: the task stays approved and merged, and the next run checks again. A repository whose fetch
    already failed this run is not fetched again, since each fetch can wait out its timeout."""
    with ctx.items_lock:
        if str(ctx.repo) in getattr(ctx, "merge_fetch_faults", ()):
            return None
    outcome = fetch_branch(ctx.repo, branch)
    if outcome.kind == Kind.FAST_FORWARDED:
        return base_holds(ctx.repo, commit, branch)
    if outcome.kind == Kind.FETCHED:
        return base_holds(ctx.repo, commit, f"refs/remotes/origin/{branch}")
    unasked = outcome.kind == Kind.FETCH_FAILED or (
        outcome.kind == Kind.PRESENT_LOCALLY and outcome.detail.startswith("not refreshed from origin")
    )
    if not unasked:
        return False
    with ctx.items_lock:
        first = str(ctx.repo) not in ctx.merge_fetch_faults
        ctx.merge_fetch_faults.add(str(ctx.repo))
    if first:
        epic_log.warning(
            "%s phase branch %s of %s could not be fetched, so its merged commits are not checked this run: %s",
            HARNESS_FAULT_PREFIX, branch, ctx.repo, outcome.detail or "no detail from git",
        )  # fmt: skip
    return None


class _MergeUnchecked(NamedTuple):
    """A merged commit that is not on the local phase branch while origin could not be asked: neither present nor missing."""

    branch: str


def _merge_missing(ctx: Ctx, phase: str, item: EpicItem) -> str | _MergeUnchecked | None:
    """Why an approved, merged task of this phase cannot be resumed from the store, or None when its commit is on the phase branch.

    Only the store carries `merged_commit`, so a task merged under files is never checked. A task whose row is now done,
    landed by another machine, is adopted into `item` and is not missing. `_MergeUnchecked` when origin could not be asked.
    """
    commit = item.get("merged_commit")
    if not (store_authoritative(ctx) and item.get("phase") == phase and item.get("state") == "approved" and commit):
        return None
    branch = ctx.phase_branch(phase)
    if base_holds(ctx.repo, str(commit), branch):
        return None
    on_origin = _merge_on_origin(ctx, str(commit), branch)
    if on_origin is None:
        return _MergeUnchecked(branch)
    if on_origin:
        return None
    (fresh,) = store_fresh([item], read_store_rows(ctx))
    if fresh["state"] == "done":
        with ctx.items_lock:
            item.update(state=fresh["state"], merged=fresh["merged"], merged_commit=fresh["merged_commit"])
        return None
    return f"merged commit {commit} is not on {branch}; {item['id']} was approved and merged by an earlier run and is not rebuilt"


def quarantine_missing_merges(
    ctx: Ctx, phase: str, items: Sequence[EpicItem], quarantined: list[Quarantine]
) -> str | None:
    """Quarantine each merged task whose commit is gone, one by one. No other item, row or file is touched.

    Returns the reason to pause the phase when a merged commit could not be checked because origin could not be asked:
    building on would land the phase without that commit, and quarantining would blame a commit that may be there."""
    with ctx.items_lock:
        by_id = {str(i["id"]): i for i in items}
        snapshot = list(items)
    unchecked: list[str] = []
    branch = ""
    for item in snapshot:
        why = _merge_missing(ctx, phase, item)
        if isinstance(why, _MergeUnchecked):
            unchecked.append(str(item["id"]))
            branch = why.branch
        elif why is not None:
            quarantined.append(
                quarantine_task(
                    ctx, by_id, phase=phase, task=str(item["id"]), reason=why, kind="infra",
                    cause=("harness", "merged commit missing"), approved_is_open=True,
                )
            )  # fmt: skip
            with ctx.items_lock:
                item["state"], item["merged"] = "quarantined", False
    if not unchecked:
        return None
    return (
        f"merged commits of {', '.join(unchecked)} could not be checked: {branch} of {ctx.repo} could not be fetched "
        "from origin, so the phase is not built this run"
    )


def stale_base_missing(ctx: Ctx, phase: str, item: Mapping[str, object]) -> tuple[str, ...]:
    """Ticket files absent from the level worktree's head; empty when its tracked paths cannot be read."""
    ok, listing = git_in("-C", str(ctx.phase_worktree(phase)), "ls-tree", "-r", "--name-only", "HEAD")
    if not ok:
        return ()
    named = ticket_paths([str(s) for s in as_list(item.get("surfaces"))], str(item.get("body") or ""))
    return missing_paths(named, listing.splitlines())


def invoke_failure_kind(failure: str) -> Literal["infra", "no_work"]:
    """`infra` for a transient OAuth refresh collision, which spends no attempt; `no_work` otherwise."""
    lowered = failure.lower()
    return "infra" if any(marker in lowered for marker in TRANSIENT_AUTH) else "no_work"


def buildable(
    items: list[EpicItem],
    phase: str,
    foreign: Mapping[str, str],
    only: frozenset[str] | None = None,
    land_at: str = DEFAULT_LAND_AT,
) -> list[EpicItem]:
    """The phase's ready tasks with met needs, a dropped one excluded. A need on another initiative reads `foreign`.

    With `only`, the ready set is narrowed to those ids.
    """
    # `ready_tasks` returns the items it was given, typed as the loose mappings it reads.
    ready = cast(list[EpicItem], workstore.ready_tasks(_ready_view(items), phase=phase, foreign=foreign, land_at=land_at))
    return [item for item in ready if item.get("state") != "dropped" and (only is None or str(item["id"]) in only)]


def earlier_merged(items: Sequence[Mapping[str, object]], phase: str, in_run: Collection[str]) -> list[Mapping[str, object]]:
    """The phase's approved items outside this run, in item order, with or without a stored handoff."""
    return [
        item
        for item in items
        if item.get("phase") == phase and item.get("state") == "approved" and str(item["id"]) not in in_run
    ]


def close_runner(runner: object) -> Exception | None:
    """Close the shared runner's threads once, after every phase. The failure is returned, for the caller to raise last."""
    close = getattr(runner, "close", None)
    if not callable(close):
        return None
    try:
        close()
    except Exception as exc:
        return exc
    return None


def base_expected(
    ctx: Ctx, phase: str, parents: Sequence[str], items: Sequence[EpicItem], runnable: Sequence[Mapping[str, object]]
) -> tuple[Expected, ...]:
    """Approved, merged tasks the phase base must hold. A task done in the store is landed and is never checked."""
    rows = read_store_rows(ctx)
    with ctx.items_lock:
        fresh = store_fresh(items, rows)
        wanted = expected_merges(fresh, phase, [str(t["id"]) for t in runnable], parents)
    state = {str(i["id"]): i.get("state") for i in fresh}
    return tuple(e for e in wanted if state.get(e.task) != "done")


def base_gaps(worktree: Path, expected: Sequence[Expected]) -> tuple[Expected, ...]:
    return gaps(expected, {e.task: base_holds(worktree, e.commit) for e in expected})
