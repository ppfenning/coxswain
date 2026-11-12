"""`route edit` core: rewrite a queued intake, or an initiative that has not started, and say what changed as a diff.

`plan_edit` and `check_options` are pure. `apply_edit` is the edge: it reads the store and the file, asks
`route_guard` whether the target may change, and writes the store first and the file second, the order
`cox route file` uses. The id and the path never change on an edit.

An initiative edit writes its title, body and frontmatter into the store's initiative row first, when there is one, then
rewrites `work/<id>/initiative.md`. An initiative's task rows are not touched.
`live_runs` is required, with no default, so a caller cannot skip the live-run check by omission. Build it as
`{lane.run for lane in run_store.live_lanes(runs_dir, now)}` and narrow it to the runs recorded on the edited initiative;
a run with no readable record stays in the set, and the guard refuses on any run in it.
A store that cannot take an intake's row is one printed warning from `route.write_filed_item` and the file is still written.
A task ticket (a `work_items` row that `resolve_target` does not name) is edited store first. `--state` goes through
`store_cli.set_state`, the single writer for work state, with a compare-and-set on the state the edit read; a move out of
done or dropped, a move backward along ready -> in_progress -> approved -> done, and a move on a row a launch claim holds
are refused. `--title`, `--surfaces` and the body go to `run_store.upsert_row_detail` as a partial row that never carries
`state`, so a state the loop set meanwhile survives. Then `queue_export.export_file(force=True)` rewrites the one file from
the row as the store now holds it and records the hash, so a hand-edited file is overwritten by the store text, except on a
requeue: a move to ready from a state other than todo, or a rebuild. There the file's body and needs are read through
`queue_rows.parse_item`, and when they differ from the row they are adopted into the planned row (`file_edit`), so the export
keeps them. With a differing file and `--body` or `--body-file` the edit is refused with a diff of the two bodies.
`--note` appends its text to the body with `append_note`, once; it applies to a task ticket only.
`--surfaces` and `--state` apply to a task ticket only.
`--epic` takes an epic id or `none` and applies to an initiative only: it sets or removes the `epic` frontmatter key and
the same key in the stored row's `extra`. The chair reads epic membership each tick and it changes no ticket, so an edit
that gives `--epic` and no other option skips `route_guard.initiative_guard`: a started initiative, or one with a live
run, may change epic. `--epic` with any other option goes through the guard as every other edit does.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Collection, Sequence
from functools import reduce
from pathlib import Path
from typing import NamedTuple

from agent_tools import (
    land_manual,
    queue_export,
    queue_rows,
    repo_path,
    route,
    route_guard,
    route_reach,
    run_store,
    store_cli,
)
from agent_tools.land import LAND_CONFLICT_KEY, conflict_note

DONE = route_guard.DONE
REFUSED = route_guard.REFUSED
NO_EPIC = "none"  # the `epic` value that removes the key
FAILED = 1  # a read or write failed after the options and the guard passed, as in route.write_filed_item


class EditResult(NamedTuple):
    text: str  # the unified diff on exit 0, the reason otherwise
    code: int  # DONE, REFUSED or FAILED


def check_options(
    title: str | None, body: str | None, body_file: str | None, repo: str | None,
    surfaces: str | None = None, state: str | None = None, rebuild: bool = False, force: bool = False, pr: str | None = None,
    after: Sequence[str] | None = None, link: str | None = None, epic: str | None = None, note: str | None = None,
) -> str | None:
    """The reason the options cannot be applied, or None. Body and body-file are mutually exclusive.
    `surfaces`, `state`, `after`, `link`, `epic` or `note` alone counts as a change, an empty `after` or `link` included;
    whether they are valid is `check_task_options` and `apply_edit`. An `epic` must be an epic id or `none`.
    `--rebuild` goes with `--state ready`, `--force` with `--rebuild` and `--pr` with `--state done`.
    A `note` is not empty and does not go with `--body` or `--body-file`."""
    if body is not None and body_file is not None:
        return "give either --body or --body-file, not both"
    if note is not None and (body is not None or body_file is not None):
        return "--note appends to the body; give it without --body or --body-file"
    if note is not None and not note.strip():
        return "--note must not be empty"
    if pr is not None and state != "done":
        return "--pr applies only with --state done"
    if rebuild and state != "ready":
        return "--rebuild applies only with --state ready"
    if force and not rebuild:
        return "--force applies only with --rebuild"
    unchanged = (surfaces, state, title, body, body_file, repo, after, link, epic, note)
    if all(option is None for option in unchanged):
        return "nothing to change: give --title, --body, --body-file, --note, --repo, --epic, --surfaces, --state or --link"
    if title == "" or repo == "":
        return "--title and --repo must not be empty"
    return _epic_problem(epic)


def _epic_problem(epic: str | None) -> str | None:
    """The validator's message when `epic` is neither None, `none` nor an epic id, else None."""
    from core.epics import valid_epic_id  # lazy: core sits beside agent_tools in one venv, as in queue_rows

    if epic is None or epic == NO_EPIC:
        return None
    try:
        valid_epic_id(epic)
    except ValueError as exc:
        return str(exc)
    return None


