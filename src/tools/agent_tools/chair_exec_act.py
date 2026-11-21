"""The chair's executor, the other actions: quarantine, hold, rescue, housekeeping, `_execute` and `perform`."""

from __future__ import annotations

import getpass
import json
import os
import re
import signal
import tempfile
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import assert_never

from agent_tools import (
    base_branch,
    chair,
    chair_apply_release,
    chair_apply_restore,
    chair_apply_review,
    chair_housekeeping,
    chair_land_freeze,
    chair_sort,
    courier,
    cox_settings,
    land_manual,
    partial_land,
    queue_export,
    repo_path,
    route,
    route_edit,
    run_store,
    stale_draft,
    store_cli,
)
from agent_tools.chair_carry_exec import perform_carry
from agent_tools.chair_exec_land import (
    LAND_ACTION_KINDS,
    LAUNCH_KINDS,
    UNFENCED_KINDS,
    Blocker,
    Deps,
    Result,
    Run,
    TargetUnreadable,
    argv_for,
    land_stop,
    land_subject,
    make_result,
    over_ssh,
    perform_clear,
    perform_fetch_exit,
    perform_land,
    perform_land_async,
    perform_land_phase,
    perform_launch,
    perform_lease,
    perform_partial_phase,
    perform_widen_ticket,
    phase_action,
    recorded_action,
    tail,
    target_branch_of,
    ticket_paths_of,
)
from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.chair_phase_quarantine import Quarantined, release_commands
from agent_tools.chair_rebase_exec import perform_rebase
from agent_tools.chair_revert_exec import MERGED, RevertResult, perform_revert
from agent_tools.chair_schedule import SCHEDULE_FILE
from agent_tools.chair_stall_alarm import notice_word, stall_message_id
from agent_tools.chair_types import Action, ActionKind, action_text, is_fenced
from agent_tools.land import LAND_CONFLICT_KEY, conflict_note
from agent_tools.pytest_tmp_sweep import sweep_pytest_tmp
from core.jsontypes import is_mapping


def _ticket_file(work_dir: Path, initiative: str, task_id: str) -> Path | None:
    """The one ticket file under `work/<initiative>/**/<task_id>.md`; None when none matches."""
    return next(iter(sorted((work_dir / "work" / initiative).glob(f"**/{task_id}.md"))), None)


def _refused_note(action: Action, deps: Deps, reason: str) -> Result:
    """A refused or failed model step publishes needs_attention and marks its key handled, so no later tick asks again.

    The next tick's sort then finds the key handled and plans the needs_chair report, never another model call."""
    _publish({**action, "epic": action.get("initiative", "")}, deps, "needs_attention", {"action": str(action["kind"]), "reason": reason})
    if deps.mark_handled is not None and action.get("key"):
        deps.mark_handled(action_text(action, "key"))
    return make_result(action, "refused", reason)


def _annotate_objection(action: Action, deps: Deps) -> Result:
    """Edge. One cheap-model rebuild instruction, appended to the ticket body once; the task goes back to ready."""
    initiative, task = action.get("initiative", ""), action.get("task_id", "")
    objection, key = action.get("objection", ""), action.get("key", "")
    path = _ticket_file(deps.work_dir, initiative, task) if initiative and task else None
    if path is None or not objection or not key:
        return _refused_note(action, deps, "annotate_objection needs a ticket, an objection and a key")
    if deps.objection_note is None:
        return _refused_note(action, deps, "no model step is configured for the rebuild instruction")
    try:
        text = deps.objection_note(objection)
    except Exception as exc:
        return _refused_note(action, deps, f"model step failed: {type(exc).__name__}: {exc}")
    if not text.strip():
        return _refused_note(action, deps, "model step refused: empty instruction")
    try:
        body = path.read_text(encoding="utf-8")
        path.write_text(body.rstrip("\n") + "\n\n" + chair_sort.rebuild_note(objection, text) + "\n", encoding="utf-8")
    except OSError as exc:
        return _refused_note(action, deps, f"cannot annotate {task}: {exc}")
    warning = store_cli.mirror_state(deps.runs_dir, initiative, task, "ready", "chair")
    if deps.mark_handled is not None:
        deps.mark_handled(key)
    return make_result(action, "done", f"annotated {task}" if warning is None else f"annotated {task}; {warning}")


def _quarantined_row(action: Action, deps: Deps) -> tuple[Mapping[str, object] | None, Result | None]:
    """The action's store row while it is still quarantined, else the result that ends the action with nothing written."""
    initiative, task = action.get("initiative", ""), action.get("task_id", "")
    if not initiative or not task or not action.get("key"):
        return None, make_result(action, "refused", f"{action.get('kind')} needs an initiative, a task and a key")
    stored = next((r for r in run_store.read_queue(deps.runs_dir, initiative, kind="task") if r.get("task_id") == task), None)
    if stored is None:
        return None, make_result(action, "failed", f"no store row for {task}")
    if stored.get("state") != "quarantined":
        return None, make_result(action, "skipped", f"{task} is {stored.get('state')}, not quarantined")
    return stored, None


def _extra_of(row: Mapping[str, object]) -> dict[str, object]:
    """Pure. The `extra` of a queue row as a fresh dict; empty when it has none. An `extra` that is not a mapping
    raises TypeError: these writes replace the whole extra, so reading it as empty would wipe held, attempts,
    priority and release_target before the state moved."""
    extra: object = row.get("extra") or {}
    if not is_mapping(extra):
        raise TypeError(f"the extra of {row.get('task_id')!r} is a {type(extra).__name__}, not a mapping")
    return dict(extra)


def taken_back(current: Mapping[str, object], before: Mapping[str, object], added: Mapping[str, object], removed: Collection[str]) -> dict[str, object]:
    """Pure. `current` extra with only this action's own change undone: each key it added that still holds its value
    comes out, and each key it removed that nobody has written since comes back. Another writer's keys stay."""
    kept = {k: v for k, v in current.items() if not (k in added and v == added[k])}
    return {**kept, **{k: before[k] for k in removed if k in before and k not in current}}


def _mark_then_move(
    action: Action, deps: Deps, stored: Mapping[str, object], added: Mapping[str, object], removed: Collection[str], state: store_cli.WorkState, done: str
) -> Result:
    """Edge. Mark the quarantined row `stored` first (its extra with `added` set and `removed` taken out), then move it to
    `state` with a compare-and-set on quarantined.

    The key is marked handled only when both writes landed. A refused move re-reads the row and takes back only this
    action's own mark (`taken_back`), so a writer that moved the row meanwhile keeps what it wrote."""
    initiative, task = action_text(action, "initiative"), action_text(action, "task_id")
    key = {"initiative": initiative, "task_id": task}
    before = _extra_of(stored)
    extra = {**{k: v for k, v in before.items() if k not in removed}, **added}
    refused_row = run_store.upsert_row_detail(deps.runs_dir, {**key, "extra": extra})
    if refused_row:
        return make_result(action, "failed", f"the store did not take the mark on {task}: {refused_row}")
    written = store_cli.set_state(deps.runs_dir, initiative, task, state, "chair-loop", "quarantined")
    if isinstance(written, store_cli.StateSet):
        if deps.mark_handled is not None:
            deps.mark_handled(action_text(action, "key"))
        return make_result(action, "done", done)
    now = next((r for r in run_store.read_queue(deps.runs_dir, initiative, kind="task") if r.get("task_id") == task), None)
    if now is None:
        unrestored = "the row could not be read back"
    else:
        unrestored = run_store.upsert_row_detail(
            deps.runs_dir, {**key, "extra": taken_back(_extra_of(now), before, added, removed)}
        )
    note = f"; the store did not take back the mark: {unrestored}" if unrestored else ""
    if isinstance(written, store_cli.StateRefused) and written.current not in (None, "quarantined") and not unrestored:
        return make_result(action, "skipped", f"{task} is {written.current}, not quarantined")
    return make_result(action, "failed", f"set-state {state} for {task}: {written}{note}")


