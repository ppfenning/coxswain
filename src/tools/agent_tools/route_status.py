"""The routing core, status: the status rows and lines, frontmatter, states and what waits on what."""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from agent_tools import intake_queue, release_gate, run_store
from agent_tools.intake_state import intake_group, intake_index

# Not core.landing.DEFAULT_LAND_AT ("ticket"): callers such as chair_read_docket and cli.py do not pass land_at yet, and
# `task` keeps their needs as strict as before. The wiring task passes the resolved value explicitly.
DEFAULT_LAND_AT = "task"


def _need_met(land_at: str, state: str | None, merged: bool) -> bool:
    from core.landing import need_met  # lazy: core sits beside agent_tools in one venv

    return need_met(land_at, state or "", merged)


def _met_by_launch(land_at: str, state: str | None, merged: bool) -> bool:
    """Readiness and waits-on lines: under `task` only done or dropped meets a need, as before; above it `need_met` decides."""
    return state in TERMINAL or (land_at != "task" and _need_met(land_at, state, merged))


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def phase_summary(items: Sequence[Mapping]) -> dict[str, list[dict]]:
    """Per initiative, its phases in first-seen order as `{phase, state, tasks}`.

    Dropped tasks are ignored and a phase left with none is omitted. `landed`: every task is done.
    `merged`: every task is done or approved with the merge flag, and at least one is not done yet.
    Anything else is `building`."""
    keys = dict.fromkeys((i["initiative"], i["phase"]) for i in items)
    out: dict[str, list[dict]] = {}
    for initiative, phase in keys:
        live = [i for i in items if (i["initiative"], i["phase"]) == (initiative, phase) and i["state"] != "dropped"]
        if not live:
            continue
        words = [state_word(i) for i in live]
        state = "landed" if all(w == "done" for w in words) else "merged" if all(w in ("done", "merged") for w in words) else "building"
        out[initiative] = [*out.get(initiative, []), {"phase": phase, "state": state, "tasks": len(live)}]
    return out


def task_tally(items: Sequence[Mapping]) -> dict[str, int]:
    """Counts of `approved`, `merged`, `done` and `total` tasks. A merged task counts under `merged` only, never under `approved`."""
    words = [state_word(i) for i in items]
    return {"approved": words.count("approved"), "merged": words.count("merged"), "done": words.count("done"), "total": len(words)}


def status_rows(entries, short_ids: Mapping[str, str] | None = None, detail: Mapping[str, Mapping] | None = None) -> list:
    """Format the rows `agent-tools route status` prints, spec §5, from a
    pre-gathered list of run entries. Each entry is
    `{"id", "pid", "alive", "started", "quarantined", "reused", "summary",
    "usage"}` — `pid`/`alive`/`started` from the CLI's own pidfile read,
    the rest already produced by `epic.summarize_log` on the run's log.
    `pid` is None for a run with no pidfile at all, which this renders as
    a distinct "no pidfile" state rather than a crash. Order is preserved
    from `entries`; no pidfile is read and `epic.alive` is not called here.
    `short_ids` maps slug to short id as plain data; a row's `short_id` is None when its slug is absent.
    `detail` maps slug to `{"phases": [...], "tally": {...}}` (from `phase_summary` and `task_tally`); a row gains `phases` and `tally` only when its slug is in it.
    """
    rows = []
    for entry in entries:
        pid = entry.get("pid")
        fenced = bool(entry.get("fenced")) and pid is not None
        if pid is None:
            state = "no pidfile"
        elif fenced:
            state = "fenced"
        elif entry.get("alive"):
            state = "alive"
        else:
            state = "exited"
        rows.append(
            {
                "id": entry["id"],
                "pid": pid,
                "state": state,
                "fenced": fenced,
                "started": entry.get("started"),
                "quarantined": entry.get("quarantined", []),
                "reused": entry.get("reused", []),
                "summary": entry.get("summary"),
                "usage": entry.get("usage"),
                "short_id": (short_ids or {}).get(entry["id"]),
                **_detail_fields((detail or {}).get(entry["id"])),
            }
        )
    return rows


def _detail_fields(detail: Mapping | None) -> dict:
    return {} if not detail else {"phases": list(detail.get("phases", [])), "tally": dict(detail.get("tally", {}))}