def _split_surfaces(surfaces: str) -> list[str]:
    return [path.strip() for path in surfaces.split(",") if path.strip()]


def check_task_options(state: str | None, surfaces: str | None) -> str | None:
    """The reason a task ticket's `state` or `surfaces` cannot be applied, or None. A state must be one of
    `route.STATES`; `surfaces` is comma-separated and must name at least one path."""
    if state is not None and state not in route.STATES:
        return f"invalid state {state!r}: use one of {', '.join(sorted(route.STATES))}"
    if surfaces is not None and not _split_surfaces(surfaces):
        return "--surfaces must name at least one path"
    return None


def plan_task_row(
    row: dict, *, title: str | None = None, body: str | None = None, surfaces: str | None = None, state: str | None = None
) -> dict:
    """A copy of the stored task `row` with only the named fields replaced; `surfaces` replaces the list.
    A stored body opens with the newline after the closing fence, so a new one is wrapped the same way."""
    named = {
        "title": title,
        "body": None if body is None else f"\n{body}\n",
        "surfaces": None if surfaces is None else _split_surfaces(surfaces),
        "state": state,
    }
    return {**row, **{key: value for key, value in named.items() if value is not None}}


def _split(text: str) -> tuple[list[str], str] | None:
    """`(header lines, everything from the closing fence on)`, or None when `text` has no frontmatter."""
    opening, closing = "---\n", "\n---\n"
    close_index = text.find(closing, len(opening)) if text.startswith(opening) else -1
    if close_index == -1:
        return None
    return text[len(opening):close_index].split("\n"), text[close_index:]


def clean_after(after: Sequence[str]) -> list[str]:
    """`after` with each id stripped, empty ids dropped and repeats removed, first occurrence kept."""
    return list(dict.fromkeys(stripped for stripped in (item.strip() for item in after) if stripped))


def _without_header_key(header_lines: list[str], key: str) -> list[str]:
    """`header_lines` without the `key` line and the block-list lines under it; every other line stays byte for byte."""
    start = next((i for i, line in enumerate(header_lines) if route._is_key_line(line, key)), None)
    if start is None:
        return header_lines
    end = next(
        (i for i in range(start + 1, len(header_lines)) if not (header_lines[i][:1].isspace() or header_lines[i].startswith("- "))),
        len(header_lines),
    )
    return [*header_lines[:start], *header_lines[end:]]


_LINK_KEYS = ("link", "issue", "issue_repo")


def _link_plan(link: str | None) -> tuple[tuple[str, ...], list[tuple[str, str]]]:
    """`(keys to drop, fields to set)` for `link`: nothing for None, all three keys dropped for an empty link, and for a
    url the `route.link_fields` pairs, with any link key those pairs do not name dropped so no old issue survives."""
    if link is None:
        return (), []
    fields = route.link_fields(link)
    named = {key for key, _ in fields}
    return tuple(key for key in _LINK_KEYS if key not in named), fields