def _request_triage(action: Action, deps: Deps) -> Result:
    """Edge. Flag a quarantined task for triage and send it back to ready, so the next run triages it before any rebuild.

    The flag holds the action's key; the epic driver removes it once the task is triaged. Nothing here asks a model."""
    from core.workstore import TRIAGE_FLAG  # lazy: core sits beside agent_tools in one venv

    stored, ended = _quarantined_row(action, deps)
    if ended is not None or stored is None:
        return ended or make_result(action, "failed", "no store row")
    reason = action.get("reason", "")
    done = f"{action_text(action, 'task_id')} sent to triage" + (f": {reason}" if reason else "")
    return _mark_then_move(action, deps, stored, {TRIAGE_FLAG: action_text(action, "key")}, (), "ready", done)


def _hold_task(action: Action, deps: Deps) -> Result:
    """Edge. Hold a quarantined task for a person: its row takes the `needs you:` reason, then moves to blocked.

    A triage flag left on the row is dropped: a held task is not triaged until a person releases it."""
    from core.workstore import HOLD_REASON, TRIAGE_FLAG, hold_text  # lazy: core sits beside agent_tools in one venv

    stored, ended = _quarantined_row(action, deps)
    if ended is not None or stored is None:
        return ended or make_result(action, "failed", "no store row")
    reason = hold_text(action.get("reason", "") or "the chair held it")
    return _mark_then_move(action, deps, stored, {HOLD_REASON: reason}, (TRIAGE_FLAG,), "blocked", f"{action_text(action, 'task_id')} held: {reason}")


def _announce_hold(action: Action, deps: Deps) -> Result:
    """Edge. One courier entry for a held task, to the chair's inbox; the key is marked so the hold is announced once."""
    initiative, task, reason = action.get("initiative", ""), action.get("task_id", ""), action.get("reason", "")
    if not task or not action.get("key"):
        return make_result(action, "refused", "announce_hold needs a task and a key")
    entry = courier.send(
        courier.Reference("task", task), deps.label or "chair", deps.note_to, f"{task} in {initiative}: {reason}",
        uuid.uuid4().hex, event="needs_attention", fields={"initiative": initiative, "task_id": task, "reason": reason},
        topic=initiative or None,
    )
    try:
        courier.append_file(deps.work_dir / "courier.jsonl", entry)
    except OSError as exc:
        return make_result(action, "failed", f"the courier entry for {task} was not written: {exc}")
    if deps.mark_handled is not None:
        deps.mark_handled(action_text(action, "key"))
    return make_result(action, "done", f"{task} announced: {reason}")


def _hold_initiative(action: Action, deps: Deps) -> Result:
    """Edge. Hold an initiative whose last runs ended the same way (H4.1): its store row first, then its
    `initiative.md`, take `held` and the `needs you:` reason; then one courier entry, and the key is marked handled.

    An initiative already held is left as it is and announced by no one here: a person, or an earlier tick, holds it."""
    from agent_tools import route_hold  # lazy: only this action writes an initiative's hold
    from core.workstore import hold_text  # lazy: core sits beside agent_tools in one venv

    initiative = action.get("initiative", "")
    reason = hold_text(action.get("reason", "") or "the chair held it: its last runs ended the same way")
    row = route_hold.initiative_row_of(deps.initiative_rows(), initiative)
    if row is None:
        return make_result(action, "failed", f"no initiative row for {initiative} in the store")
    if route_hold.is_held_initiative(row):
        return make_result(action, "skipped", f"{initiative} is held already")
    refused = run_store.upsert_row_detail(deps.runs_dir, route_hold.initiative_row_held_as(row, reason))
    if refused:
        return make_result(action, "failed", f"the store did not take the hold on {initiative}: {refused}")
    path = deps.work_dir / "work" / initiative / "initiative.md"
    note = ""
    try:
        if path.is_file():
            path.write_text(route_hold.initiative_held_as(path.read_text(encoding="utf-8"), reason), encoding="utf-8")
            queue_export.record_written_paths(deps.work_dir, [path])
    except OSError as exc:  # the store row holds it; the board export rewrites the file
        note = f" (its file was not written: {exc})"
    entry = courier.send(
        courier.Reference("initiative", initiative), deps.label or "chair", deps.note_to, f"{initiative}: {reason}",
        uuid.uuid4().hex, event="needs_attention", fields={"initiative": initiative, "reason": reason}, topic=initiative or None,
    )
    try:
        courier.append_file(deps.work_dir / "courier.jsonl", entry)
    except OSError as exc:
        return make_result(action, "failed", f"{initiative} held, but its courier entry was not written: {exc}")
    if deps.mark_handled is not None and action.get("key"):
        deps.mark_handled(action_text(action, "key"))
    return make_result(action, "done", f"{initiative} held: {reason}{note}")


def _stall_alarm(action: Action, deps: Deps) -> Result:
    """Edge. One courier entry for a stall the chair raised (`stall_raised`) or saw clear (`stall_cleared`), H7.1.

    The recorded row of this action is what marks the stall raised or cleared: the next tick's facts read it back
    (`chair_stall_alarm.open_alarms`), so a stall is raised once and cleared once. The row is recorded only after this
    entry is written, and the entry's id is this raise's or clear's own (`stall_message_id`, with the action's `seq`), so
    a tick re-run after a write whose row was lost appends the same message again, which the inbox folds into one, and
    a stall raised again after it cleared is a new message. A `quiet` clear writes no entry. A
    failed write records `failed`, and the next tick plans it again."""
    raised = action["kind"] == "stall_alarm"
    stall, subject, since = action.get("stall", ""), action.get("subject", ""), action.get("since", "")
    detail = action.get("detail", "")
    if action.get("quiet"):  # a streak that went quiet or an idle spell that ended: its row closes the stall, unsaid
        return make_result(action, "done", f"expired: {stall}")
    note = f"{notice_word(stall, raised)}: {detail}"
    fields: dict[str, object] = {"stall": stall, "since": since, "subject": subject, "detail": detail}
    held = action.get("held")
    if held:
        fields["held"] = held
    entry = courier.send(
        courier.Reference("initiative", subject or stall), deps.label or "chair", deps.note_to, note,
        stall_message_id(action["kind"], stall, since, action.get("seq", 0)),
        event="stall_raised" if raised else "stall_cleared", fields=fields, topic=subject or None,
    )
    try:
        courier.append_file(deps.work_dir / "courier.jsonl", entry)
    except OSError as exc:
        return make_result(action, "failed", f"the courier entry for {stall} was not written: {exc}")
    return make_result(action, "done", note)


def _rescue(action: Action, deps: Deps) -> Result:
    """The rescue launch; once it is out, its task state's key is handled, so the next tick does not launch it again.

    A rescue that finds the task not eligible writes nothing, so the key is all that tells the next tick it ran."""
    result = perform_launch(action, deps)
    if result["status"] == "done" and deps.mark_handled is not None and action.get("key"):
        deps.mark_handled(action_text(action, "key"))
    return result