def recent_rows(rows: list, now: datetime.datetime, hours: int = 24) -> tuple[list, int]:
    """Rows that are alive or started within `hours` of `now`, and the count hidden.
    A dead row whose `started` is missing or unparseable counts as old; a naive `started` is UTC."""
    cutoff = now - datetime.timedelta(hours=hours)
    kept = [row for row in rows if _alive(row) or _started_since(row.get("started"), cutoff)]
    return kept, len(rows) - len(kept)


def _alive(row: dict) -> bool:
    return bool(row.get("alive")) or row.get("state") in ("alive", "fenced") or bool(row.get("fenced"))


def _started_since(started, cutoff: datetime.datetime) -> bool:
    try:
        when = datetime.datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return False
    return (when if when.tzinfo else when.replace(tzinfo=datetime.UTC)) >= cutoff


FENCED_NOTE = (
    "the run's process still answers but its lease heartbeat stopped, so the epoch fence refuses its writes "
    "and it is not making progress; stop that pid on its host, then relaunch."
)  # names no pid, so other screens can reuse it


def _status_line(row: dict, short_id: str | None = None) -> str:
    short = short_id or row.get("short_id")
    name = f"{row['id']} [{short}]" if short else row["id"]
    head = (
        f"{name}: {row['state']}"
        if row["pid"] is None
        else f"{name}: {row['state']} (pid {row['pid']}, started {row['started']})"
    )
    tails = (
        ["; ".join(row[key]) for key in ("quarantined", "reused") if row.get(key)]
        + [row[key] for key in ("summary", "usage") if row.get(key)]
        + _phase_tails(row)
        + ([FENCED_NOTE] if row["state"] == "fenced" else [])
    )
    return " ".join([head, *tails])


def _phase_tails(row: dict) -> list[str]:
    """`phases: p1 merged, p2 building` and `tally: 7 approved, 9 merged`, each only when the row carries it."""
    phases = ", ".join(f"{p['phase']} {p['state']}" for p in row.get("phases") or [])
    tally = row.get("tally") or {}
    counts = ", ".join(f"{tally[k]} {k}" for k in ("approved", "merged", "done") if tally.get(k))
    return ([f"phases: {phases}"] if phases else []) + ([f"tally: {counts}"] if counts else [])


def intake_tags(entry: Mapping) -> str:
    """` [stage ready, repo r, epic e, priority 3, target 2026.12.0]` from an intake entry, leaving out any part
    that is absent, empty or zero; empty when none remain. `repo` shows the last path component."""
    priority = _int_or_zero(entry.get("priority"))
    repo = os.path.basename(str(entry.get("repo") or "").rstrip("/"))
    parts = [
        *([f"stage {entry['stage']}"] if entry.get("stage") else []),
        *([f"repo {repo}"] if repo else []),
        *([f"epic {entry['epic']}"] if entry.get("epic") else []),
        *([f"priority {priority}"] if priority else []),
        *([f"target {entry['target_release']}"] if entry.get("target_release") else []),
    ]
    return f" [{', '.join(parts)}]" if parts else ""


def _intake_group_line(name: str, entries: list, waiting: Mapping[str, Sequence[str]] | None = None) -> str:
    """`waiting` (intake id to the ids it waits on) annotates a queued entry as `id (waiting on a, b)`.
    An entry's stage, repo, epic, priority and target release follow as `intake_tags`."""
    waits = waiting or {}

    def label(entry: Mapping) -> str:
        on = waits.get(entry["id"]) if name == "queued" else None
        return (f"{entry['id']} (waiting on {', '.join(on)})" if on else entry["id"]) + intake_tags(entry)

    names = ", ".join(label(entry) for entry in entries)
    return f"intake {name}: {len(entries)}" + (f" — {names}" if entries else "")


def group_deferred(*id_targets: Mapping[str, str]) -> dict[str, list[str]]:
    """Id-to-target mappings as target-to-sorted-ids, targets ascending by version. Targets that parse to the
    same version (`1.2`, `v1.2`) share one heading, labelled with the smaller text."""
    pairs = {(release_gate.release_tuple(t) or (), t, i) for ids in id_targets for i, t in ids.items()}
    versions = sorted({version for version, _, _ in pairs})
    label = {v: min(t for version, t, _ in pairs if version == v) for v in versions}
    return {label[v]: sorted({i for version, _, i in pairs if version == v}) for v in versions}