def plan_edit(
    text: str, *, title: str | None = None, body: str | None = None, repo: str | None = None, path: str = "",
    after: Sequence[str] | None = None, link: str | None = None, epic: str | None = None,
) -> tuple[str, str]:
    """`(new text, unified diff)` of `text` with the given overrides. A header line not named stays byte for byte.
    An empty `body` falls back to the title, as `route.intake_file` does. The diff is empty when nothing changed.
    `after` sets the list in the order given, cleaned by `clean_after`; an empty one removes the key; None leaves it.
    `link` sets `link`, `issue` and `issue_repo` from `route.link_fields`; an empty one removes all three; None leaves them.
    `epic` sets the `epic` key to an id, or removes it for `none`; None leaves it.
    `text` must carry frontmatter; one without it comes back unchanged."""
    parts = _split(text)
    if parts is None:
        return text, ""
    header_lines, rest = parts
    cleaned = None if after is None else clean_after(after)
    dropped, link_fields = _link_plan(link)
    changes = [
        (key, value)
        for key, value in (("title", title), ("repo", repo), ("after", cleaned or None))
        if value is not None
    ] + link_fields + ([("epic", epic)] if epic not in (None, NO_EPIC) else [])
    removed = (*(("after",) if cleaned == [] else ()), *(("epic",) if epic == NO_EPIC else ()), *dropped)
    kept = reduce(_without_header_key, removed, header_lines)
    header = "\n".join(route._with_header_fields(kept, changes))
    fields, _ = route.parse_frontmatter(text)
    new_body = None if body is None else (body or title or fields.get("title", ""))
    tail = rest if new_body is None else f"\n---\n\n{new_body}\n"
    new_text = f"---\n{header}{tail}"
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return new_text, diff


def initiative_content(stored: dict, new_text: str) -> dict | None:
    """Pure: the partial row that carries an edited initiative.md into its stored initiative row: title, body and the
    file's frontmatter merged over the stored `extra` (which keeps keys such as `phases` the file never names), never
    `state`. The one exception is `epic`: a text with no `epic` key drops it from the stored extra, so `none` reaches the
    store. None when the text parses to no initiative row."""
    parsed = queue_rows.parse_item("initiative", (stored["initiative"], "initiative.md"), new_text)
    if parsed is None:
        return None
    kept = {key: value for key, value in (stored.get("extra") or {}).items() if key != "epic"}
    extra = {**kept, **parsed["extra"]}
    return {"initiative": stored["initiative"], "task_id": stored["task_id"], "title": parsed["title"], "body": parsed["body"], "extra": extra}


def _store_initiative(runs_dir: Path, initiative: str, new_text: str) -> bool:
    """Edge: True when the store holds no initiative row to update, or took the edit; False when it refused it."""
    stored = next((r for r in run_store.read_queue(runs_dir, initiative, kind="initiative") if r.get("task_id") == initiative), None)
    partial = None if stored is None else initiative_content(stored, new_text)
    return partial is None or run_store.upsert_row(runs_dir, partial)


def _rewrite(path: Path, runs_dir: Path, is_intake: bool, new_text: str) -> str | None:
    """Edge: the store first and the file second, for an intake and for an initiative. A reason on failure.
    The written file is recorded as the workspace's export baseline (`write_filed_item` records an intake), but an
    initiative.md only once its store row holds the edit: else the next export would put the old text back."""
    if is_intake:
        return None if route.write_filed_item(runs_dir, path, "intake", (path.name,), new_text) == 0 else f"could not write {path}"
    stored = _store_initiative(runs_dir, path.parent.name, new_text)
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return f"could not write {path}: {exc}"
    if stored:
        queue_export.record_written_paths(path.parents[2], [path])  # work/<id>/initiative.md under the workspace
    return None


# Along ready -> in_progress -> approved -> done a ticket only moves forward by hand: a move back could relaunch work
# that has started or ended. todo and ready share a rank, both unstarted. store_cli's set-state has no override flag.
_STATE_RANK = {"todo": 0, "ready": 0, "in_progress": 1, "approved": 2, "done": 3}
_FINAL_STATES = frozenset({"done", "dropped"})
# The states `harness.store_cli set-state` takes, as its target and as its `--expect`.
SET_STATES = ("ready", "approved", "done", "dropped")
_CONTENT_FIELDS = ("title", "body", "needs", "surfaces", "extra")  # needs changes only when a requeue adopts the file's
EDITED_BY = "cox route edit"
_REBUILD_HINT = "; --rebuild needs a recorded land conflict, or --force"
REBUILT_FROM_KEY = "rebuilt_from"  # `extra` key: every run id a rebuild superseded, kept across rebuilds


