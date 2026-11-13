"""`route hold` and `route release` core: mark a queued intake or an initiative held, or free it again. The edge writes; the name, date and reason come in as arguments."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_tools import queue_export, route_guard, run_store

HELD_KEYS = ("stage", "held", "hold_reason")
STAGES = ("draft", "ready")
_INITIATIVE_KEYS = ("held", "hold_reason")


@dataclass(frozen=True)
class Refused:
    reason: str  # one line, names the id


Plan = route_guard.QueuedIntake | route_guard.InitiativeFound | Refused


def hold_reason(by: str, date: str, reason: str) -> str:
    """`date` is an ISO day. Whitespace in the reason collapses so the field stays one line."""
    return f"held by {by} on {date}: {' '.join(reason.split())}"


def _split(text: str) -> tuple[str, list[str], str] | None:
    """(line ending, header lines, the text from the closing fence on), or None when there is no front matter."""
    eol = "\r\n" if text.startswith("---\r\n") else "\n"
    opening, closing = f"---{eol}", f"{eol}---{eol}"
    close_index = text.find(closing, len(opening) - len(eol)) if text.startswith(opening) else -1
    if close_index == -1:
        return None
    header = text[len(opening):close_index] if close_index >= len(opening) else ""
    return eol, header.split(eol) if header else [], text[close_index:]


def _join(eol: str, lines: list[str], rest: str) -> str:
    return f"---{eol}{eol.join(lines)}{rest}" if lines else f"---{rest}"


def _key_span(lines: list[str], key: str) -> tuple[int, int] | None:
    """The key's line and any block-list continuation lines under it, as a slice, or None when the key is absent."""
    start = next((i for i, line in enumerate(lines) if not line[:1].isspace() and line.split(":", 1)[0].strip() == key and ":" in line), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if not (lines[i][:1].isspace() or lines[i].startswith("- "))), len(lines))
    return start, end


def _with_line(lines: list[str], key: str, line: str) -> list[str]:
    span = _key_span(lines, key)
    return [*lines, line] if span is None else [*lines[: span[0]], line, *lines[span[1] :]]


def _without_key(lines: list[str], key: str) -> list[str]:
    span = _key_span(lines, key)
    return lines if span is None else [*lines[: span[0]], *lines[span[1] :]]


def _reason_line(by: str, date: str, reason: str) -> str:
    return f"hold_reason: {json.dumps(hold_reason(by, date, reason), ensure_ascii=False)}"


def _rewritten(text: str, edit, bare: str) -> str:
    """`edit` maps the header lines to new ones. `bare` is the front matter a file without any gets. Text stays byte for byte when `edit` changes nothing."""
    split = _split(text)
    if split is None:
        return f"---\n{bare}---\n{text}"
    eol, lines, rest = split
    new = edit(lines)
    return text if new == lines else _join(eol, new, rest)


def staged_text(text: str, stage: str) -> str:
    """The intake text with `stage` set and nothing else touched. An intake that already carries the stage comes back unchanged."""
    return _rewritten(text, lambda lines: _with_line(lines, "stage", f"stage: {stage}"), f"stage: {stage}\n")


def held_text(text: str, reason: str, by: str, date: str) -> str:
    """The intake text with `stage: draft` and `hold_reason` set and any `held` line removed; every other line, and the body, stay byte for byte."""
    reason_line = _reason_line(by, date, reason)
    edit = lambda lines: _with_line(_with_line(_without_key(lines, "held"), "stage", "stage: draft"), "hold_reason", reason_line)  # noqa: E731
    return _rewritten(text, edit, f"stage: draft\n{reason_line}\n")


def released_text(text: str) -> str:
    """The intake text with `stage: ready`, and without `hold_reason` and `held`. An intake that is already ready and not held comes back unchanged."""
    edit = lambda lines: _with_line(_without_key(_without_key(lines, "held"), "hold_reason"), "stage", "stage: ready")  # noqa: E731
    return _rewritten(text, edit, "stage: ready\n")


def _initiative_held_text(text: str, reason: str, by: str, date: str) -> str:
    """Initiatives have no stage: `held: true` and `hold_reason`, as before."""
    reason_line = _reason_line(by, date, reason)
    split = _split(text)
    if split is None:
        return f"---\nheld: true\n{reason_line}\n---\n{text}"
    eol, lines, rest = split
    return _join(eol, _with_line(_with_line(lines, "held", "held: true"), "hold_reason", reason_line), rest)


def _initiative_released_text(text: str) -> str:
    split = _split(text)
    if split is None:
        return text
    eol, lines, rest = split
    freed = _without_key(_without_key(lines, "held"), "hold_reason")
    return text if freed == lines else _join(eol, freed, rest)


def _plan(rows: Sequence[Mapping], target_id: str) -> Plan:
    target = route_guard.resolve_target(rows, target_id)
    if isinstance(target, route_guard.QueuedIntake | route_guard.InitiativeFound):
        return target
    return Refused(f"no queued intake named {target_id}")


def plan_hold(rows: Sequence[Mapping], target_id: str, reason: str) -> Plan:
    """`rows` are queue rows. A queued intake or an initiative is held; a decomposed or done intake resolves as not found."""
    if not reason.strip():
        return Refused(f"a hold on {target_id} needs a reason")
    return _plan(rows, target_id)


def plan_release(rows: Sequence[Mapping], target_id: str) -> Plan:
    return _plan(rows, target_id)


def plan_stage(rows: Sequence[Mapping], target_id: str) -> route_guard.QueuedIntake | Refused:
    """Only a queued intake has a stage; an initiative id is refused like an unknown one."""
    plan = _plan(rows, target_id)
    return plan if isinstance(plan, route_guard.QueuedIntake) else Refused(f"no queued intake named {target_id}")