def deferred_by_target(queue: Sequence[Mapping], current: str) -> dict[str, list[str]]:
    """Deferred initiatives and intakes grouped by target release. An intake that names a deferred initiative is
    left out: the initiative stands for it."""
    initiatives = release_gate.deferred_initiatives(queue, current)
    names = {str(r["task_id"]): str((r.get("extra") or {}).get("initiative") or "").strip()
             for r in queue if r["kind"] == "intake"}
    intakes = {i: t for i, t in release_gate.deferred_intakes(queue, current).items() if names.get(i, "") not in initiatives}
    return group_deferred(intakes, initiatives)


def held_initiative_entries(
    queue: Sequence[Mapping], held_ids: Collection[str], held_intakes: Sequence[Mapping] = ()
) -> list[dict]:
    """Held rows for initiatives: id and `hold_reason`, from the initiative's own row, else a named intake.
    An initiative a listed held intake names, or whose id that intake has, is left out: its hold is already shown."""
    def named(row: Mapping) -> str:
        return str((row.get("extra") or {}).get("initiative") or "").strip()

    def reason(name: str) -> str:
        own = [r for r in queue if r["kind"] == "initiative" and str(r.get("initiative") or r["task_id"]) == name]
        via = [r for r in queue if r["kind"] == "intake" and r["state"] != "removed" and named(r) == name]
        texts = (str((r.get("extra") or {}).get("hold_reason") or "") for r in [*own, *via])
        return next((t for t in texts if t), "")

    covered = {named(r) for r in held_intakes} | {str(r["task_id"]) for r in held_intakes}
    return [{"id": name, "reason": reason(name)} for name in sorted(set(held_ids) - covered)]


def chair_machine_line(mode_line: str) -> str:
    """`chair machine <host>: <mode>` from `chair_orchestrate.mode_line`'s `<host>: <mode> (<availability>)`."""
    return f"chair machine {mode_line.rsplit(' (', 1)[0]}"


def merged_event_lines(events: Sequence[Mapping]) -> list[str]:
    """`merged: a, b into a` per `tasks_merged` event, in order; other events are skipped."""
    return [
        f"merged: {', '.join(str(t) for t in event.get('tasks', []))} into {event.get('into', '')}"
        for event in events
        if event.get("event") == "tasks_merged"
    ]


def render_status(rows: list, groups: dict | None = None, problems: list | None = None,
                  gate_level: str | None = None, hidden: int = 0,
                  short_ids: Mapping[str, str] | None = None,
                  waiting: Mapping[str, Sequence[str]] | None = None,
                  held: Sequence[Mapping] | None = None,
                  deferred: Mapping[str, Sequence[str]] | None = None,
                  chair_line: str = "",
                  events: Mapping[str, Sequence[Mapping]] | None = None) -> str:
    """The human-readable text `agent-tools route status` prints, spec §5,
    from `status_rows`' output. One line per row: a run with no pidfile
    states only its id and state, since `pid` and `started` are both
    `None` for that row and printing them would show the word "None"
    twice for no reason. `quarantined`, `reused`, `summary` and `usage`
    are appended only when the row carries them, so a quiet run stays
    one line. A fenced row ends with `FENCED_NOTE`. `groups`, an `intake_groups` result, appends one line per
    group when given, and nothing when `None`. `problems`, `state_problems`'
    output, appends one line per entry last. `hidden`, when positive, adds
    one line after the rows saying that many older runs were left out.
    `short_ids` (slug to short id) shows `slug [short]` on a row whose slug is in it.
    `waiting` (intake id to the ids it waits on) marks queued intake entries in the group lines. Queued entries
    whose `stage` is "draft" leave the queued line for an `intake drafts: N` line after it, shown only when
    there is one, so the queued line lists what the chair plans next. Each entry shows its `intake_tags`.
    `held`, rows of `{"id", "reason"}` and optionally the same entry keys, adds a `held:` heading after the groups
    with one `  id [tags]: reason` line each.
    `deferred` (target release to ids, `group_deferred`'s output) adds a `deferred to <version>:` heading per target
    after the held block, in the mapping's order, with one `  id` line each.
    `chair_line`, when non-empty, is the last line, after `gate:`.
    `events` (run id to its initiative's events) puts a `merged:` line under a row for each `tasks_merged` event.
    """
    ids = short_ids or {}
    by_run = events or {}
    lines = [
        line
        for row in rows
        for line in (_status_line(row, ids.get(row["id"])), *merged_event_lines(by_run.get(row["id"], [])))
    ]
    lines += [f"({hidden} older runs hidden; --all shows every run)"] if hidden > 0 else []
    if groups is not None:
        queue = groups["queued"]
        drafts = [e for e in queue if e.get("stage") == "draft"]
        shown = {
            "queued": [e for e in queue if e.get("stage") != "draft"],
            "drafts": drafts,
            "decomposed": groups["decomposed"],
            "landed": groups["landed"],
        }
        lines += [
            _intake_group_line(name, shown[name], waiting)
            for name in ("queued", "drafts", "decomposed", "landed")
            if name != "drafts" or drafts
        ]
    lines += (
        ["held:", *(f"  {h['id']}{intake_tags(h)}: {h['reason']}" if h.get("reason") else f"  {h['id']}{intake_tags(h)}" for h in held)]
        if held
        else []
    )
    lines += [line for target, names in (deferred or {}).items() for line in (f"deferred to {target}:", *(f"  {n}" for n in names))]
    lines += [f"problem: {p}" for p in problems or []]
    lines += [f"gate: {gate_level}"] if gate_level is not None else []
    lines += [chair_line] if chair_line else []
    return "\n".join(lines)