def _rebuild_quarantined(action: Action, deps: Deps, blocked: dict[str, Blocker]) -> Result:
    """The relaunch pair, in line: clear_branches carries current main into the kept phases, then the relaunch runs.

    A clear that did not finish, or that raised a conflict, launches nothing and leaves the key unhandled."""
    cleared = perform_clear({**action, "kind": ActionKind.CLEAR_BRANCHES}, deps, blocked)
    if cleared["status"] != "done" or "needs_chair" in cleared:
        return {**cleared, "action": action}
    result = perform_launch({**action, "kind": ActionKind.RELAUNCH}, deps)
    if result["status"] == "done" and deps.mark_handled is not None and action.get("key"):
        deps.mark_handled(action_text(action, "key"))
    return {**result, "action": action}


def _release_approvals(action: Action, deps: Deps) -> Result:
    """In-process, with no argv; each released ticket's `ready` is mirrored to the store, and the action's cause rides in the reason."""
    initiative, task_ids, cause = action.get("initiative", ""), action.get("task_ids", []), action.get("reason", "")
    if not initiative or not task_ids:
        return make_result(action, "refused", "release_approvals needs an initiative and task_ids")
    ok, reason = chair_apply_release.apply_release(deps.work_dir, initiative, task_ids, cause, deps.now()[:10])
    if not ok:
        return make_result(action, "failed", reason)
    warnings = [w for t in dict.fromkeys(task_ids) if (w := store_cli.mirror_state(deps.runs_dir, initiative, t, "ready", "chair")) is not None]
    return make_result(action, "done", "; ".join([f"{reason}: {cause}" if cause else reason, *warnings]))


def _restore_approvals(action: Action, deps: Deps) -> Result:
    """In-process, with no argv; each restored ticket's `approved` is mirrored to the store, and the action's cause rides in the reason."""
    initiative, task_ids, cause = action.get("initiative", ""), action.get("task_ids", []), action.get("reason", "")
    if not initiative or not task_ids:
        return make_result(action, "refused", "restore_approvals needs an initiative and task_ids")
    ok, reason = chair_apply_restore.apply_restore(deps.work_dir, initiative, task_ids, cause, deps.now()[:10])
    if not ok:
        return make_result(action, "failed", reason)
    warnings = [w for t in dict.fromkeys(task_ids) if (w := store_cli.mirror_state(deps.runs_dir, initiative, t, "approved", "chair")) is not None]
    return make_result(action, "done", "; ".join([f"{reason}: {cause}" if cause else reason, *warnings]))


_CLOSE_STATES = ("done", "dropped")


def _close_state_problem(state: object) -> str:
    """"" when `state` is a state an initiative may be closed into, else the reason it is refused."""
    if state in _CLOSE_STATES:
        return ""
    return f"close_initiative state must be done or dropped, not {state!r}"


def _close_initiative(action: Action, deps: Deps) -> Result:
    """An initiative row's task_id is its own id, so the id fills both the initiative and the task slot of `set_state`."""
    state, initiative = action.get("state"), action.get("initiative", "")
    problem = _close_state_problem(state)
    if problem:
        return make_result(action, "failed", problem)
    closing: store_cli.WorkState = "dropped" if state == "dropped" else "done"  # the state is done or dropped, checked above
    written = store_cli.set_state(deps.runs_dir, initiative, initiative, closing, "chair-loop")
    if isinstance(written, store_cli.StateSet):
        return make_result(action, "done", f"closed {initiative} as {state}")
    return make_result(action, "failed", f"set-state {state} for {initiative}: {written}")


def _queue_phases(action: Action, deps: Deps) -> Result:
    """Edge. The tick's `phase_queue` writes for ready phases (`chair_phase_queue.changes`), in one transaction at the
    store's time. Each write names the state it moves from, so a repeat, or a worker's claim in between, changes nothing."""
    from harness import store_api

    written = store_api.apply_phase_changes(
        run_store.store_url_for(deps.runs_dir), action.get("changes", []), "chair-loop", _stamp_now(),
    )
    if isinstance(written, store_api.Failed):
        return make_result(action, "failed", f"phase queue: {written.detail}")
    if isinstance(written, store_api.NotAvailable):
        return make_result(action, "failed", f"phase queue: {written.reason}")
    return make_result(action, "done", f"{written} phase queue row(s) written; {action.get('reason', '')}")


def _stamp_now() -> str:
    """Edge. Only the migration's bookkeeping reads this: every phase queue time is the store's own."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finish_task(action: Action, deps: Deps) -> Result:
    """A `land: manual` task is done once its branch is merged on origin; the check fetches, then reads `origin/` refs only."""
    initiative, task, run = action.get("initiative", ""), action.get("task_id", ""), action.get("run", "")
    repo = repo_path.expand(action.get("repo", ""), os.path.expanduser("~"))
    stamp = deps.runs_dir / run / f"finish-{task}.checked"
    if land_manual.checked_recently(stamp):
        return make_result(action, "skipped", f"{task} was checked less than {land_manual.RECHECK_S}s ago")
    phase = action.get("phase", "")
    branches = land_manual.pr_branches(initiative, phase, task, land_manual.record_branch(deps.runs_dir, run, task))
    try:
        base = base_branch.base_name(repo, target_branch_of(deps, initiative), None, deps.run)
    except TargetUnreadable as exc:
        return make_result(action, "failed", f"resolving the branch {initiative} lands on: {exc}")
    targets = land_manual.merge_targets(base, initiative, phase)
    verdict, why = land_manual.merged_on_origin(repo, branches, targets, deps.run)
    if verdict != "merged":
        land_manual.mark_checked(stamp)
        if verdict == "unverifiable":
            return make_result(action, "failed", f"{why}; {land_manual.HATCH}")
        return make_result(action, "skipped", why)
    written = store_cli.set_state(deps.runs_dir, initiative, task, "done", "chair-loop", "approved")
    if isinstance(written, store_cli.StateSet):
        return make_result(action, "done", f"{task} done: {why}{_record_merged_pr(deps, repo, run, phase, task, branches, initiative)}")
    return make_result(action, "failed", f"set-state done for {task}: {written}")


def _requeue_task(action: Action, deps: Deps) -> Result:
    """Edge. Mark an approved task's store row with `reason` as a rebuild marker, then set it ready; a task no longer approved is skipped."""
    initiative, task = action.get("initiative", ""), action.get("task_id", "")
    why = action.get("reason", "") or "requeued by the chair"
    stored = next((r for r in run_store.read_queue(deps.runs_dir, initiative, kind="task") if r.get("task_id") == task), None)
    if stored is None:
        return make_result(action, "failed", f"no store row for {task}")
    if stored.get("state") != "approved":
        return make_result(action, "skipped", f"{task} is {stored.get('state')}, not approved")
    key = {"initiative": initiative, "task_id": task}
    marked = {"extra": {**_extra_of(stored), LAND_CONFLICT_KEY: why}, "body": route_edit.append_note(stored.get("body") or "", conflict_note(why))}
    refused_row = run_store.upsert_row_detail(deps.runs_dir, {**key, **marked})
    if refused_row:
        return make_result(action, "failed", f"the store did not take the reason of {task}: {refused_row}")
    written = store_cli.set_state(deps.runs_dir, initiative, task, "ready", "chair-loop", "approved")
    if isinstance(written, store_cli.StateSet):
        return make_result(action, "done", f"{task} requeued: {why}")
    # The task is not ours to mark: put the row back as it was.
    unrestored = run_store.upsert_row_detail(deps.runs_dir, {**key, "extra": stored.get("extra") or {}, "body": stored.get("body") or ""})
    note = f"; the store did not put the row back: {unrestored}" if unrestored else ""
    if isinstance(written, store_cli.StateRefused) and written.current not in (None, "approved") and not unrestored:
        return make_result(action, "skipped", f"{task} is {written.current}, not approved")
    return make_result(action, "failed", f"set-state ready for {task}: {written}{note}")