def rebuild_allowed(state: str | None, extra: dict | None, *, rebuild: bool, force: bool) -> bool:
    """Pure: `--rebuild` may send an approved task back to ready when its land recorded a conflict, or under `--force`.
    A land that recorded a conflict already left the task ready; `--rebuild` then only files the note and clears the marker."""
    marked = bool((extra or {}).get(LAND_CONFLICT_KEY))
    return rebuild and ((state == "approved" and (force or marked)) or (state == "ready" and marked))


def superseded_runs(initiative: str, run_ids: Collection[str], task_runs: Sequence[str], earlier: Sequence[str]) -> list[str]:
    """Pure: the runs a rebuild supersedes. `earlier` first, then `task_runs`, then every id `<initiative>-N` in `run_ids`
    sorted; no duplicates. The prefix rule is the one `run_store.task_run_phases` applies."""
    of_initiative = sorted(run for run in run_ids if run.rsplit("-", 1)[0] == initiative)
    return list(dict.fromkeys([*earlier, *task_runs, *of_initiative]))


def rebuild_note(conflict: str | None) -> str:
    """Pure: the body note a rebuild appends, naming the recorded conflict (`land conflict with ...`) if there is one."""
    if conflict:
        return conflict_note(conflict)
    return "Rebuild forced from approved with --force; no land conflict was recorded."


def append_note(body: str, note: str) -> str:
    """Pure: `body` (a stored body opens and ends with a newline) with `note` as a last paragraph, unless it is there already."""
    if note in body:
        return body
    return (body.rstrip("\n") + "\n\n" if body.strip() else "\n") + note + "\n"


def check_state_move(current: str | None, new: str, *, rebuild: bool = False) -> str | None:
    """Pure: the reason a hand move from `current` to `new` is refused, or None. Out of done or dropped is refused,
    and so is a move backward along ready -> in_progress -> approved -> done, and a move to a state the store's
    single writer does not set. `rebuild` (see `rebuild_allowed`) lets approved go back to ready."""
    if current == new or (rebuild and (current, new) == ("approved", "ready")):
        return None
    if new not in SET_STATES:
        return f"--state {new!r} is not one the store sets by hand: use one of {', '.join(SET_STATES)}"
    if current in _FINAL_STATES:
        return f"the ticket is {current!r}; a hand edit does not move it out of {current}"
    if current in _STATE_RANK and new in _STATE_RANK and _STATE_RANK[new] < _STATE_RANK[current]:
        return f"the ticket is {current!r}; moving it back to {new!r} could launch it again"
    return None


def check_claim(row: dict, now: str | None) -> str | None:
    """Pure: the reason a state move is refused because a launch claim holds the row, or None. A claim whose
    `expires_at` is at or before `now` (ISO text, compared as text as `live_lanes` does) no longer holds;
    with no `now`, any holder holds."""
    holder, expires = row.get("holder"), row.get("expires_at")
    if not holder:
        return None
    if now is not None and expires and str(expires) <= now:
        return None
    return f"{holder} holds a launch claim on {row.get('task_id')}; let that run finish or stop it first"


def content_row(row: dict, planned: dict) -> dict | None:
    """Pure: the partial row that writes only the content fields `planned` changed, keyed by initiative and task_id,
    or None when none changed. It never carries `state`, so the store's upsert keeps whatever state is stored now."""
    changed = {key: planned[key] for key in _CONTENT_FIELDS if planned.get(key) != row.get(key)}
    return {"initiative": row["initiative"], "task_id": row["task_id"], **changed} if changed else None


def _stored_row(runs_dir: Path, row: dict) -> dict | None:
    """Edge: the row as the store holds it now, or None when the store cannot say."""
    return next(
        (r for r in run_store.read_queue(runs_dir, row.get("initiative"), kind="task") if r.get("task_id") == row.get("task_id")), None
    )