def render_drafts(rows: Sequence, age: Callable[[int | None], str]) -> str:
    """The Drafts block of `route status`: a header, then `id  proposer  age` per row (`.id`, `.proposed_by`,
    `.age_seconds`, as `draft_list.DraftRow`). Empty for no rows, so the caller adds nothing."""
    return "\n".join(["drafts:", *(f"  {r.id}  {r.proposed_by}  {age(r.age_seconds)}" for r in rows)]) if rows else ""


def status_entries(runs: list, summaries: dict) -> list:
    """The entries `status_rows` expects, assembled from what the CLI has
    already read: `runs` is `run_entries`' output (one `{"id", "pid",
    "alive", "started"}` per id with a pidfile), `summaries` is `id ->
    epic.summarize_log`'s output for whatever log text the CLI found for
    that id, `{}` where there was none. One entry per id in the union of
    both, sorted by id; an id with a log but no pidfile gets the pid-less
    shape `run_entries` would have given it: `pid` `None`, `alive`
    `False`, `started` `None`.
    """
    by_id = {row["id"]: row for row in runs}
    no_pidfile = {"pid": None, "alive": False, "started": None}
    return [
        {"id": run_id, **by_id.get(run_id, no_pidfile), **summaries.get(run_id, {})}
        for run_id in sorted(set(by_id) | set(summaries))
    ]


def child_env(
    environ: dict, *, harness_bin: str = "", repo: str = "", trace_dir: str = "", chair_model: str = "",
) -> dict:
    """The harness child's environment, spec §4: `environ` with
    `<repo>/.venv/bin` and `harness_bin` (the folder of the harness python) prepended to `PATH`
    when given, in that order. Every other key is untouched; `environ`
    itself is never mutated. When `trace_dir` is given, `AGENT_GRAPHS_TRACE_DIR`
    is set to it, replacing any value already in `environ`; when omitted, the
    key is left exactly as it was. A non-empty `chair_model` sets
    `COXSWAIN_CHAIR_MODEL`; an empty one leaves any pre-set value alone.
    """
    env = dict(environ)
    prefixes = [p for p in (f"{repo}/.venv/bin" if repo else "",
                             harness_bin) if p]
    if prefixes:
        rest = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(prefixes + ([rest] if rest else []))
    if trace_dir:
        env["AGENT_GRAPHS_TRACE_DIR"] = trace_dir
    if chair_model:
        env["COXSWAIN_CHAIR_MODEL"] = chair_model
    return env


def _unquote(raw: str) -> str:
    """Reverse `_yaml_scalar`'s double-quoted branch: `raw` is the quoted
    scalar including its outer quotes. Undo in the opposite order to the
    encode (quotes first, then backslashes) — encoding doubles backslashes
    before it escapes quotes, so decoding must collapse the quote escape
    before the backslash escape or a literal `\\"` collapses to the wrong
    character.
    """
    inner = raw[1:-1]
    quotes_undone = inner.replace('\\"', '"')
    return quotes_undone.replace("\\\\", "\\")


def _parse_list(raw: str) -> list:
    """Reverse the flat-list branch: `raw` is `[...]`, comma-separated bare
    items, `[]` for empty. Not a YAML parser — no quoted or nested items.
    """
    inner = raw[1:-1].strip()
    return [] if inner == "" else [item.strip() for item in inner.split(",")]