def _record_merged_pr(deps: Deps, repo: str, run: str, phase: str, task: str, branches: list[str], initiative: str) -> str:
    """Edge. Mirror the merged PR onto the finished task's record; the text to add to the reason.
    The task is already done, so a PR that cannot be found or recorded changes the reason only, never the status.
    A miss names the `route edit --pr` command that records it later, since the finish is not retried."""
    pr = land_manual.lookup_merged_pr(repo, branches, deps.run)
    if pr is None:
        return f"; no merged PR found by gh: record one with `cox route edit {task} --initiative {initiative} --state done --pr <url>`"
    warning = land_manual.record_landed(deps.runs_dir, run, phase, task, pr, deps.now())
    return f"; {warning}" if warning else f"; recorded {pr}"


def _prune_available(run: Run, harness_python: str) -> bool:
    """Edge. True only when `<harness_python> -m harness.store_backfill_traces prune --help` exits 0."""
    code, _ = run([harness_python, "-m", "harness.store_backfill_traces", "prune", "--help"])
    return code == 0


def _housekeeping(action: Action, deps: Deps) -> Result:
    """Lake sync, trace prune, runs clean, in order; the reason names all three, none stopping the others."""
    traces_root = run_store.traces_root_for(deps.runs_dir).url
    status, reason = chair_housekeeping.run_housekeeping(
        deps.run, traces_root, partial(_prune_available, deps.run, deps.harness_python),
        deps.harness_python, deps.log_retention_days,
    )
    return make_result(action, status, reason)


def _send_note(deps: Deps, ref: courier.Reference, text: str) -> None:
    """Edge. One courier line for `ref` appended to `work_dir/courier.jsonl`, addressed to `deps.note_to`."""
    session = (chair.read(deps.runs_dir) or {}).get("session")
    sender = str(session) if session else "chair"
    entry = courier.send(ref, sender, deps.note_to, text, uuid.uuid4().hex)
    courier.append_file(deps.work_dir / "courier.jsonl", entry)


def _send_stale_draft_note(deps: Deps, initiative: str, reason: str) -> None:
    """Edge. One `coxswain://initiative/<id>` courier line to `deps.note_to`, quoting `reason` verbatim."""
    _send_note(deps, courier.Reference("initiative", initiative), reason)


def lane_writer(profile_path: Path) -> Callable[[str, int], int]:
    """The `Deps.set_lanes` that writes through `cox settings set host <host>.capacity <n>`, the one lane write path."""
    return lambda host, n: cox_settings.run_set(profile_path, "host", f"{host}.capacity", str(n), False)


def _tune_lanes(action: Action, deps: Deps) -> Result:
    """Edge. Sets the host's lane count through `deps.set_lanes`. The bounds are `min_lanes` and `max_lanes` in
    the action's evidence; a missing bound or a count outside it is refused with nothing written."""
    host = action.get("host", "")
    to_lanes = action.get("to_lanes")
    evidence = action.get("evidence", {})
    low, high = evidence.get("min_lanes"), evidence.get("max_lanes")
    if not host or not isinstance(to_lanes, int) or isinstance(to_lanes, bool) or deps.set_lanes is None:
        return make_result(action, "refused", "tune_lanes needs a host, an integer to_lanes and a wired lane writer")
    if not isinstance(low, int) or not isinstance(high, int):
        return make_result(action, "refused", "tune_lanes needs min_lanes and max_lanes in its evidence")
    if not low <= to_lanes <= high:
        return make_result(action, "refused", f"{host}: {to_lanes} lanes is outside {low} to {high}")
    if deps.set_lanes(host, to_lanes) != 0:
        return make_result(action, "failed", f"{host}: setting {to_lanes} lanes failed")
    return make_result(action, "done", f"{host}: lanes {action.get('from_lanes', '?')} to {to_lanes}: {action.get('reason', '')}")


def _propose_tiers(action: Action, deps: Deps) -> Result:
    """Edge. One courier line carrying the action's rendered body. Propose-only: no settings writer is called."""
    body = action.get("body", "")
    if not body:
        return make_result(action, "refused", "propose_tiers needs a body")
    _send_note(deps, courier.Reference("proposal", "tiers"), body)
    return make_result(action, "done", "proposed tiers to the inbox")