def _set_state(runs_dir: Path, row: dict, state: str) -> EditResult | None:
    """Edge: move the row's state through store_cli's single writer, compare-and-set on the state the edit read.
    None when the store took it. set-state's `--expect` takes only `SET_STATES`; from any other state (todo, blocked,
    quarantined, in_progress) the row is re-read just before the write instead, which narrows the race but does not close it."""
    read = row.get("state")
    if read not in SET_STATES:
        stored = _stored_row(runs_dir, row)
        if stored is None or stored.get("state") != read:
            now = "no row" if stored is None else stored.get("state")
            return EditResult(f"{row['task_id']} moved to {now!r} after the edit read it as {read!r}; nothing written", REFUSED)
    expected = read if read in SET_STATES else None
    result = store_cli.set_state(runs_dir, row["initiative"], row["task_id"], state, EDITED_BY, expected=expected)
    if isinstance(result, store_cli.StateSet):
        return None
    if isinstance(result, store_cli.StateRefused):
        now = result.current or "no row"
        return EditResult(f"{row['task_id']} moved to {now!r} after the edit read it as {row.get('state')!r}; nothing written", REFUSED)
    detail = result.detail if isinstance(result, store_cli.Failed) else (getattr(result, "reason", "") or "harness not available")
    return EditResult(f"the store did not take the state of {row['task_id']}: {detail}", FAILED)


def _record_run(runs_dir: Path, row: dict) -> tuple[str, str] | None:
    """Edge: the `(run, phase)` that holds the task's PR: the newest run whose phase is the row's, else the newest run; None with no run."""
    runs = run_store.task_run_phases(runs_dir, row["initiative"], row["task_id"])
    return next((rp for rp in runs if rp[1] == row.get("phase")), runs[0]) if runs else None


def _record_pr(runs_dir: Path, row: dict, pr: str | None, now: str | None) -> str:
    """Edge. After a task moved to done, mirror its merged PR onto the newest run's record of it: `pr` as given, else the merged
    PR `gh` finds for the task's branches in its repo. The line to append to the output, "" when there is no PR to record.
    The task is already done, so every failure here is a printed note and never changes the exit code."""
    initiative, task = row["initiative"], row["task_id"]
    found = _record_run(runs_dir, row)
    if found is None:
        return f"note: no run holds {task}; PR not recorded\n" if pr else ""
    run, phase = found
    if pr is None:
        repo = repo_path.expand(str((row.get("extra") or {}).get("repo") or ""), os.path.expanduser("~"))
        branches = land_manual.pr_branches(initiative, phase, task, land_manual.record_branch(runs_dir, run, task))
        pr = land_manual.lookup_merged_pr(repo, branches) if repo else None
        if pr is None:
            return ""
    warning = land_manual.record_landed(runs_dir, run, phase, task, pr, now or "")
    return f"{warning}\n" if warning else f"recorded PR {pr} on {run}/{phase}/{task}\n"


def _recorded_pr(runs_dir: Path, row: dict) -> str | None:
    """Edge: the `landed.pr` on the run `_record_run` picks, None when no run holds the task or it names no PR."""
    found = _record_run(runs_dir, row)
    record = run_store.task_record(runs_dir, *found, row["task_id"]) if found else None
    landed = (record or {}).get("landed")
    return str(landed["pr"]) if isinstance(landed, dict) and landed.get("pr") else None


def _recorded_result(note: str) -> EditResult | None:
    """Pure: None when `_record_pr` recorded the PR, else its note as a DONE result, since the row is already done."""
    return None if note.startswith("recorded PR") else EditResult(note.rstrip("\n"), DONE)


_DONE_WITH_PR_FROM = ("approved", "ready", "quarantined")


def mark_done_with_pr(runs_dir: Path, row: dict, pr: str, now: str | None) -> EditResult | None:
    """Edge: approved, ready or quarantined row to done with `pr`; None once recorded, a DONE note when it could not be, else a REFUSED or FAILED result."""
    state, task = row.get("state"), row["task_id"]
    if state == "done":  # a retry: never a state write, and the PR is recorded only when no run holds one yet
        seen = _recorded_pr(runs_dir, row)
        if seen is not None and seen != pr:
            return EditResult(f"{task} is done with PR {seen}; not recording {pr}", REFUSED)
        return None if seen == pr else _recorded_result(_record_pr(runs_dir, row, pr, now))
    refusal = check_state_move(state, "done")
    if refusal is None and state not in _DONE_WITH_PR_FROM:
        refusal = f"the ticket is {state!r}; only an approved, ready or quarantined task is marked done with a PR"
    if refusal:
        return EditResult(refusal, REFUSED)
    failed = _set_state(runs_dir, row, "done")
    return failed if failed is not None else _recorded_result(_record_pr(runs_dir, row, pr, now))