def _block_list(header_lines: list, start: int) -> tuple:
    """Items under a block-list key, from `header_lines[start]` to the first line
    that is neither indented nor a `- item` line, or the end of `header_lines`:
    `(items, next_index)`. Items may be indented (`  - x`, this module's own
    writer) or at column zero (`- x`, yaml.safe_dump, which the store's export uses
    through `queue_rows.render_item`); a quote yaml adds around an item is removed.
    Reading only the indented form made every exported ticket's needs and surfaces
    parse as [] (2026-10-06), so the chair treated waiting tasks as buildable.
    """
    items = []
    index = start
    while index < len(header_lines):
        line = header_lines[index]
        if not (line[:1].isspace() or line.startswith("- ") or line.rstrip() == "-"):
            break
        items.append(_unquote_item(line.strip()[2:].strip()))
        index += 1
    return items, index


def _unquote_item(item: str) -> str:
    """A yaml-quoted scalar back to its text: '...' with '' for a quote inside, or "..."; anything else unchanged."""
    if len(item) >= 2 and item[0] == item[-1] == "'":
        return item[1:-1].replace("''", "'")
    if len(item) >= 2 and item[0] == item[-1] == '"':
        return item[1:-1].replace('\\"', '"')
    return item


def _parse_field(line: str):
    """One `key: value` header line back to `(key, value)`, dispatching on
    the value's first character the same three ways `_yaml_scalar` and the
    list literals write them. A line with no `": "` separator — a stray
    comment, a block-list item, a hand edit — is not a line this module
    ever wrote, so it is not a field: `None` tells the caller to drop it
    rather than raise, which is what "never raises" requires of every line
    in the fence, not just the well-formed ones.
    """
    parts = line.split(": ", 1)
    if len(parts) != 2:
        return None
    key, raw = parts
    if raw.startswith('"'):
        return key, _unquote(raw)
    if raw.startswith("["):
        return key, _parse_list(raw)
    if raw.startswith("'"):
        return key, _unquote_item(raw)
    return key, raw


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """The inverse of `_frontmatter`: `(fields, body)` from `---`-delimited
    text of the shape `_frontmatter` writes. `text` with no leading `---`
    line, or with an opening `---` and no closing one, comes back as
    `({}, text)` unchanged — this never raises. This round-trips only the
    three value shapes `_yaml_scalar` and the list literals produce; it is
    not a general YAML parser and does not try to be.
    """
    opening = "---\n"
    if not text.startswith(opening):
        return {}, text
    after_opening = text[len(opening):]
    closing = "\n---\n"
    close_index = after_opening.find(closing)
    if close_index == -1:
        return {}, text
    header_block = after_opening[:close_index]
    after_closing = after_opening[close_index + len(closing):]
    header_lines = [line for line in header_block.split("\n") if line]
    fields = {}
    index = 0
    while index < len(header_lines):
        line = header_lines[index]
        field = _parse_field(line)
        if field is None and line.endswith(":"):
            items, index = _block_list(header_lines, index + 1)
            fields[line[:-1]] = items
            continue
        if field is not None:
            fields[field[0]] = field[1]
        index += 1
    with_leading_blank_stripped = (
        after_closing[1:] if after_closing.startswith("\n") else after_closing
    )
    body = (
        with_leading_blank_stripped[:-1]
        if with_leading_blank_stripped.endswith("\n")
        else with_leading_blank_stripped
    )
    return fields, body


def _after_list(value: object) -> list:
    """A front-matter `after` as a list of ids: a list stays, a bare scalar is one id, absent or empty is none."""
    if isinstance(value, list):
        return list(value)
    return [str(value)] if value else []


def _intake_entry(filename: str, text: str) -> dict[str, Any]:
    fields, body = parse_frontmatter(text)
    stem = os.path.splitext(os.path.basename(filename))[0]
    entry_id = fields.get("id", stem)
    body_lines = [line.strip() for line in body.split("\n") if line.strip()]
    first_body_line = body_lines[0] if body_lines else entry_id
    return {
        "id": entry_id,
        "title": fields.get("title", first_body_line),
        "initiative": fields.get("initiative"),
        "done": filename.startswith("done/"),
        "path": f"intake/{os.path.basename(filename)}",
        "after": _after_list(fields.get("after")),
        "held": intake_queue.is_held(fields),
        "stage": intake_queue.stage(fields),
        "priority": _int_or_zero(fields.get("priority")),
        "epic": str(fields.get("epic") or ""),
        "target_release": str(fields.get("target_release") or ""),
        "repo": str(fields.get("repo") or ""),
    }


