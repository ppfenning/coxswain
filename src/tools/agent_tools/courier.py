"""Courier reference: parse, format, and resolve `coxswain://<kind>/<id>` (docs/design/courier.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

from agent_tools.route import intake_entries, parse_frontmatter
from agent_tools.stats_ingest import LEDGER_PATH, _read_ledger

__all__ = [
    "EVENTS", "EXCEPTION_EVENTS", "Reference", "ack", "ack_file", "append_file", "append_line", "format_reference",
    "inbox", "needs_model", "parse_reference", "parse_run_exited", "resolve", "run_exited_fields", "send",
]

_KINDS = ("run", "task", "pr", "intake", "proposal", "finding", "initiative")
EVENTS = (
    "needs_attention", "level_merged", "merge_failed", "relaunched", "stuck", "gate_passed", "gate_failed",
    "epic_complete", "run_exited",
    "chat_directive", "chat_question", "chat_answer", "directive_applied", "directive_refused",
)
EXCEPTION_EVENTS = ("needs_attention", "merge_failed", "stuck", "gate_failed")
_PATTERN = re.compile(r"^coxswain://([a-z]+)/(.+)$")


class Reference(NamedTuple):
    kind: str
    id: str


def parse_reference(s: str) -> Reference | None:
    match = _PATTERN.match(s)
    if match is None:
        return None
    kind, id_ = match.group(1), match.group(2)
    return Reference(kind, id_) if kind in _KINDS else None


def format_reference(ref: Reference) -> str:
    return f"coxswain://{ref.kind}/{ref.id}"


def _run_record(runs_dir: Path, run_id: str) -> dict | None:
    matches = sorted((runs_dir / run_id / "tasks").glob("*/*.json"))
    if not matches:
        return None
    try:
        return json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _finding(record: dict) -> dict | None:
    """`arbitration.reasoning`, falling back to `adversary`'s `why_wrong` (the fields `runs_detail._objection` reads)."""
    arbitration = record.get("arbitration")
    reasoning = arbitration.get("reasoning") if isinstance(arbitration, dict) else None
    if reasoning:
        return {"reasoning": reasoning}
    adversary = record.get("adversary") or []
    findings = adversary if isinstance(adversary, list) else [adversary]
    for finding in findings:
        why_wrong = (finding or {}).get("why_wrong") if isinstance(finding, dict) else None
        if why_wrong:
            return {"reasoning": why_wrong}
    return None


def _ledger_row(key: str, ledger_path: Path) -> dict | None:
    return next((row for row in _read_ledger(ledger_path, []) if row.get("key") == key), None)


def _work_item(workspace_dir: Path, rel_path: str) -> dict | None:
    try:
        text = (workspace_dir / rel_path).read_text(encoding="utf-8")
    except OSError:
        return None
    fields, body = parse_frontmatter(text)
    return {**fields, "body": body} if fields else None


def _find_by_id(workspace_dir: Path, glob_pattern: str, ref_id: str) -> Path | None:
    for path in sorted(workspace_dir.glob(glob_pattern)):
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fields and fields.get("id") == ref_id:
            return path
    return None


def _resolve_by_search(workspace_dir: Path, ref_id: str, direct: Path | None, glob_pattern: str) -> dict | None:
    """`task`/`initiative`: a path-shaped `ref_id` resolves as today; otherwise search by frontmatter `id`
    and stamp the found record with `path`, the workspace-relative path, for `cox courier inbox` to print."""
    if (workspace_dir / ref_id).exists():
        return _work_item(workspace_dir, ref_id)
    path = direct if direct and direct.exists() else _find_by_id(workspace_dir, glob_pattern, ref_id)
    if path is None:
        return None
    rel = path.relative_to(workspace_dir).as_posix()
    item = _work_item(workspace_dir, rel)
    return {**item, "path": rel} if item else None


def _intake_record(workspace_dir: Path, ticket_id: str) -> dict | None:
    """`route.intake_entries`, over every file under `intake/`, filtered to the entry whose own `id` matches —
    the ticket id courier.md fixes as `intake`'s id, distinct from a task's path."""
    intake_dir = workspace_dir / "intake"
    if not intake_dir.is_dir():
        return None
    files = {p.relative_to(intake_dir).as_posix(): p.read_text(encoding="utf-8") for p in intake_dir.rglob("*.md")}
    entry = next((e for e in intake_entries(files) if e["id"] == ticket_id), None)
    return _work_item(workspace_dir, entry["path"]) if entry else None


def resolve(ref: Reference, workspace_dir: Path | str, ledger_path: Path | str | None = None) -> dict | None:
    workspace_dir = Path(workspace_dir)
    runs_dir = workspace_dir / "runs"
    if ref.kind == "run":
        return _run_record(runs_dir, ref.id)
    if ref.kind == "finding":
        record = _run_record(runs_dir, ref.id)
        return _finding(record) if record else None
    if ref.kind in ("pr", "proposal"):
        return _ledger_row(ref.id, Path(ledger_path) if ledger_path is not None else LEDGER_PATH)
    if ref.kind == "intake":
        return _intake_record(workspace_dir, ref.id)
    if ref.kind == "task":
        return _resolve_by_search(workspace_dir, ref.id, None, f"work/*/*/{ref.id}.md")
    if ref.kind == "initiative":
        direct = workspace_dir / "work" / ref.id / "initiative.md"
        return _resolve_by_search(workspace_dir, ref.id, direct, "work/*/initiative.md")
    return _work_item(workspace_dir, ref.id)


def needs_model(entry: dict) -> bool:
    return entry.get("event") in EXCEPTION_EVENTS