def file_edit(stored: dict, rel: str, text: str) -> dict | None:
    """Pure: the `body` and `needs` of the task file `text` that differ from the `stored` row, keyed by field, or None when
    both match or the text parses to no task row. `rel` is the file's `work/<initiative>/<phase>/<task>.md` path.
    A row with no needs is compared on its body only: a file naming needs there does not put them back."""
    parsed = queue_rows.parse_item("task", tuple(Path(rel).parts[-3:]), text)
    if parsed is None:
        return None
    needs = list(stored.get("needs") or [])
    held = {"body": stored.get("body") or "", **({"needs": needs} if needs else {})}
    differs = {key: parsed[key] for key, value in held.items() if parsed[key] != value}
    return differs or None


def kept_refusal(stored: dict, rel: str, kept: dict) -> str:
    """Pure: the refusal text when an explicit body meets a file that differs from the row: a reason, then the unified diff
    of the stored needs and body against the file's."""
    def lines(needs: Collection[str], body: str) -> list[str]:
        return [f"needs: {', '.join(needs)}\n", *body.splitlines(keepends=True)]

    diff = "".join(
        difflib.unified_diff(
            lines(stored.get("needs") or [], stored.get("body") or ""),
            lines(kept.get("needs", stored.get("needs") or []), kept.get("body", stored.get("body") or "")),
            fromfile=f"a/{rel} (stored)", tofile=f"b/{rel} (file)",
        )
    )
    return f"{rel} was edited by hand; --body or --body-file would overwrite it. Stored against file:\n{diff}"


def _requeues(row: dict, new_row: dict, *, moves: bool, allowed: bool) -> bool:
    """Pure: True when the edit sends a ticket to ready as a requeue. A rebuild always is. A todo ticket has never been
    launched, so its move to ready keeps writing the stored text over the file."""
    return new_row.get("state") == "ready" and (allowed or (moves and row.get("state") != "todo"))


def _report(text: str, lines: Sequence[str]) -> str:
    """Pure: `text` with each of `lines` on a line of its own below it."""
    return "".join([f"{text.rstrip(chr(10))}\n" if text and lines else text, *(f"{line}\n" for line in lines)])