def intake_entries(files: dict[str, str]) -> list[dict[str, Any]]:
    """Rows for the intake queue, the `intake` input `render_context` and
    `context_document` already accept: one `{"id", "title", "initiative",
    "done", "path", "after", "held", "stage", "priority", "epic", "target_release", "repo"}` per file in `files`
    (filename -> text, as the caller listed the intake dir), sorted by filename. `epic`, `target_release` and `repo`
    are strings, empty when the frontmatter has none. `held` and `stage` come from `intake_queue`, the predicate the
    row readers use: `stage` is "draft" or "ready", and `held` is true for any draft. A filename under a
    `consumed/` prefix has already been handled and is skipped. `id` comes
    from frontmatter's `id` field, or the filename's stem when frontmatter
    has none; `title` comes from frontmatter's `title` field, or the first
    non-empty body line, or `id` when the body is empty too.
    """
    return [
        _intake_entry(filename, files[filename])
        for filename in sorted(files)
        if not filename.startswith("consumed/")
    ]


# `quarantined` is a store state since graphs #699: a quarantine on a lane host reaches the store. It is neither
# ready nor done, so a quarantined task's siblings still show and its dependents wait.
STATES = frozenset({"todo", "ready", "in_progress", "blocked", "done", "dropped", "approved", "merged", "quarantined"})
TERMINAL = frozenset({"done", "dropped"})


def is_merged(row: Mapping[str, object]) -> bool:
    """A store row's merge flag, `extra["merged"]`: set on an `approved` task once its PR has merged."""
    extra = row.get("extra")
    return isinstance(extra, Mapping) and bool(extra.get("merged"))


def _merged_key(row: Mapping) -> dict:
    return {"merged": True} if is_merged(row) else {}


def is_awaiting_merge(item: Mapping) -> bool:
    """`approved` and not yet merged; a merged task is past the wait, whatever the store state still says."""
    return item["state"] == "approved" and not item.get("merged")


def is_met_need(item: Mapping[str, object], *, land_at: str = DEFAULT_LAND_AT) -> bool:
    """A need in the same initiative is met per `core.landing.need_met`: `done`, `dropped`, or `approved` that is merged or above `land_at` task."""
    state = item["state"]
    return _need_met(land_at, state if isinstance(state, str) else None, bool(item.get("merged")))


def state_word(item: Mapping[str, object]) -> str:
    """The word a status line shows: `merged` for an approved item carrying the merge flag, else its state."""
    return "merged" if item["state"] == "approved" and item.get("merged") else str(item["state"])


def initiative_states(ids: Sequence[str], items: Sequence[Mapping[str, object]]) -> dict[str, bool]:
    """`id -> True` when it has at least one item and every item naming it is `"done"` or `"dropped"`; an id with no items is not done."""
    own = {i: [item for item in items if item["initiative"] == i] for i in ids}
    return {i: bool(own[i]) and all(item["state"] in TERMINAL for item in own[i]) for i in ids}