def run_exited_fields(run: str, initiative: str, outcome: str) -> dict:
    return {"run": run, "initiative": initiative, "outcome": outcome}


def parse_run_exited(entry: dict) -> tuple[str, str, str] | None:
    fields = entry.get("fields")
    if entry.get("event") != "run_exited" or not isinstance(fields, dict):
        return None
    values = tuple(fields.get(k) for k in ("run", "initiative", "outcome"))
    return values if all(isinstance(v, str) for v in values) else None


def send(
    ref: Reference, sender: str, to: str, note: str, message_id: str,
    *, event: str | None = None, fields: dict | None = None, topic: str | None = None,
) -> dict:
    """Builds one bus entry; never writes it (docs/design/courier.md `#the-bus`).
    `event`, `fields` and `topic` add their key only when given; an unknown `event` raises ValueError."""
    if event is not None and event not in EVENTS:
        raise ValueError(f"unknown courier event {event!r}; expected one of {', '.join(EVENTS)}")
    base = {"ref": format_reference(ref), "from": sender, "to": to, "note": note, "id": message_id, "ack": False}
    extras = {"event": event, "fields": fields, "topic": topic}
    return {**base, **{k: v for k, v in extras.items() if v is not None}}


def append_line(blob: str, entry: dict) -> str:
    return blob + json.dumps(entry) + "\n"


def _latest_by_id(blob: str) -> dict[str, dict]:
    """Last line per id wins, in first-seen order."""
    latest: dict[str, dict] = {}
    for line in blob.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and "id" in entry:
            latest[entry["id"]] = _fold(latest.get(entry["id"]), entry)
    return latest


def _fold(prior: dict | None, entry: dict) -> dict:
    """The last line wins, but `acks` is unioned and a legacy `ack` stays true once any line set it,
    so a line appended from a stale read cannot lose another reader's ack or revive a hidden entry."""
    acks = list(dict.fromkeys([*(prior or {}).get("acks", []), *(entry.get("acks") or [])]))
    legacy = bool((prior or {}).get("ack")) and not entry.get("ack")
    return {**entry, **({"acks": acks} if acks else {}), **({"ack": True} if legacy else {})}


def inbox(
    blob: str, label: str | None = None, holder: str | None = None,
    topic: str | None = None, event: str | None = None, consumer: str | None = None,
) -> list[dict]:
    """`holder`: the label currently holding the leader lock, read at the edge.
    An entry addressed to the generic `chair` reaches `label` when `label` is the
    holder or `label` itself starts with `chair`; any other `to` needs an exact match.
    `consumer` hides an entry that consumer has acked; with none, only the legacy `ack` flag hides it."""
    entries = _latest_by_id(blob).values()

    def _matches(e: dict) -> bool:
        if label is None or e["to"] == label:
            return True
        return e["to"] == "chair" and (label == holder or label.startswith("chair"))

    def _hidden(e: dict) -> bool:
        return bool(e["ack"]) or (consumer is not None and consumer in e.get("acks", []))

    return [
        e for e in entries
        if not _hidden(e) and _matches(e)
        and (topic is None or e.get("topic") == topic) and (event is None or e.get("event") == event)
    ]


def _ack_line(entry: dict, consumer: str | None) -> dict:
    """A consumer ack records the consumer in `acks` and leaves the legacy `ack` flag as it was."""
    if consumer is None:
        return {**entry, "ack": True}
    return {**entry, "acks": list(dict.fromkeys([*entry.get("acks", []), consumer]))}


def ack(blob: str, message_id: str, consumer: str | None = None) -> str:
    entry = _latest_by_id(blob).get(message_id)
    return blob if entry is None else append_line(blob, _ack_line(entry, consumer))


def read_text(path: Path) -> str:
    """Edge. The bus named by `path` as JSON lines; "" when there is none yet. With a shared store the bus is the
    store's courier log (`courier_bus`), else the file itself."""
    from agent_tools import courier_bus

    if courier_bus.uses_store(Path(path)):
        return courier_bus.read_text(Path(path))
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def stamp(path: Path) -> tuple:
    """Edge. A value that changes whenever the bus named by `path` gains a line, for a cache key: the store's line
    count, or the file's size and mtime. No size for a bus that cannot be read."""
    from agent_tools import courier_bus

    if courier_bus.uses_store(Path(path)):
        return (str(path), "store", courier_bus.line_count(Path(path)))
    try:
        info = Path(path).stat()
    except OSError:
        return (str(path), None)
    return (str(path), info.st_size, info.st_mtime_ns)


def append_file(path: Path, entry: dict) -> None:
    """Appends one line and never reads or rewrites the bus. In a file, a final line missing its newline
    (a torn write) is closed first so the entry stays on its own line."""
    from agent_tools import courier_bus

    if courier_bus.uses_store(Path(path)):
        courier_bus.append_line(Path(path), json.dumps(entry))
        return
    with open(path, "ab+") as f:
        size = f.seek(0, 2)
        if size > 0:
            f.seek(size - 1)
        last = f.read(1) if size > 0 else b"\n"
        torn = last != b"\n"
        f.write((b"\n" if torn else b"") + json.dumps(entry).encode("utf-8") + b"\n")


def ack_file(path: Path, message_id: str, consumer: str | None = None) -> bool:
    """Appends one ack line and never rewrites the bus; False when no entry has `message_id`."""
    blob = read_text(path)
    if not blob:
        return False
    entry = _latest_by_id(blob).get(message_id)
    if entry is None:
        return False
    append_file(path, _ack_line(entry, consumer))
    return True


def entries(blob: str) -> list[dict]:
    """Every entry once, in first-seen order, at its latest state."""
    return list(_latest_by_id(blob).values())