def _edit_task(
    runs_dir: Path, ws: Path, row: dict, *, title: str | None, body: str | None, body_file: str | None,
    surfaces: str | None, state: str | None, dry_run: bool, now: str | None, rebuild: bool = False, force: bool = False,
    pr: str | None = None, user_note: str | None = None,
) -> EditResult:
    """Edge: a state move first, through `store_cli.set_state` with a compare-and-set on the state read; then the changed
    content fields only, as a partial row `upsert_row_detail` merges (never `state`); then the store's row as it now stands,
    through `export_file(force=True)`. The diff is the file on disk against the text the planned row renders.
    On a requeue (`_requeues`) the file on disk is read first: a body or needs that differ from the row are adopted into the
    planned row, or refused with a diff when `body` or `body_file` also names a body. `user_note` is appended to the body once."""
    try:
        new_body = Path(body_file).read_text(encoding="utf-8").removesuffix("\n") if body_file is not None else body
    except OSError as exc:
        return EditResult(f"cannot read: {exc}", FAILED)
    new_row = plan_task_row(row, title=title, body=new_body, surfaces=surfaces, state=state)
    moves = new_row.get("state") != row.get("state")
    allowed = rebuild_allowed(row.get("state"), row.get("extra"), rebuild=rebuild, force=force)
    if moves:
        refusal = check_state_move(row.get("state"), new_row["state"], rebuild=allowed)
        hint = _REBUILD_HINT if refusal and rebuild and row.get("state") == "approved" else ""
        problem = (refusal and refusal + hint) or check_claim(row, now)
        if problem:
            return EditResult(problem, REFUSED)
    report: list[str] = []
    if _requeues(row, new_row, moves=moves, allowed=allowed):
        rel = queue_rows.row_path(new_row)
        try:
            on_disk = (ws / rel).read_text(encoding="utf-8") if (ws / rel).is_file() else None
        except OSError as exc:
            return EditResult(f"cannot read: {exc}", FAILED)
        kept = None if on_disk is None else file_edit(row, rel, on_disk)
        if kept is not None and new_body is not None:
            return EditResult(kept_refusal(row, rel, kept), REFUSED)
        if kept is not None:
            new_row = {**new_row, **kept}
            report.append(f"kept from the file: {' and '.join(kept)}")
    unmarked = ""
    if allowed:
        extra = dict(row.get("extra") or {})
        note = rebuild_note(extra.pop(LAND_CONFLICT_KEY, None))
        runs = [run for run, _ in run_store.task_run_phases(runs_dir, row["initiative"], row["task_id"])]
        superseded = superseded_runs(row["initiative"], run_store.run_ids(runs_dir), runs, extra.get(REBUILT_FROM_KEY) or [])
        marked = {**extra, REBUILT_FROM_KEY: superseded} if superseded else extra
        if not superseded and row.get("state") == "approved":
            unmarked = f"note: no run holds {row.get('task_id')}; its old approval is not marked superseded\n"
        new_row = {**new_row, "body": append_note(new_row.get("body") or "", note), "extra": marked}
    if user_note is not None:
        text = user_note.strip()
        noted = append_note(new_row.get("body") or "", text)
        report.append(f"note appended: {text}" if noted != (new_row.get("body") or "") else "note already in the body")
        new_row = {**new_row, "body": noted}
    if queue_export.is_contentless(new_row):
        return EditResult(f"task {row.get('task_id')!r} has no content to export", REFUSED)
    [(rel, new_text)] = queue_export.export_rows([new_row]).items()
    path = ws / rel
    try:
        old_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError as exc:
        return EditResult(f"cannot read: {exc}", FAILED)
    diff = "".join(
        difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), fromfile=f"a/{rel}", tofile=f"b/{rel}")
    )
    backfill = state == "done" and row.get("state") == "done"  # already done: --state done only records the PR
    unchanged = not diff and new_row == row
    if dry_run or (unchanged and not backfill):
        return EditResult(_report(diff, report), DONE)
    if moves:
        refused = _set_state(runs_dir, row, new_row["state"])
        if refused is not None:
            return refused
    note = unmarked + (_record_pr(runs_dir, row, pr, now) if new_row["state"] == "done" and (moves or backfill) else "")
    if unchanged:
        return EditResult(note or f"note: no merged PR found for {row.get('task_id')}; give --pr <url>\n", DONE)
    partial = content_row(row, new_row)
    if partial is not None:
        reason = run_store.upsert_row_detail(runs_dir, partial)
        if reason:
            return EditResult(f"the store did not take {row.get('task_id')}: {reason}", FAILED)
    stored = _stored_row(runs_dir, row) or new_row
    try:
        queue_export.export_file(ws, stored, force=True, hashes=queue_export.store_hashes(runs_dir))
    except OSError as exc:
        return EditResult(f"the store row was updated but {rel} could not be written: {exc}", FAILED)
    text = diff or f"store row {row.get('task_id')} updated; {rel} already matched"
    return EditResult(_report(f"{text.rstrip(chr(10))}\n{note}" if note else text, report), DONE)


def intake_reach_refusal(text: str, new_body: str, title: str | None) -> str | None:
    """The refusal a launched decompose would raise for the intake `text` once its body becomes `new_body`.
    The file's own header is kept, so its `reach_allow` applies; an empty body falls back to the title, as `plan_edit` does."""
    parts = _split(text)
    if parts is None:
        return None
    header_lines, _ = parts
    fields, _ = route.parse_frontmatter(text)
    candidate = "---\n" + "\n".join(header_lines) + f"\n---\n\n{new_body or title or fields.get('title', '')}\n"
    return route_reach.launch_text_refusal(candidate)