def intake_groups(intake: list[dict[str, Any]], initiatives: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """queued has no `initiative`; decomposed names one not yet `done`, or predates the field but is cited by some initiative's text; landed names one `done`, or predates the field and is uncited. An intake named by an initiative's `intake` field is decomposed, or landed when every such initiative is done."""
    by_id = {i["id"]: i for i in initiatives}
    naming = intake_index(initiatives)
    grouped = [(intake_group(entry, by_id, initiatives, naming), entry) for entry in intake]
    return {name: [entry for group, entry in grouped if group == name] for name in ("queued", "decomposed", "landed")}


def _run_entry(run_id: str, pid_text: str, alive: dict, started: dict, heartbeats: dict, fenced: Mapping[str, bool]) -> dict:
    stripped = pid_text.strip()
    pid = int(stripped) if stripped.isdigit() else None
    return {
        "id": run_id,
        "pid": pid,
        "alive": alive.get(run_id, False) if pid is not None else False,
        "fenced": bool(fenced.get(run_id, False)) if pid is not None else False,
        "started": started.get(run_id),
        "heartbeat": heartbeats.get(run_id),
        "host": None,
        "remote": False,
    }


def run_entries(pids: dict, alive: dict, started: dict, heartbeats: dict | None = None, fenced: Mapping[str, bool] | None = None) -> list:
    """Rows for the runs list, the `runs` input `render_context` and
    `context_document` already accept: one `{"id", "pid", "alive",
    "started", "heartbeat"}` per run id in `pids` (pidfile text the caller
    already read), sorted by id. `heartbeats` maps a run id to its lease's
    `heartbeat_at`, only for runs that hold their lease; any other run gets
    `heartbeat` `None`. A pidfile whose text is not a plain integer — a partial
    write, a stray hand edit — is treated as unreadable: `pid` comes back
    `None` and `alive` comes back `False` regardless of what the caller
    passed in `alive` for that id. `fenced` maps a run id to whether its process
    answers without a held lease; it is carried on the entry as a `fenced` flag, `False` by default.
    """
    return [_run_entry(run_id, pids[run_id], alive, started, heartbeats or {}, fenced or {}) for run_id in sorted(pids)]


def remote_lane_entries(lanes: Sequence) -> list:
    """Run entries for lanes no local pidfile names, in input order: `pid` None, `alive` True, `remote` True. The caller passes lanes already filtered by `remote_lanes`; nothing is deduplicated here."""
    return [
        {
            "id": lane.run,
            "pid": None,
            "alive": True,
            "started": lane.launched_at,
            "heartbeat": lane.heartbeat_at,
            "host": lane.host,
            "remote": True,
        }
        for lane in lanes
    ]


def _ready_unblocked(item: Mapping, done_ids: set) -> bool:
    return item["state"] == "ready" and all(need in done_ids for need in item["needs"])


def _blockers_of(item: Mapping, own_items: Sequence) -> list[Mapping]:
    """`blocked` siblings a task sits behind: one it names in `needs`, or one
    in an earlier phase. A held item is not done, so work behind it is not ready.
    """
    return [
        other
        for other in own_items
        if other["state"] == "blocked" and (other["id"] in item["needs"] or other["phase"] < item["phase"])
    ]


def launch_blockers(items: list) -> list[str]:
    """Sorted `file` of every `blocked` item, in one initiative's `items`,
    that a `ready` task sits behind (see `_blockers_of`). A launch would
    run that held item again, so `route launch` refuses on a non-empty result.
    """
    return sorted({
        blocker["file"]
        for item in items
        if item["state"] == "ready"
        for blocker in _blockers_of(item, items)
    })


def waiting_lines(
    items: list, remedies: Mapping[str, str], initiative: str, *, land_at: str = DEFAULT_LAND_AT
) -> list[str]:
    """For each `ready` task of `initiative` with a need not met, `<task> waits on <dep> (<state>)`; a same-initiative need is met per `is_met_need`, a foreign one only by done or dropped;
    an `approved` dep with an entry in `remedies` adds that remedy line after it. A need in another initiative
    is looked up by id, and one that names no item reads `missing`."""
    states = {
        **{item["id"]: state_word(item) for item in items if item["initiative"] != initiative},
        **{item["id"]: state_word(item) for item in items if item["initiative"] == initiative},
    }
    met = {
        **{item["id"]: item["state"] in TERMINAL for item in items if item["initiative"] != initiative},
        **{
            item["id"]: _met_by_launch(land_at, item["state"], bool(item.get("merged")))
            for item in items
            if item["initiative"] == initiative
        },
    }
    return [
        line
        for item in items
        if item["initiative"] == initiative and item["state"] == "ready"
        for need in item["needs"]
        if not met.get(need, False)
        for line in [
            f"{item['id']} waits on {need} ({states.get(need, 'missing')})",
            *([remedies[need]] if states.get(need) == "approved" and need in remedies else []),
        ]
    ]


def state_problems(items: list) -> list[str]:
    """One `"<initiative>: <file>: unknown state '<value>'"` line per item
    whose `state` is not in `STATES` — a typo, or a value some other tool
    wrote — sorted by initiative then file so the printed order is stable.
    """
    bad = sorted(
        (item for item in items if item["state"] not in STATES),
        key=lambda item: (item["initiative"], item["file"]),
    )
    return [f"{item['initiative']}: {item['file']}: unknown state {item['state']!r}" for item in bad]


def with_store_states(items: list[dict[str, object]], rows: Sequence[Mapping[str, object]], mode: str) -> list[dict[str, object]]:
    """Under mode "store", each item's `state` is the store row's; an item with no row keeps its file state. Any other mode returns `items` as given."""
    if mode != "store":
        return items
    index = run_store.rows_by_task(rows)
    found = [(item, index.get((item["initiative"], item["id"]), {})) for item in items]
    return [item if row.get("state") is None else {**item, "state": row["state"], **_merged_key(row)} for item, row in found]


def _initiative_summary(initiative_id: str, own_items: Sequence[Mapping[str, object]], land_at: str = DEFAULT_LAND_AT) -> dict[str, object] | None:
    """`None` when nothing to report. An item outside `STATES` makes the
    whole initiative unlaunchable (`ready` 0) until fixed, regardless of
    what its siblings are. Otherwise an `approved` item counts as neither
    ready nor done and is surfaced as `awaiting_merge`, unless it carries the
    merge flag: then it is done for the needs of its own initiative. Under
    `land_at` ticket or level an unmerged approved need is met too.
    """
    if any(item["state"] not in STATES for item in own_items):
        return {"id": initiative_id, "phase": None, "ready": 0}
    awaiting_merge = sum(1 for item in own_items if is_awaiting_merge(item))
    done_ids = {item["id"] for item in own_items if is_met_need(item, land_at=land_at)}

    def counts_ready(item: Mapping) -> bool:
        return _ready_unblocked(item, done_ids) and not _blockers_of(item, own_items)

    ready_phases = sorted({str(item["phase"]) for item in own_items if counts_ready(item)})
    if not ready_phases:
        if not awaiting_merge:
            return None
        return {"id": initiative_id, "phase": None, "ready": 0, "awaiting_merge": awaiting_merge}
    phase = ready_phases[0]
    ready_count = sum(
        1
        for item in own_items
        if item["phase"] == phase and counts_ready(item)
    )
    summary = {"id": initiative_id, "phase": phase, "ready": ready_count}
    if awaiting_merge:
        summary["awaiting_merge"] = awaiting_merge
    return summary


def initiative_summaries(items: Sequence[Mapping[str, object]], *, land_at: str = DEFAULT_LAND_AT) -> list[dict[str, object]]:
    """One row per initiative with a task ready to run, the `initiatives`
    input `render_context` and `context_document` already accept: `items`
    are work items with at least `id`, `initiative`, `phase`, `state`,
    `needs`, each parsed with `parse_frontmatter` by the caller. Each row is
    `{"id": <initiative>, "phase": <sorted-first phase holding a ready,
    unblocked task>, "ready": <count of such tasks in that phase>}`.
    "Unblocked" means every id in `needs` names a task, anywhere in the same
    initiative, whose state meets the need under `land_at` (`is_met_need`). An initiative with no ready,
    unblocked task is omitted. Sorted by initiative id.
    """
    initiative_ids = sorted({str(item["initiative"]) for item in items})
    summaries = [
        _initiative_summary(
            initiative_id,
            [item for item in items if item["initiative"] == initiative_id],
            land_at,
        )
        for initiative_id in initiative_ids
    ]
    return [summary for summary in summaries if summary is not None]


def parse_pid(text: str) -> int | None:
    """A pidfile's text to a pid, or `None` when it isn't one: stripped,
    digit-only text becomes an int; anything else — empty, garbage, a
    partial write — is not a pid this module will hand to `os.kill`.
    """
    stripped = text.strip()
    return int(stripped) if stripped.isdigit() else None


def latest_groups_file(names: list) -> str | None:
    """work-shape.md §5: the newest `plans/intake-groups/<date>.md`
    filename, picked by lexicographic max (ISO dates sort naturally); `None`
    when `names` is empty.
    """
    return max(names) if names else None


def work_item(fields: Mapping[str, object], *, initiative: str, phase_dir: str, stem: str) -> dict[str, object]:
    """A work item row for `initiative_summaries`, filled in from the path
    the caller read it from wherever frontmatter left a gap: `id` falls
    back to `stem`, `phase` to `phase_dir`, `state` to `"todo"`, `needs` to
    `[]`. `initiative` always comes from the argument — the directory name
    is the initiative, never a frontmatter claim to the contrary. `file` is
    `<phase_dir>/<stem>.md`, cited by `state_problems` when `state` is bad.
    """
    return {
        **fields,
        "id": fields.get("id", stem),
        "initiative": initiative,
        "phase": fields.get("phase", phase_dir),
        "state": fields.get("state", "todo"),
        "needs": fields.get("needs", []),
        "file": f"{phase_dir}/{stem}.md",
    }