def _refuse(verb: str, reason: str) -> int:
    print(f"{verb}: refused: {reason}")
    return route_guard.REFUSED


def _row_of(rows: Sequence[Mapping], task_id: str) -> Mapping:
    return next(row for row in rows if row.get("kind") == "intake" and row.get("state") == "queued" and row.get("task_id") == task_id)


def _initiative_row(rows: Sequence[Mapping], initiative: str) -> Mapping | None:
    return next((row for row in rows if row.get("kind") == "initiative" and row.get("task_id") == initiative), None)


def _extra_without(row: Mapping, keys: Sequence[str]) -> dict:
    return {k: v for k, v in (row.get("extra") or {}).items() if k not in keys}


def _row_without_held(row: Mapping) -> dict:
    return {**row, "extra": {**_extra_without(row, HELD_KEYS), "stage": "ready"}}


def _row_held(row: Mapping, value: str) -> dict:
    return {**row, "extra": {**_extra_without(row, ("held",)), "stage": "draft", "hold_reason": value}}


def _row_staged(row: Mapping, stage: str) -> dict:
    return {**row, "extra": {**(row.get("extra") or {}), "stage": stage}}


def _initiative_row_without_held(row: Mapping) -> dict:
    return {**row, "extra": _extra_without(row, _INITIATIVE_KEYS)}


def _initiative_row_held(row: Mapping, value: str) -> dict:
    return {**row, "extra": {**(row.get("extra") or {}), "held": "true", "hold_reason": value}}


def _locate(plan: route_guard.QueuedIntake | route_guard.InitiativeFound, ws: Path, rows: Sequence[Mapping]) -> tuple[str, str, Path, Mapping | None]:
    """(kind, id, the file, the store row or None when the store has none)."""
    if isinstance(plan, route_guard.QueuedIntake):
        return "intake", plan.task_id, ws / "intake" / f"{plan.task_id}.md", _row_of(rows, plan.task_id)
    return "initiative", plan.initiative, ws / "work" / plan.initiative / "initiative.md", _initiative_row(rows, plan.initiative)


def _apply(verb: str, ws: Path, runs_dir: Path, plan: route_guard.QueuedIntake | route_guard.InitiativeFound, rows: Sequence[Mapping], new_row, new_text, line: str, dry_run: bool) -> int:
    kind, ident, path, row = _locate(plan, ws, rows)
    if row is None:
        return _refuse(verb, f"no initiative row for {ident} in the store")
    if not path.is_file():
        return _refuse(verb, f"no {kind} file at {path}")
    sent = new_row(row)
    text = path.read_bytes().decode("utf-8")
    new = new_text(text)
    if dry_run:
        print(f"would {verb} {kind} {ident}: {line}")
        return route_guard.DONE
    if new == text and sent == dict(row):
        print(f"{verb}: {kind} {ident}: nothing to change")
        return route_guard.DONE
    if not run_store.upsert_row(runs_dir, sent):
        print(f"{verb}: the store did not take {kind} {ident}")
        return 1
    try:
        path.write_bytes(new.encode("utf-8"))
    except OSError as exc:
        print(f"{verb}: could not write {path}: {exc}")
        return 1
    queue_export.record_written_paths(ws, [path])
    print(f"{verb}: {kind} {ident}: {line}")
    return route_guard.DONE


def hold(
    ws: Path, runs_dir: Path, id: str, reason: str, by: str, now: datetime, rows: Sequence[Mapping], *, dry_run: bool = False,
) -> int:
    """Edge. `rows` are queue rows; `runs_dir` is the store's, whose parent is `ws`. Exit 0 done, 1 a write failed, 2 refused with nothing written.

    The store takes the row first and the file (`intake/<id>.md` or `work/<id>/initiative.md`) follows, as `route.set_intake_priority` does.
    """
    plan = plan_hold(rows, id, reason)
    if isinstance(plan, Refused):
        return _refuse("hold", plan.reason)
    date = now.date().isoformat()
    value = hold_reason(by, date, reason)
    if isinstance(plan, route_guard.InitiativeFound):
        return _apply("hold", ws, runs_dir, plan, rows, lambda row: _initiative_row_held(row, value), lambda text: _initiative_held_text(text, reason, by, date), value, dry_run)
    return _apply("hold", ws, runs_dir, plan, rows, lambda row: _row_held(row, value), lambda text: held_text(text, reason, by, date), value, dry_run)


def release(ws: Path, runs_dir: Path, id: str, rows: Sequence[Mapping], *, dry_run: bool = False) -> int:
    """Edge. Exit codes as `hold`. Releasing an initiative that is not held, or an intake that is ready and not held, writes nothing."""
    plan = plan_release(rows, id)
    if isinstance(plan, Refused):
        return _refuse("release", plan.reason)
    if isinstance(plan, route_guard.InitiativeFound):
        return _apply("release", ws, runs_dir, plan, rows, _initiative_row_without_held, _initiative_released_text, "held fields removed", dry_run)
    return _apply("release", ws, runs_dir, plan, rows, _row_without_held, released_text, "stage ready, hold removed", dry_run)


def set_stage(ws: Path, runs_dir: Path, target_id: str, stage: str, rows: Sequence[Mapping], dry_run: bool = False) -> int:
    """Edge. The plain draft and ready moves: sets `stage` and leaves `hold_reason` alone. Exit codes as `hold`."""
    if stage not in STAGES:
        return _refuse("stage", f"{stage} is not a stage for {target_id}; use draft or ready")
    plan = plan_stage(rows, target_id)
    if isinstance(plan, Refused):
        return _refuse("stage", plan.reason)
    return _apply("stage", ws, runs_dir, plan, rows, lambda row: _row_staged(row, stage), lambda text: staged_text(text, stage), f"stage {stage}", dry_run)