def apply_edit(
    runs_dir: Path,
    ws: Path,
    id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    body_file: str | None = None,
    repo: str | None = None,
    surfaces: str | None = None,
    state: str | None = None,
    dry_run: bool = False,
    live_runs: Collection[str],
    now: str | None = None,
    rebuild: bool = False,
    force: bool = False,
    pr: str | None = None,
    after: Sequence[str] | None = None,
    link: str | None = None,
    epic: str | None = None,
    note: str | None = None,
) -> EditResult:
    """Edge: edit the queued intake, the initiative or the task ticket named `id`. Exit 0 returns the diff; exit 2 returns the
    reason and writes nothing, whether the options are wrong, the id is unknown, or the guard refuses. With
    `dry_run` the diff is returned and neither the file nor the store is written.
    A move to done also records the task's merged PR on its newest run record: `pr`, else the one `gh` finds.
    `after` sets, or with an empty list clears, the ids a queued intake waits on; it is refused anywhere else.
    `link` sets or replaces a queued intake's link with its issue fields, or with an empty string clears all three;
    it is refused anywhere else.
    `epic` sets an initiative's epic to an id, or removes it with `none`, in the store row and in initiative.md; it is
    refused on an intake and a task ticket. Given alone it skips the initiative guard; see the module docstring.
    `note` is appended once to a task ticket's body, with or without a state move; it is refused on an intake and an initiative."""
    problem = check_options(title, body, body_file, repo, surfaces, state, rebuild, force, pr, after, link, epic, note)
    if problem is not None:
        return EditResult(problem, REFUSED)
    rows: Sequence[dict] = run_store.read_queue(runs_dir)
    target = route_guard.resolve_target(rows, id)
    if isinstance(target, route_guard.NotFound):
        tickets = [row for row in rows if row.get("kind", "task") == "task" and row.get("task_id") == id]
        if not tickets:
            return EditResult(f"no queued intake or initiative named {id!r}", REFUSED)
        if len(tickets) > 1:
            initiatives = ", ".join(sorted(str(row.get("initiative")) for row in tickets))
            return EditResult(f"{id!r} names a task in several initiatives ({initiatives}); the id alone cannot pick one", REFUSED)
        if repo is not None:
            return EditResult("--repo applies to an intake or an initiative, not a task ticket", REFUSED)
        if after is not None:
            return EditResult("--after applies to a queued intake, not a task ticket", REFUSED)
        if link is not None:
            return EditResult("--link applies to a queued intake, not a task ticket", REFUSED)
        if epic is not None:
            return EditResult("--epic applies to an initiative, not a task ticket", REFUSED)
        problem = check_task_options(state, surfaces)
        if problem is not None:
            return EditResult(problem, REFUSED)
        return _edit_task(
            runs_dir, ws, dict(tickets[0]), title=title, body=body, body_file=body_file, surfaces=surfaces, state=state, dry_run=dry_run,
            now=now, rebuild=rebuild, force=force, pr=pr, user_note=note,
        )
    if note is not None:
        return EditResult("--note applies to a task ticket, not an intake or an initiative", REFUSED)
    if surfaces is not None or state is not None:
        return EditResult("--surfaces and --state apply to a task ticket only", REFUSED)
    if after is not None and not isinstance(target, route_guard.QueuedIntake):
        return EditResult("--after applies to a queued intake, not an initiative", REFUSED)
    if link is not None and not isinstance(target, route_guard.QueuedIntake):
        return EditResult("--link applies to a queued intake, not an initiative", REFUSED)
    if epic is not None and isinstance(target, route_guard.QueuedIntake):
        return EditResult("--epic applies to an initiative, not a queued intake", REFUSED)
    if after is not None and id in clean_after(after):
        return EditResult(f"{id!r} cannot come after itself", REFUSED)
    if isinstance(target, route_guard.QueuedIntake):
        path, is_intake = ws / "intake" / f"{target.task_id}.md", True
    else:
        epic_only = epic is not None and all(
            option is None for option in (title, body, body_file, repo, surfaces, state, after, link)
        )
        tasks = [row for row in rows if row.get("kind", "task") == "task" and row.get("initiative") == id]
        guard = None if epic_only else route_guard.initiative_guard(tasks, live_runs)
        if isinstance(guard, route_guard.Refusal):
            return EditResult(guard.reason, REFUSED)
        path, is_intake = ws / "work" / id / "initiative.md", False
    try:
        new_body = Path(body_file).read_text(encoding="utf-8").removesuffix("\n") if body_file is not None else body
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return EditResult(f"cannot read: {exc}", FAILED)
    if route.parse_frontmatter(text)[0] == {}:
        return EditResult(f"{path} has no frontmatter to edit", REFUSED)
    if is_intake and new_body is not None:
        refusal = intake_reach_refusal(text, new_body, title)
        if refusal is not None:
            return EditResult(refusal, REFUSED)
    new_text, diff = plan_edit(text, title=title, body=new_body, repo=repo, path=str(path.relative_to(ws)), after=after, link=link, epic=epic)
    if dry_run or not diff:
        return EditResult(diff, DONE)
    failure = _rewrite(path, runs_dir, is_intake, new_text)
    return EditResult(diff, DONE) if failure is None else EditResult(failure, FAILED)