def export_restores(planned: Mapping[str, str], rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Pure. The paths of `planned` (workspace-relative path to text) the board export would write back over, sorted:
    each one a store row renders to other text. A path no row renders is the workspace's own and stays written."""
    rendered = queue_export.export_rows([dict(r) for r in rows])
    return sorted(path for path, text in planned.items() if path in rendered and rendered[path] != text)


def _stale_to_draft(action: Action, deps: Deps) -> Result:
    """Edge. Reads the initiative and its tickets, asks `stale_draft.plan_stale_draft` to turn the stale ones into
    a draft, and on success writes the rewritten files and tells `deps.note_to` by courier.

    A Refusal (an initiative already a draft, no stale task, an unknown ticket) writes nothing and sends no
    note; it is reported as an ordinary refusal, the same as any other action's precondition failure. So is a plan
    the board export would undo: the store carries no draft flag, so under a store every rewritten file would be
    restored on the next export, saved as a hand edit, and drafted again on the next tick.
    """
    initiative = action.get("initiative", "")
    initiative_path = deps.work_dir / "work" / initiative / "initiative.md"
    try:
        initiative_text = initiative_path.read_text(encoding="utf-8")
    except OSError:
        return make_result(action, "refused", f"no initiative.md at {initiative_path}")
    ticket_paths = ticket_paths_of(deps.work_dir, initiative)
    ticket_texts: dict[str, str] = {}
    for task_id, path in ticket_paths.items():
        try:
            ticket_texts[task_id] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    stale_tasks = action.get("stale_tasks", [])
    reason = action.get("reason", "")
    plan = stale_draft.plan_stale_draft(initiative_text, ticket_texts, stale_tasks, reason, action.get("since", ""), deps.now())
    if isinstance(plan, stale_draft.Refusal):
        return make_result(action, "refused", plan.reason)
    planned = {
        f"work/{initiative}/initiative.md": plan.initiative_text,
        **{str(ticket_paths[t].relative_to(deps.work_dir)): plan.tickets[t] for t in stale_tasks},
    }
    rows = [r for r in deps.initiative_rows() if r.get("initiative") == initiative]
    if restored := export_restores(planned, rows):
        return make_result(action, "refused", f"{stale_draft.EXPORT_RESTORES} {', '.join(restored)}")
    initiative_path.write_text(plan.initiative_text, encoding="utf-8")
    for task_id in stale_tasks:
        ticket_paths[task_id].write_text(plan.tickets[task_id], encoding="utf-8")
    _send_stale_draft_note(deps, initiative, reason)
    return make_result(action, "done", f"drafted {initiative}: stale tasks {stale_tasks}")


def _check_login(action: Action, deps: Deps) -> Result:
    """Runs the injected check_login edge against the action's host; the row it prints becomes the reason.
    Never touches the attempts table, so its outcome is "recorded", the same status standby and mark_lost
    carry, unless the store refused the write: then it is "failed", the store's answer as the reason."""
    host = action.get("host", "")
    if not host or deps.check_login is None:
        return make_result(action, "refused", "check_login needs a host and a wired check_login edge")
    row = deps.check_login(host)
    return make_result(action, "failed" if store_cli.STORE_ERROR in row else "recorded", json.dumps(row, sort_keys=True))


def _probe_host(action: Action, deps: Deps) -> Result:
    """Runs the injected probe_host edge against the action's host; the row it returns becomes the reason.
    Its outcome is "recorded", or "failed" when the store refused a write, and it sends no notice: the planner's
    host_back action does."""
    host = action.get("host", "")
    if not host or deps.probe_host is None:
        return make_result(action, "refused", "probe_host needs a host and a wired probe_host edge")
    row = deps.probe_host(host)
    return make_result(action, "failed" if store_cli.STORE_ERROR in row else "recorded", json.dumps(row, sort_keys=True))


def _stalled(action: Action, deps: Deps, sig: int) -> Result:
    """Edge. One `sig` to the pid in `<run>.pid`; a missing pidfile or a failed send is a failed result, never a raise."""
    pidfile = Path(deps.runs_dir) / f'{action.get("run", "")}.pid'
    try:
        text = pidfile.read_text(encoding="utf-8")
    except OSError:
        return make_result(action, "failed", "no pidfile")
    pid = route.parse_pid(text)
    if pid is None:
        return make_result(action, "failed", "no pidfile")
    try:
        deps.send_signal(pid, sig)
    except OSError as exc:
        return make_result(action, "failed", f"signal {pid}: {exc}")
    return make_result(action, "done")


def _review_landed(action: Action, deps: Deps) -> Result:
    """Edge. `chair_apply_review.apply_review` over this tick's store, git and ssh doors."""

    def delete_branches(ssh: str, repo: str, pattern: str) -> tuple[list[str], str]:
        return (over_ssh(deps, ssh) if ssh else deps).delete_branches(repo, pattern)

    outcome = chair_apply_review.apply_review(
        action,
        set_state=partial(store_cli.set_state, deps.runs_dir),
        mark_landed=partial(store_cli.mark_landed, deps.runs_dir),
        delete_branches=delete_branches,
        ssh_for=deps.ssh_for,
        now=deps.now,
    )
    if outcome.status != "needs_chair":
        return make_result(action, outcome.status, outcome.reason)
    raised: Action = {
        "kind": ActionKind.NEEDS_CHAIR, "initiative": action.get("initiative", ""), "phase": action.get("phase", ""),
        "task_id": action.get("task_id", ""), "url": action.get("url", ""), "cause": outcome.cause,
        "epoch": action.get("epoch", 0), "reason": outcome.reason,
    }
    return {**make_result(action, "refused", outcome.reason), "needs_chair": raised}


def _carry_phase(action: Action, deps: Deps) -> Result:
    """Edge. `perform_carry` over the wired ports. A stop comes back refused and carrying its needs_chair, the shape
    `_review_landed` returns; a port failure is a failed result, never a raise out of perform."""
    if deps.carry_ports is None:
        return make_result(action, "refused", "carry_phase needs wired carry ports")
    try:
        git, forge, store = deps.carry_ports(action)
    except TargetUnreadable as exc:
        return make_result(action, "failed", f"carry of {action.get('phase', '')} refused, its target branch is unreadable: {exc}")
    try:
        result = perform_carry(action, git, forge, store)
    except Exception as exc:  # the edge: a port failure becomes a failed result
        return make_result(action, "failed", f"carry of {action.get('phase', '')} failed: {exc}")
    finally:
        close = getattr(git, "close", None)  # the real git edge's worktree; a fake has none
        if close is not None:
            close()
    stop = result["action"]
    if result["status"] == "escalated" and stop.get("kind") == "needs_chair":
        return {**make_result(action, "refused", result["reason"]), "needs_chair": stop}
    return {**result, "action": action}


def _rebase_phase(action: Action, deps: Deps) -> Result:
    """Edge. `perform_rebase` over the wired port; its own refusal already carries the needs_chair."""
    if deps.rebase_port is None:
        return make_result(action, "refused", "rebase_phase needs a wired rebase port")
    return perform_rebase(action, deps.rebase_port(action))


def revert_outcome(action: Action, reverted: RevertResult) -> tuple[str, str]:
    """The needs_chair (cause, reason) for a revert: main_red once merged, else revert_failed, which says main is still red."""
    if reverted.status == MERGED:
        return "main_red", f"main went red after PR #{action.get('pr', 0)}; reverted {action.get('commit', '')} in {reverted.pr}"
    return "revert_failed", f"{reverted.detail}; main is still red after the revert of {action.get('commit', '')}"


def revert_reason(reason: str, quarantined: Quarantined, initiative: str) -> str:
    """Pure. `reason`, then how a person releases each task the quarantine moved (they stay quarantined: no relaunch
    rebuilds them), then each quarantine write the store did not take, so the needs_chair names a phase left runnable."""
    release = release_commands(initiative, quarantined.moved)
    released = [f"quarantined {', '.join(quarantined.moved)}; once main is green, release each: {'; '.join(release)}"] if release else []
    return "; ".join([reason, *released, *(f"quarantine not recorded: {line}" for line in quarantined.failures)])


def _revert_land(action: Action, deps: Deps) -> Result:
    """Edge. `perform_revert` over the wired ports, then quarantine the phase whatever the status, so no relaunch stacks onto
    a red main. The land is resolved as reverted only when the revert merged; a failed one is planned again next tick."""
    if deps.revert_ports is None or deps.quarantine_phase is None or deps.resolve_land is None:
        return make_result(action, "refused", "revert_land needs wired revert ports, quarantine and resolve writers")
    initiative, phase = action.get("initiative", ""), action.get("phase", "")
    git, forge = deps.revert_ports(action)
    try:
        reverted = perform_revert(
            action.get("repo", ""), int(action.get("pr", 0)), action.get("commit", ""), action.get("reason", ""), git, forge
        )  # a revert_land's pr is the number of the land's pull request
    except Exception as exc:  # the edge: a port failure is a failed revert, and main is still red
        reverted = RevertResult("failed", "", f"revert raised {type(exc).__name__}: {exc}")
    quarantined = deps.quarantine_phase(initiative, phase, "main_red", action.get("reason", "")) or Quarantined()
    if reverted.status == MERGED:
        deps.resolve_land(initiative, phase, "reverted")
    cause, outcome = revert_outcome(action, reverted)
    reason = revert_reason(outcome, quarantined, initiative)
    raised: Action = {
        "kind": ActionKind.NEEDS_CHAIR, "initiative": initiative, "phase": phase, "cause": cause,
        "epoch": action.get("epoch", 0), "reason": reason,
    }
    return {**make_result(action, "done" if reverted.status == MERGED else "failed", reason), "needs_chair": raised}


MAIN_INFRA_CAUSE = "main_infra"


def _rerun_main(action: Action, deps: Deps) -> Result:
    """Edge. Rerun an infra main's failed jobs through the wired port; never revert, quarantine or resolve a land.

    "rerun" and "pending" are done: the next tick reads main CI again. "capped" is failed with a needs_chair naming the
    commit, since GitHub has already rerun it as often as the port allows. A raise is a failed result.
    """
    if deps.rerun_main is None:
        return make_result(action, "refused", "rerun_main needs a wired rerun port")
    repo, commit = action.get("repo", ""), action.get("commit", "")
    try:
        state, line = deps.rerun_main(repo, commit)
    except Exception as exc:  # the edge: a port failure is a failed rerun, planned again next tick
        return make_result(action, "failed", f"rerun raised {type(exc).__name__}: {exc}")
    if state in ("rerun", "pending"):
        return make_result(action, "done", line)
    reason = f"main CI for {commit} never got past setup and was not rerun again: {line}"
    raised: Action = {
        "kind": ActionKind.NEEDS_CHAIR, "initiative": action.get("initiative", ""), "phase": action.get("phase", ""),
        "cause": MAIN_INFRA_CAUSE, "epoch": action.get("epoch", 0), "reason": reason,
    }
    return {**make_result(action, "failed", reason), "needs_chair": raised}


def _steer_reason(action: Action) -> str:
    """`steer clear of <other>: <paths joined by comma>`, cut to 200 characters."""
    return f"steer clear of {action.get('other', '')}: {','.join(action.get('paths', []))}"[:200]


def schedule_document(action: Action) -> str:
    """The text of `SCHEDULE_FILE`: the action's schedule object alone, as JSON."""
    if "schedule" not in action:
        raise KeyError("schedule")
    return json.dumps(action["schedule"], indent=2, sort_keys=True) + "\n"


def _write_replacing(path: Path, text: str) -> None:
    """Edge. `text` lands in `path` in one step: written beside it, then renamed over it, so a reader never sees half a file.

    mkstemp creates 0600, which the rename would carry onto `path`; the document is published for other readers, so it is 0644.
    """
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(name, 0o644)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _schedule_updated(action: Action, deps: Deps) -> Result:
    if not isinstance(action.get("schedule"), Mapping):
        return make_result(action, "refused", "schedule_updated carries no schedule")
    try:
        _write_replacing(deps.runs_dir / SCHEDULE_FILE, schedule_document(action))
    except OSError as err:
        return make_result(action, "failed", f"{SCHEDULE_FILE}: {err}")
    return make_result(action, "done", f"wrote {SCHEDULE_FILE}")


def _deferred_reason(action: Action) -> str:
    """`deferred to the last phase (land_at <land_at>)`, naming the land_at the action carries."""
    return f"deferred to the last phase (land_at {action.get('land_at', '?')})"


def _publish(action: Action, deps: Deps, event: str, fields: Mapping[str, str], detail: str = "") -> None:
    """Edge. One typed courier line on the action's topic, appended to `work_dir/courier.jsonl` and never read back.

    It is addressed to `deps.note_to`, the chair label; `courier.needs_model` marks the events a model must see.
    A `detail` (a failure's reason, cut to 300 characters) rides in the note; `fields` stay the action's own.
    """
    epic = action.get("epic", "")
    note = f"{event}: {epic}" + (f" ({tail(detail.strip(), 300)})" if detail.strip() else "")
    entry = courier.send(
        courier.Reference("initiative", epic), deps.label or "chair", deps.note_to, note, uuid.uuid4().hex,
        event=event, fields=dict(fields), topic=action.get("topic"),
    )
    courier.append_file(deps.work_dir / "courier.jsonl", entry)


def _would(action: Action) -> str:
    """What a dry run would have done for a schedule update; empty for every other kind."""
    if action.get("kind") == "schedule_updated":
        return f"would write {SCHEDULE_FILE}"
    return ""


# The kinds whose result may carry a needs_chair that is recorded as its own row after the result.
_CARRIES_NEEDS_CHAIR = ("clear_branches", "rebuild_quarantined", "review_landed", "carry_phase", "rebase_phase", "revert_land", "rerun_main")


def _execute(action: Action, deps: Deps, blocked: dict[str, Blocker]) -> Result:
    """Run one action by its kind. The match names every ActionKind, and pyright checks it is exhaustive (H2)."""
    raw = action.get("kind")
    try:
        kind = ActionKind(raw)
    except ValueError:
        return make_result(action, "refused", f"unsupported action kind {raw!r}")
    if (
        kind in LAND_ACTION_KINDS and deps.lands is not None and action.get("repo", "") not in blocked
        and (argv_for(action) is not None or (kind == "partial_land" and bool(partial_land.steps(action))))
    ):
        return perform_land_async(action, deps, deps.lands)
    match kind:
        case ActionKind.LAND:
            return perform_land(action, deps, blocked)
        case ActionKind.LAND_PHASE:
            return perform_land_phase(action, deps, blocked)
        case ActionKind.PARTIAL_LAND:
            return perform_partial_phase(action, deps, blocked)
        case ActionKind.CLEAR_BRANCHES:
            return perform_clear(action, deps, blocked)
        case ActionKind.REVIEW_LANDED:
            return _review_landed(action, deps)
        case ActionKind.CARRY_PHASE:
            return _carry_phase(action, deps)
        case ActionKind.REBASE_PHASE:
            return _rebase_phase(action, deps)
        case ActionKind.REVERT_LAND:
            return _revert_land(action, deps)
        case ActionKind.RERUN_MAIN:
            return _rerun_main(action, deps)
        case ActionKind.TAKE_LEASE:
            return perform_lease(action, deps)
        case ActionKind.CHECK_LOGIN:
            return _check_login(action, deps)
        case ActionKind.PROBE_HOST:
            return _probe_host(action, deps)
        case ActionKind.STANDBY | ActionKind.MARK_LOST:
            return make_result(action, "recorded")
        case ActionKind.NEEDS_CHAIR:
            return make_result(action, "recorded", action.get("reason", ""))  # `recorded_action` overwrites reason, so carry it in the result
        case ActionKind.STEER_CLEAR:
            return make_result(action, "recorded", _steer_reason(action))
        case ActionKind.WAITING:
            phase = action_text(action, "phase")
            if "quarantined" not in action:
                raise KeyError("quarantined")
            if "awaiting_land" not in action:
                raise KeyError("awaiting_land")
            return make_result(
                action,
                "recorded",
                f"blocked by {phase}: {action['quarantined']} quarantined, {action['awaiting_land']} awaiting land",
            )
        case ActionKind.DEFERRED:
            return make_result(action, "recorded", _deferred_reason(action))
        case ActionKind.UNLAUNCHED:  # "skipped", not "recorded": CHANGED_STATUSES must not count it
            return make_result(action, "skipped", action.get("reason", ""))
        case ActionKind.FETCH_EXIT:
            return perform_fetch_exit(action, deps)
        case ActionKind.WIDEN_TICKET:
            return perform_widen_ticket(action, deps)
        case ActionKind.ANNOTATE_OBJECTION:
            return _annotate_objection(action, deps)
        case ActionKind.REBUILD_QUARANTINED:
            return _rebuild_quarantined(action, deps, blocked)
        case ActionKind.REQUEST_TRIAGE:
            return _request_triage(action, deps)
        case ActionKind.HOLD_TASK:
            return _hold_task(action, deps)
        case ActionKind.ANNOUNCE_HOLD:
            return _announce_hold(action, deps)
        case ActionKind.HOLD_INITIATIVE:
            return _hold_initiative(action, deps)
        case ActionKind.STALL_ALARM | ActionKind.STALL_CLEAR:
            return _stall_alarm(action, deps)
        case ActionKind.RELEASE_APPROVALS:
            return _release_approvals(action, deps)
        case ActionKind.RESTORE_APPROVALS:
            return _restore_approvals(action, deps)
        case ActionKind.CLOSE_INITIATIVE:
            return _close_initiative(action, deps)
        case ActionKind.FINISH_TASK:
            return _finish_task(action, deps)
        case ActionKind.QUEUE_PHASES:
            return _queue_phases(action, deps)
        case ActionKind.REQUEUE_TASK:
            return _requeue_task(action, deps)
        case ActionKind.RESCUE:
            return _rescue(action, deps)
        case (
            ActionKind.RELAUNCH | ActionKind.RETRY | ActionKind.LAUNCH_EPIC | ActionKind.LAUNCH_DECOMPOSE
            | ActionKind.PULL | ActionKind.FETCH
        ):
            return perform_launch(action, deps)
        case ActionKind.HOUSEKEEPING:
            return _housekeeping(action, deps)
        case ActionKind.STALE_TO_DRAFT:
            return _stale_to_draft(action, deps)
        case ActionKind.TUNE_LANES:
            return _tune_lanes(action, deps)
        case ActionKind.PROPOSE_TIERS:
            return _propose_tiers(action, deps)
        case ActionKind.STALLED_USR1:
            return _stalled(action, deps, signal.SIGUSR1)
        case ActionKind.STALLED_STOP:
            return _stalled(action, deps, signal.SIGTERM)
        case ActionKind.SCHEDULE_UPDATED:
            return _schedule_updated(action, deps)
        case ActionKind.LAND_BATCH | ActionKind.PULL_WORKSPACE | ActionKind.CLOSE_MERGED_TASK | ActionKind.PYTEST_TMP_SWEEP:
            # declared, but no executor runs them as planned actions (the sweep is only recorded): refused, as before
            # the match was exhaustive
            return make_result(action, "refused", f"unsupported action kind {kind.value!r}")
        case _:
            assert_never(kind)


def notices_to_ack(tasks: list[dict[str, object]], notices: list[dict[str, object]]) -> list[str]:
    """Ids of the open land notices whose task is done, in first-seen order.

    A land notice is an unacked `coxswain://task/<id>` entry whose note mentions landing. Acked entries and
    unknown tasks yield nothing, so acking twice or acking an absent notice is a no-op.
    """
    done = {t["id"] for t in tasks if t.get("state") == "done"}
    prefix = "coxswain://task/"
    return [
        str(n["id"])
        for n in notices
        if not n.get("ack") and str(n.get("ref", "")).startswith(prefix)
        and str(n["ref"])[len(prefix):] in done and "land" in str(n.get("note", "")).lower()
    ]


def current_user() -> str:
    """Edge. The login name pytest names its temp directory after; "" when the system has none."""
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return ""


def _sweep_pytest_tmp(deps: Deps) -> list[str]:
    """Edge. Names of the stale `pytest-of-<user>` run directories removed at the tick's clock.
    The module's age band is the only test of life. A failure removes nothing here and stops nothing else."""
    if deps.temp_root is None:
        return []
    try:
        return sweep_pytest_tmp(deps.temp_root, deps.user, _as_utc(deps.now()).timestamp())
    except Exception:  # a sweep of scratch space must never fail the tick
        return []


def _ack_done_land_notices(deps: Deps) -> list[str]:
    """Edge. Acks, through `courier.ack_file`, every open land notice in `work_dir/courier.jsonl` whose task is
    done under `work_dir/work/*/*/*.md`; returns the ids acked. A missing or unreadable file acks nothing."""
    path = deps.work_dir / "courier.jsonl"
    try:
        notices = courier.entries(courier.read_text(path))
    except OSError:
        return []
    if not any(not n.get("ack") for n in notices):
        return []
    tasks: list[dict[str, object]] = []
    for task_path in sorted((deps.work_dir / "work").glob("*/*/*.md")):
        try:
            fields, _ = route.parse_frontmatter(task_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        tasks.append({"id": fields.get("id", task_path.stem), "state": fields.get("state", "todo")})
    acked: list[str] = []
    for message_id in notices_to_ack(tasks, notices):
        try:
            if courier.ack_file(path, message_id):
                acked.append(message_id)
        except OSError:
            continue
    return acked


def _note_uncounted(
    action: Action, result: Result, blocked: dict[str, Blocker], blocked_initiatives: dict[str, Blocker]
) -> None:
    """A land_phase or partial_land that names an initiative blocks only that initiative; any other land blocks its whole repo."""
    initiative = action.get("initiative", "")
    stop = land_stop(result.get("reason", ""))
    blocker = Blocker(land_subject(action), initiative, action.get("phase", ""), stop[0] if stop else "")
    if action.get("kind") in ("land_phase", "partial_land") and initiative:
        blocked_initiatives[initiative] = blocker
    else:
        blocked[action.get("repo", "")] = blocker


def _resumes_at_wait_checks(action: Action, blocker: Blocker) -> bool:
    """A land_phase for the blocker's own initiative and phase, after a stop at wait_checks, resumes on its open PR."""
    return (
        action.get("kind") == "land_phase"
        and blocker.step == "wait_checks"
        and bool(blocker.phase)
        and action.get("phase", "") == blocker.phase
        and action.get("initiative", "") == blocker.initiative
    )


def freeze_skip_reason(freeze: chair_land_freeze.LandFreeze | None, now: datetime) -> str | None:
    """The skip reason a land gets under a freeze still live at `now`, else None."""
    if freeze is None or freeze.expires_at <= now:
        return None
    return chair_land_freeze.freeze_text(freeze)


def _as_utc(stamp: str) -> datetime:
    """An ISO stamp as an aware datetime; a stamp with no offset is UTC."""
    parsed = datetime.fromisoformat(stamp)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _frozen_reason(deps: Deps) -> str | None:
    """Edge. Reads the land freeze at the tick's clock; the skip reason when it is live, else None."""
    now = _as_utc(deps.now())
    return freeze_skip_reason(chair_land_freeze.read_freeze(deps.runs_dir, now), now)


def perform(
    actions: list[Action], deps: Deps, current_epoch: Callable[[], int], dry_run: bool, sleep: Callable[[float], None] = time.sleep
) -> list[Result]:
    """Edge. One result per action, in order; each is recorded after it runs.

    A launch on a host that already launched earlier this tick first calls `sleep(deps.launch_stagger_s)`, unless that is 0.
    The wait is the full stagger, not the time left since the previous launch started. Launches with no host share one
    key, the loop's own machine, because the plan names the local host as "". Order never changes.

    The epoch is re-read per action. standby and take_lease are never fenced. A dry run touches and records nothing.
    A relaunch whose initiative had a clear_branches end other than done earlier this tick is skipped.
    A land or land_phase whose command exits nonzero is refused, escalated with the cause `land_refusal` read
    from its output, and blocks its repo's later lands and land_phases this tick, except that a land_phase naming
    an initiative blocks only that initiative's later lands, clear_branches and relaunches. A land refused before
    its command runs blocks nothing. A launch_epic for an initiative whose land_phase was not counted is skipped too.
    A land_phase for the same initiative and phase as an earlier land that stopped at wait_checks is not skipped:
    it resumes from the open PR.
    Each land's start re-reads the land freeze at `deps.now()`; a live one skips the land, without an escalation, for
    a later tick to plan again. A land already running is never stopped.
    With a land sink, a land returns `in_progress` at once and the actions after it run in the same tick. The lands
    that finished since the last tick are collected first, so their results lead this tick's. A collected result
    comes from a land process read back from disk, not from a thread of this tick.
    """
    results: list[Result] = []
    blocked: dict[str, Blocker] = {}  # repo -> the uncounted land or land_phase that blocks its later lands and deletes
    blocked_initiatives: dict[str, Blocker] = {}  # initiative -> its uncounted land_phase that skips its later lands
    uncleared: dict[str, str] = {}  # initiative -> status of its clear_branches that did not finish done, this tick
    launched_hosts: set[str] = set()  # hosts a launch was attempted on this tick; "" is the loop's own machine
    if not dry_run:
        _ack_done_land_notices(deps)
    for finished in deps.lands.collect() if deps.lands is not None and not dry_run else []:
        land = finished["action"]
        if finished["status"] == "not_landed" or "needs_chair" in finished:
            _note_uncounted(land, finished, blocked, blocked_initiatives)
        deps.record(recorded_action(finished))
        results.append(finished)
        if finished["status"] not in ("landed", "fenced", "busy") and not finished.get("already_landed"):
            escalated = _escalate(land, finished)
            deps.record(recorded_action(escalated))
            results.append(escalated)
    for action in actions:
        if dry_run and action.get("kind") == "unlaunched":  # same result as live, and not a would item
            results.append(_execute(action, deps, blocked))
            continue
        if dry_run:
            results.append(make_result(action, "dry_run", _would(action)))
            continue
        initiative = action.get("initiative", "")
        holding = [b for b in (blocked_initiatives.get(initiative), blocked.get(action.get("repo", ""))) if b is not None]
        resumes = bool(holding) and all(_resumes_at_wait_checks(action, b) for b in holding)
        frozen: str | None = None  # set by the freeze branch below, so a frozen land is not escalated
        if action.get("kind") not in UNFENCED_KINDS and is_fenced(action, current_epoch()):
            result = make_result(action, "fenced", "planned under another lease epoch")
        elif action.get("kind") == "relaunch" and initiative in uncleared:
            result = make_result(action, "skipped", f"clear_branches for {initiative} was {uncleared[initiative]} this tick")
        elif (
            initiative in blocked_initiatives
            and action.get("kind") in (*LAND_ACTION_KINDS, "clear_branches", "relaunch", "launch_epic")
            and not resumes
        ):
            result = make_result(action, "skipped", f"an earlier land in {action.get('repo', '')} ({blocked_initiatives[initiative].label}) was not counted")
        elif action.get("kind") in LAND_ACTION_KINDS and (frozen := _frozen_reason(deps)) is not None:
            result = make_result(action, "skipped", frozen)
        else:
            if action.get("kind") in LAUNCH_KINDS:
                launch_host = action.get("host", "")
                if launch_host in launched_hosts and deps.launch_stagger_s > 0:
                    sleep(deps.launch_stagger_s)
                launched_hosts.add(launch_host)
            result = _execute(action, deps, {} if resumes else blocked)
        if action.get("kind") in LAND_ACTION_KINDS and (result["status"] == "not_landed" or "needs_chair" in result):
            _note_uncounted(action, result, blocked, blocked_initiatives)
        if action.get("kind") == "clear_branches" and result["status"] != "done":
            uncleared[initiative] = result["status"]
        # The land's own result is recorded when it is collected. An unlaunched is never recorded: the chair
        # repeats it every few minutes for every waiting initiative, so a row each tick would flood the store.
        if result["status"] != "in_progress" and action.get("kind") != "unlaunched":
            deps.record(recorded_action(result))
        results.append(result)
        if (
            action.get("kind") in LAND_ACTION_KINDS
            and frozen is None
            and result["status"] not in ("landed", "fenced", "busy", "in_progress", "recorded")
        ):
            escalated = _escalate(action, result)
            deps.record(recorded_action(escalated))
            results.append(escalated)
        if action.get("kind") in _CARRIES_NEEDS_CHAIR and "needs_chair" in result:
            carried = make_result(result["needs_chair"], "recorded", result["needs_chair"].get("reason", ""))
            deps.record(recorded_action(carried))
            results.append(carried)
    # After every action's result, so a caller that reads `results[0]` for its single action still gets that action's.
    if not dry_run and (removed := _sweep_pytest_tmp(deps)):
        swept = make_result(
            # recorded only: no planner plans the kind, so no housekeeping timer moves
            {"kind": ActionKind.PYTEST_TMP_SWEEP},
            "done",
            f"removed stale pytest run directories: {', '.join(removed)}",
        )
        deps.record(recorded_action(swept))
        results.append(swept)
    return results


def escalation(land: Action) -> Action:
    """The needs_chair a land that did not land raises when its command gave no refusal to classify: the facts
    dropped its stranded row, so this reports it instead."""
    return {
        "kind": ActionKind.NEEDS_CHAIR, "initiative": land.get("initiative", ""), "task_id": land.get("task_id", ""),
        "cause": STRANDED_CAUSE, "epoch": land.get("epoch", 0), **phase_action(land),
    }


LAND_CONFLICT_CAUSE = "land_conflict"


_LAND_CONFLICT_ROUTE = "Rebase the phase branch onto main, or re-run the phase."


def phase_task_states(rows: Sequence[Mapping[str, object]], phase: str) -> list[str]:
    """The `state` of each work_items row in `phase`; rows are one initiative's, as `run_store.work_items` returns them."""
    return [str(row.get("state")) for row in rows if row.get("phase") == phase]


def _stopped_land(land: Action, result: Result) -> Result | None:
    """The escalated needs_chair for a phase land whose output names the step it stopped at, else None.

    A real stop exits nonzero and arrives `refused` with `land_refusal`'s guessed cause; the stop line replaces
    that guess. A squash_phase stop is a conflict on the phase branch, so it takes its own cause and a routing
    sentence."""
    stop = (
        land_stop(result["reason"])
        if land.get("kind") == "land_phase" and result["status"] in ("not_landed", "refused")
        else None
    )
    if stop is None:
        return None
    step, detail = stop
    reason = f"land {land_subject(land)} stopped at {step}: {detail}"
    raised = escalation(land)
    if step == "squash_phase":
        return make_result({**raised, "cause": LAND_CONFLICT_CAUSE}, "escalated", f"{reason}. {_LAND_CONFLICT_ROUTE}")
    return make_result(raised, "escalated", reason)


def _escalate(land: Action, result: Result) -> Result:
    """The escalated needs_chair row; a needs_chair that says what stopped it (a partial land's cherry_pick conflict
    names its paths) keeps that after the land's subject and status."""
    stopped = _stopped_land(land, result)
    if stopped is not None:
        return stopped
    raised = result.get("needs_chair") or escalation(land)
    why = str(raised.get("reason", ""))
    return make_result(raised, "escalated", f"land {land_subject(land)} {result['status']}" + (f": {why}" if why else ""))


# The monorepo `coxswain` carries tools and graphs now; the component names stay for an unmigrated chair.
SMOKE_REPOS = frozenset({"coxswain", "coxswain-tools", "coxswain-graphs"})
PR_URL_RE = re.compile(r"^pr_create: \S*/pull/(\d+)\s*$", re.MULTILINE)  # only the step line, never a stray /pull/ elsewhere
