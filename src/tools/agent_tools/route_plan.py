"""The routing core, launching: ceilings, the context document, the launch claim, filing and priorities."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

from agent_tools import chair, decompose_empty, intake_queue, open_queue_priority, queue_rows, run_store
from agent_tools.jsonl_log import parse_log
from agent_tools.pacing import Assessment
from agent_tools.route_lint import _with_header_fields
from agent_tools.route_profile import _PROFILE_FIELDS, _frontmatter
from agent_tools.route_status import (
    DEFAULT_LAND_AT,
    _int_or_zero,
    _merged_key,
    _met_by_launch,
    initiative_states,
    intake_groups,
    is_awaiting_merge,
    is_merged,
    parse_frontmatter,
)

_TIER_LADDER = ("cheap", "standard", "deep")
_EFFORT_LADDER = ("low", "high")


def _ceiling_error(kind: str, ceiling: str, top) -> str:
    return f"routing: --{kind}-ceiling {ceiling} exceeds this profile's own {kind} ceiling of {top}"


def _profile_top(values, ladder: tuple) -> str | None:
    """Pure: the highest rung of `ladder` among `values`, or None when no value
    is on the ladder at all. A profile that names nothing usable has no
    ceiling to tighten from, and a ceiling that fails open on missing or
    off-ladder data is not a ceiling."""
    on_ladder = [v for v in values if v in ladder]
    return max(on_ladder, key=ladder.index) if on_ladder else None


def _tighten(kind: str, top: str | None, requested: str | None, ladder: tuple) -> tuple[str | None, str | None]:
    """Pure: `(ceiling, error)` for one axis. `requested=None` is a no-op."""
    if requested is None:
        return None, None
    if top is None:
        return None, _ceiling_error(kind, requested, "unset")
    if ladder.index(requested) > ladder.index(top):
        return None, _ceiling_error(kind, requested, top)
    return requested, None


def overlay(profile: dict, tier_ceiling: str | None, effort_ceiling: str | None):
    """Pure: a same-run, tighten-only overlay on a provider profile.

    A provider profile (`providers/claude-code.yaml`) maps each TIER to a model
    under `tiers` (`cheap: haiku, standard: sonnet, deep: opus`) and each tier
    to a thinking effort under `effort` (`cheap: low, standard: high, deep:
    high`). The profile's own tier ceiling is the highest tier it maps; its
    effort ceiling is the highest effort it names. The profile
    is an UPPER LIMIT — a launch may pick at or below it, never above.

    `--tier-ceiling T` rewrites every tier above T to run T's model and T's
    effort, so `deep` on a `standard` ceiling runs sonnet at standard's effort.
    `--effort-ceiling E` clamps every tier's effort to E. A request above the
    profile's own top, or on an axis the profile does not usably declare,
    returns a one-line refusal string instead of a dict; the edge turns that
    into exit 2 before any file is written. No flags: `profile` comes back
    unchanged. Budgets are untouched — a ceiling is about model and effort.
    """
    tiers = profile.get("tiers") if isinstance(profile.get("tiers"), dict) else {}
    efforts = profile.get("effort") if isinstance(profile.get("effort"), dict) else {}
    tier_top = _profile_top(tiers.keys(), _TIER_LADDER)
    effort_top = _profile_top(efforts.values(), _EFFORT_LADDER)

    tier_cap, tier_error = _tighten("tier", tier_top, tier_ceiling, _TIER_LADDER)
    if tier_error is not None:
        return tier_error
    effort_cap, effort_error = _tighten("effort", effort_top, effort_ceiling, _EFFORT_LADDER)
    if effort_error is not None:
        return effort_error
    if tier_cap is None and effort_cap is None:
        return profile

    def capped_tier(tier: str) -> str:
        above = tier in _TIER_LADDER and tier_cap is not None and _TIER_LADDER.index(tier) > _TIER_LADDER.index(tier_cap)
        return tier_cap if above else tier

    def capped_effort(value):
        return effort_cap if effort_cap is not None and value in _EFFORT_LADDER and _EFFORT_LADDER.index(value) > _EFFORT_LADDER.index(effort_cap) else value

    new_tiers = {tier: tiers[capped_tier(tier)] for tier in tiers}
    new_efforts = {tier: capped_effort(efforts.get(capped_tier(tier), value)) for tier, value in efforts.items()}
    return {**profile, **({"tiers": new_tiers} if tiers else {}), **({"effort": new_efforts} if efforts else {})}


def launch_gate(assessment: Assessment, force: bool) -> tuple[int | None, list[str]]:
    """Pure: what `route launch` does with one usage `Assessment`, computed
    once via the gatherer. `stop` refuses before anything starts unless
    `force` overrides it; `hold` only narrates the reason and lets the launch
    continue; `go_degraded` does the same as an advisory, since the epic
    launch path applies no ceiling; `go` is silent. This function decides —
    `_route_launch` only prints the returned lines and returns the code.
    """
    if assessment.verdict == "stop" and not force:
        return 2, [f"routing: usage stop: {assessment.reason}"]
    if assessment.verdict == "stop":
        return None, [f"routing: usage stop overridden by --force: {assessment.reason}"]
    if assessment.verdict == "go_degraded":
        return None, [f"routing: advisory: usage go_degraded: {assessment.reason}"]
    if assessment.verdict == "hold":
        return None, [f"routing: usage hold: {assessment.reason}"]
    return None, []


def _alive_runs(runs: list) -> list:
    """The subset of `runs` still in flight — the docket's `lanes:` count
    and roster both come from this, not from every entry with a pidfile."""
    return [r for r in runs if r["alive"]]


def _describe_initiative(i: dict) -> str:
    bits = [f'{i["ready"]} tasks ready in phase {i["phase"]}'] if i["ready"] else []
    if i.get("awaiting_merge"):
        bits.append(f'{i["awaiting_merge"]} awaiting merge')
    return f'{i["id"]} ({", ".join(bits) or "unlaunchable"})'


def _local_hhmm(started) -> str:
    """`started` (ISO, UTC) as HH:MM in the machine's local zone; text that is not an ISO time is shown as it came."""
    try:
        return datetime.datetime.fromisoformat(started).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return str(started)


def _heartbeat_age(heartbeat_at, now) -> str | None:
    """`Ns` under 60 s, else `Nm`, between two `YYYY-MM-DDTHH:MM:SSZ` times; None when either is missing or unreadable."""
    try:
        seconds = int((datetime.datetime.fromisoformat(now) - datetime.datetime.fromisoformat(heartbeat_at)).total_seconds())
    except (TypeError, ValueError):
        return None
    return f"{max(seconds, 0)}s" if seconds < 60 else f"{seconds // 60}m"


def _lane(run: dict, now: str | None) -> str:
    age = _heartbeat_age(run.get("heartbeat"), now)
    since = _local_hhmm(run["started"])
    if run.get("remote"):
        where = f'on {run.get("host") or "another machine"}'
        return f'{run["id"]} ({where}, since {since})' if age is None else f'{run["id"]} ({where}, since {since}, heartbeat {age} ago)'
    return f'{run["id"]} (pid {run["pid"]}, since {since})' if age is None else f'{run["id"]} (since {since}, heartbeat {age} ago)'


def efficiency_line(spend: float | None, completions: int, first_try_rate: float | None, measured: int | None = None) -> str | None:
    """The docket's `efficiency:` line; None with no store. `first_try_rate` is a 0..1 share of `measured` tasks, None when none were measured."""
    if spend is None:
        return None
    if completions == 0:
        return f"efficiency: today ${spend:.2f}, nothing completed yet"
    head = f"efficiency: today ${spend:.2f} for {completions} completions (${spend / completions:.2f} per completion)"
    if first_try_rate is None:
        return f"{head}, first-try unknown"
    if measured is not None and measured < completions:
        return f"{head}, first-try {first_try_rate * 100:.0f}% of {measured} measured"
    return f"{head}, first-try {first_try_rate * 100:.0f}%"


def _is_landed_today(row: dict, today: str) -> bool:
    steps = row.get("steps_reached")
    return row.get("exit") == 0 and isinstance(steps, list) and "mark_done" in steps and str(row.get("ts", "")).startswith(today)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def landed_today(land_log: str, today: str) -> list[tuple[str | None, str | None]]:
    """`(run, task)` per task landed on `today` (exit 0, `mark_done` reached, `ts` that day), once per named task; unparseable lines skipped."""
    rows = [
        (_str_or_none(r.get("run")), _str_or_none(r.get("task"))) for r in parse_log(land_log).rows
        if _is_landed_today(r, today)
    ]
    return [row for i, row in enumerate(rows) if row[1] is None or row not in rows[:i]]


def task_owner(task_id: str, run: str | None, tickets: Collection[str]) -> str | None:
    """The one of `tickets` landed in `run` that store `task_id` names, bare or as a `<run>:<phase>:<head>` composite; None when none or several could."""
    if task_id in tickets:
        return task_id
    parts = task_id.split(":", 2)
    owners = [t for t in tickets if len(parts) == 3 and parts[0] == run and parts[2] != "" and t.startswith(parts[2])]
    return owners[0] if len(owners) == 1 else None


def first_try(landed: list[tuple[str | None, str | None]], build_counts: dict[str, int]) -> tuple[float | None, int]:
    """(share of measured tasks with one build call, tasks measured); measured tasks have build calls, and the share is None when none do."""
    tickets = {run: {t for r, t in landed if r == run and t is not None} for run, _ in landed}
    counts = [
        sum(n for task_id, n in build_counts.items() if task_owner(task_id, run, tickets[run]) == ticket)
        for run, ticket in landed if ticket is not None
    ]
    measured = [n for n in counts if n > 0]
    return (sum(1 for n in measured if n == 1) / len(measured) if measured else None), len(measured)


def render_context(profile_or_none, intake: dict, runs, initiatives, problems: list | None = None,
                   gate_level: str | None = None, now: str | None = None, efficiency: str | None = None) -> str:
    """The human-readable layout `agent-tools route context` prints, spec
    §2. `profile_or_none` is a parsed profile dict or None; `intake` is an
    `intake_groups` result; `runs` and `initiatives` are already-gathered
    lists — this function lists a directory or reads a pidfile for none of
    it, that is the CLI's job. `problems`, `state_problems`' output, prints
    one line per entry after the ready line.
    """
    if profile_or_none is None:
        return (
            "routing: no profile at ~/.config/agent-tools/profile.yaml; "
            "the harness is not configured on this machine"
        )
    team = profile_or_none.get("team", "")
    lines = [
        f"routing: team {team}; work requests go through the route-work "
        "skill, questions stay inline",
        f"intake: {len(intake['queued'])} queued, {len(intake['decomposed'])} decomposed, "
        f"{len(intake['landed'])} landed",
    ]
    live = _alive_runs(runs)
    if live:
        described = ", ".join(_lane(r, now) for r in live)
        lines.append(f"lanes: {len(live)} busy — {described}")
    else:
        lines.append("lanes: all clear")
    lines += [efficiency] if efficiency is not None else []
    if initiatives:
        described = ", ".join(_describe_initiative(i) for i in initiatives)
        lines.append(f"ready: {described}")
    else:
        lines.append("ready: none")
    lines += [f"problem: {p}" for p in problems or []]
    lines += [f"gate: {gate_level}"] if gate_level is not None else []
    return "\n".join(lines)


def context_document(profile_or_none, intake, runs, initiatives, problems: list | None = None) -> dict:
    """The `--json` shape for `agent-tools route context`, spec §2: the
    same facts as `render_context`, keyed on exactly the profile fields
    `parse_profile` produces so the no-profile case (every profile field
    None) renders cleanly rather than omitting keys a caller would have
    to guard for.
    """
    doc = {
        field: (profile_or_none.get(field) if profile_or_none else None)
        for field in _PROFILE_FIELDS
    }
    doc["intake"] = intake
    doc["runs"] = runs
    doc["live"] = len(_alive_runs(runs))
    doc["initiatives"] = initiatives
    doc["problems"] = problems or []
    return doc


def _task_items(rows: Sequence[Mapping]) -> list[dict]:
    """Task rows from the store's queue, in the item shape `initiative_summaries` takes. Mirrors `chair_read_docket._item_of`."""
    return [
        {"id": r["task_id"], "initiative": r["initiative"], "phase": r["phase"], "state": r["state"], "needs": list(r["needs"]),
         **_merged_key(r)}
        for r in rows
        if r["kind"] == "task"
    ]


def _intake_entry_from_row(row: Mapping) -> dict:
    """One queue row (kind `intake`) in the entry shape `intake_groups`/`intake_entries` already accept. `path`
    names the file's stem the same way for a queued or a done row — `_intake_entry` never puts `done/` in it."""
    body_lines = [line.strip() for line in (row.get("body") or "").split("\n") if line.strip()]
    entry_id = row["extra"].get("id", row["task_id"])
    return {
        "id": entry_id,
        "title": row["title"] or (body_lines[0] if body_lines else entry_id),
        "initiative": row["extra"].get("initiative"),
        "done": row["state"] == "done",
        "path": f"intake/{row['task_id']}.md",
    }


def _phase_of(own_items_by_id: Mapping[str, dict], ready_tasks: Sequence[Mapping]) -> str | None:
    return next((own_items_by_id[t["id"]]["phase"] for t in ready_tasks if t["id"] in own_items_by_id), None)


def _initiative_from_docket(entry: Mapping, items: list) -> dict | None:
    """One `initiative_summaries`-shaped row from one `chair_read_docket.docket_from_rows` entry: `ready` and
    `phase` come from `entry["ready_tasks"]`, already less any task a live claim holds, so a claimed task is not
    offered again. `None` when the initiative has nothing to report, matching `_initiative_summary`."""
    own = [i for i in items if i["initiative"] == entry["id"]]
    awaiting_merge = sum(1 for item in own if is_awaiting_merge(item))
    ready = len(entry["ready_tasks"])
    if not ready and not awaiting_merge:
        return None
    summary = {
        "id": entry["id"],
        "phase": _phase_of({i["id"]: i for i in own}, entry["ready_tasks"]) if ready else None,
        "ready": ready,
    }
    if awaiting_merge:
        summary["awaiting_merge"] = awaiting_merge
    return summary


def context_from_rows(rows: Sequence[Mapping], now: str) -> tuple[dict, list[dict]]:
    """`(intake, initiatives)` in exactly the shapes `context_document`/`render_context` already accept, built from
    `run_store.read_queue` rows instead of the workspace's files. `initiatives` goes through
    `chair_read_docket.docket_from_rows` so a task a live claim holds does not count as ready; `intake`'s `queued`
    group comes from `chair_read_intake.intake_from_rows`, the same rows the chair itself would pick up next.

    unknown: an intake entry that predates the `initiative:` field, matched only by an initiative.md body citing its
    path (`intake_state.intake_group`'s `naming` fallback), cannot be reconstructed here — rows carry no initiative.md text.
    """
    # Deferred: chair_read_docket reads route.TERMINAL at import time, so a top-level import here would cycle.
    from agent_tools import chair_read_docket, chair_read_intake

    items = _task_items(rows)
    initiatives = [
        summary
        for summary in (_initiative_from_docket(entry, items) for entry in chair_read_docket.docket_from_rows(rows, now))
        if summary is not None
    ]
    entries = _intake_entries_from_rows(rows)
    ids = sorted({item["initiative"] for item in items})
    initiative_rows = [{"id": iid, "done": done} for iid, done in initiative_states(ids, items).items()]
    groups = intake_groups(entries, initiative_rows)
    groups["queued"] = [_intake_entry_from_row(r) for r in chair_read_intake.intake_from_rows(rows)]
    return groups, initiatives


def _intake_entries_from_rows(rows: Sequence[Mapping]) -> list[dict]:
    return [_intake_entry_from_row(r) for r in rows if r["kind"] == "intake"]


def context_rows(runs_dir: Path, now: str) -> tuple[dict, list[dict]] | None:
    """`context_from_rows` over the store's queue rows; `None` when `read_queue` comes back empty because the
    harness, table or store is unavailable, so the caller falls back to reading the workspace's files as today."""
    rows = run_store.read_queue(runs_dir)
    return context_from_rows(rows, now) if rows else None


def launch_claim(
    runs_dir: Path, initiative: str, task_id: str, holder: str, now: str, ttl_s: float = chair.DEFAULT_LEASE_TTL_SECONDS,
) -> tuple[dict | None, str | None]:
    """The claim taken on `(initiative, task_id)`, or `(None, <holder>)` naming who already holds it, or
    `(None, run_store.UNAVAILABLE)` when the store cannot say. `ttl_s` defaults to the chair's own lease TTL, so a
    launch claim outlives one heartbeat the same way the chair's lease does."""
    claim, reason = run_store.claim_outcome(runs_dir, initiative, task_id, holder, ttl_s, now)
    if claim is not None or reason != run_store.HELD:
        return claim, reason
    held = next(
        (r["holder"] for r in run_store.read_queue(runs_dir, initiative, kind="task") if r["task_id"] == task_id and r["holder"]),
        None,
    )
    return None, held or "another holder"


def launch_claim_gate(claim: dict | None, blocker: str | None) -> tuple[int | None, list[str]]:
    """Pure: what `route launch` does with one `launch_claim` outcome, mirroring `launch_gate`'s shape. A claim
    taken is silent; the store being unavailable is one warning and launch proceeds; anything else is `blocker`
    naming the holder already running it, refused with the same exit code `launch_gate` uses for a stop."""
    if claim is not None:
        return None, []
    if blocker == run_store.UNAVAILABLE:
        return None, ["routing: warning, the work-item store is unavailable; launching without a claim"]
    return 2, [f"routing: launch refused, {blocker} already holds this task"]


def is_chair_machine(lease_host: str, local_host: str) -> bool:
    """Pure: a lease held by another machine names that machine the chair; no lease host leaves this one the chair."""
    return lease_host in ("", local_host)


def should_export_board(is_chair: bool) -> bool:
    """Pure: only the chair machine exports the board, because only the chair commits the files it writes."""
    return is_chair


def run_under_claim[T](
    runs_dir: Path, initiative: str, task_id: str, holder: str, now: str, body: Callable[[], T],
    ttl_s: float = chair.DEFAULT_LEASE_TTL_SECONDS,
) -> tuple[int | None, list[str], T | None]:
    """`launch_claim` then `launch_claim_gate`; a refusal or an unavailable store runs no body. `body` failing
    before it returns releases the claim this call took, then re-raises unchanged — a release never masks the
    body's own exception. `now` is the caller's clock reading, passed in from the edge, never read here."""
    claim, blocker = launch_claim(runs_dir, initiative, task_id, holder, now, ttl_s)
    code, lines = launch_claim_gate(claim, blocker)
    if code is not None:
        return code, lines, None
    try:
        value = body()
    except BaseException:
        if claim is not None:
            run_store.release_row(runs_dir, initiative, task_id, holder)
        raise
    return None, lines, value


def _unrecorded_row_warning(path: Path, row: queue_rows.Row | None) -> str:
    """Pure: why `write_filed_item` is about to write `path` with no store row behind it yet."""
    if row is None:
        return f"routing: no store row for {path} (an unrecognized item shape); filing it without one"
    return f"routing: store unavailable, filing {path} without it"


def _filed_write_error(path: Path, stored: bool, exc: OSError) -> str:
    """Pure: the one line `write_filed_item` prints when the file write itself fails, naming which side —
    the store, the file, or both — is now out of step."""
    if stored:
        return f"routing: store recorded {path} but the file write failed: {exc}"
    return f"routing: store unavailable and the file write for {path} failed too: {exc}"


def write_filed_item(
    runs_dir: Path, path: Path, kind: str, path_parts: tuple[str, ...], text: str, priority: int | None = None
) -> int:
    """The store first, the file second, for one file `cox route file` is about to write. `queue_rows.parse_item`
    is the codec that turns `(kind, path_parts, text)` into the row; `run_store.upsert_row` is the seam that
    takes it. A store that cannot take it — down, or a shape the codec does not recognize — is one warning, not
    a refusal, and the file is written the same as today, so a machine without a harness still files the work;
    the next `route import` or chair export reconciles it. A file write that then fails is one printed line and
    a non-zero return either way, since a store row already taken makes the row the record a chair export
    recreates the file from, and a store that never took one leaves nothing else to fall back on. Returns 0 on
    the ordinary, no-surprise path. A `priority` is also sent as the row's top-level int `priority` key, beside
    the frontmatter copy `parse_item` leaves in `extra`, which alone does not reach the store's priority column.
    A written file is recorded as the workspace's export baseline, so the export never holds it as a hand edit."""
    from agent_tools import queue_export  # imported here: route loads on every cox start

    row = queue_rows.parse_item(kind, path_parts, text)
    sent = row if row is None or priority is None else {**row, "priority": priority}
    stored = sent is not None and run_store.upsert_row(runs_dir, sent)
    if not stored:
        print(_unrecorded_row_warning(path, row))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(_filed_write_error(path, stored, exc))
        return 1
    queue_export.record_written_paths(Path(runs_dir).parent, [path])
    return 0


_UNSTARTED_STATES = frozenset({"todo", "ready"})  # route_guard.EDITABLE_STATES; any other state is past ready


def select_intake_row(rows: Sequence[Mapping], intake_id: str) -> Mapping | None:
    """The intake row named by `intake_id`, matched on `task_id` or `extra["id"]` in any state, else None."""
    return next(
        (r for r in rows if r.get("kind") == "intake" and intake_id in (r.get("task_id"), (r.get("extra") or {}).get("id"))),
        None,
    )


def intake_input_from_row(row: Mapping) -> dict:
    """The plain data a decompose run takes in place of the intake file path."""
    return {"id": row["task_id"], "body": row["body"], "state": row["state"]}


def plan_writes(
    plan: Mapping, intake_row: Mapping, existing: Sequence[Mapping], lane_model: str = queue_rows.DEFAULT_LANE_MODEL,
) -> tuple[list[dict], str | None]:
    """`(rows, failure)`: the initiative row, ticket rows and stamped intake row to upsert, in that order.

    A plan with no tasks gives no rows and the `decompose_empty` reason. A row the store holds past ready
    is left out, so a rerun never resets it. `existing` is the store's rows for the plan's initiative.
    A non-zero intake priority is set on the initiative and task rows; the intake row keeps its own.
    `lane_model` is stamped on a new initiative row; one the store already holds keeps the model it was decomposed under.
    """
    outcome = decompose_empty.empty_outcome(len(plan["tasks"]), list(plan.get("lint", [])), False)
    if outcome["empty"]:
        return [], f"routing: {outcome['reason']}"
    started = {(r["kind"], r["task_id"]) for r in existing if r.get("state") not in _UNSTARTED_STATES}
    value = _row_priority(intake_row)
    inherit = {"initiative", "task"} if value else set()
    held = next((r for r in existing if r.get("kind") == "initiative" and r.get("task_id") == plan["id"]), None)
    model = lane_model if held is None else queue_rows.lane_model_of_row(held)
    planned = [
        {**r, "priority": value} if r["kind"] in inherit else r
        for r in queue_rows.plan_to_rows(plan, intake_row["task_id"], model)
    ]
    built = [*planned, queue_rows.intake_stamp(dict(intake_row), plan["id"])]
    return [r for r in built if (r["kind"], r["task_id"]) not in started], None


_TASK_FIELDS = ("id", "phase", "title", "body", "needs", "surfaces")


def plan_from_result(result: Mapping, initiative_id: str, title: str, body: str, *, repo: str = "") -> dict:
    """The plan `decompose_into_rows` wants, from a decompose graph result; no tasks gives an empty tasks list.

    `repo` is the intake's repo, carried as `plan["repo"]`; empty when the caller has none.
    """
    tasks = [
        {k: (list(t[k]) if k in ("needs", "surfaces") else t[k]) for k in _TASK_FIELDS if k in t}
        for t in result.get("tasks") or []
    ]
    return {
        "id": initiative_id,
        "title": title,
        "body": body,
        "phases": [dict(p) for p in result.get("phases") or []],
        "tasks": tasks,
        "lint": list(result.get("lint") or []),
        "repo": repo,
    }


def initiative_with_repo(text: str, repo: str) -> str:
    """`text`, an initiative.md, with its frontmatter `repo` line set to `repo`; an empty `repo` returns `text` as is."""
    opening, closing = "---\n", "\n---\n"
    close_index = text.find(closing, len(opening)) if text.startswith(opening) else -1
    if not repo:
        return text
    if close_index == -1:
        return _frontmatter([("repo", repo)], text)
    header_lines = text[len(opening):close_index].split("\n")
    header = "\n".join(_with_header_fields(header_lines, [("repo", repo)]))
    return f"{opening}{header}{text[close_index:]}"


def missing_repo_refusal(intake_name: str, fields: Mapping) -> str | None:
    """None when `fields` holds a non-empty string `repo`, else the one-line refusal naming the intake."""
    value = fields.get("repo")
    if isinstance(value, str) and value:
        return None
    return f"routing: intake {intake_name} has no repo: field"


def decompose_into_rows(
    runs_dir: Path, intake_id: str, run: Callable[[dict], Mapping], lane_model: str = queue_rows.DEFAULT_LANE_MODEL,
) -> tuple[int, list[str]]:
    """Edge for store mode: read the intake row, hand its body to `run`, write the plan it returns as rows.

    No workspace file is read or written. `lane_model` is `policy.dispatch.lane_model`, stamped by `plan_writes`.
    Returns `(exit code, lines to print)`.
    """
    intake = select_intake_row(run_store.read_queue(runs_dir, kind="intake"), intake_id)
    if intake is None:
        return 2, [f"routing: no intake row {intake_id}"]
    if intake_queue.stage(intake.get("extra") or {}) == "draft":
        return 2, [f"routing: intake {intake_id} is a draft; ready it with `cox route ready {intake_id}`"]
    plan = run(intake_input_from_row(intake))
    existing = run_store.read_queue(runs_dir, plan["id"]) if plan["tasks"] else []
    rows, failure = plan_writes(plan, intake, existing, lane_model)
    if failure is not None:
        return 1, [failure]
    for row in rows:
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            return 1, [f"routing: store refused {row['kind']} row {row['task_id']}: {detail}"]
    return 0, [f"routing: wrote {len(rows)} rows for {plan['id']}"]


def ready_tasks(task_rows: Sequence[Mapping], *, land_at: str = DEFAULT_LAND_AT) -> list[Mapping]:
    """The `ready` task rows whose every need is met under `land_at` (`core.landing.need_met`), in the order given."""
    done_ids = {r["task_id"] for r in task_rows if _met_by_launch(land_at, r.get("state"), is_merged(r))}
    return [r for r in task_rows if r.get("state") == "ready" and all(n in done_ids for n in r.get("needs") or [])]


def initiative_repo(initiative_row: Mapping, intake_rows: Sequence[Mapping]) -> str:
    """The repo an initiative's rows name: its own `extra.repo`, else the repo of the intake row it came from; "" for neither.

    Decompose's initiative row holds no repo: queue_rows.plan_to_rows writes id, intake and phases only. The idea's
    `repo:` stays in the intake row's `extra`, matched by the `intake/<id>.md` path or by the `initiative` its stamp records.
    """
    extra = initiative_row.get("extra") or {}
    intake_id = Path(str(extra.get("intake") or "")).stem
    linked = next(
        (
            r for r in intake_rows
            if (intake_id and r.get("task_id") == intake_id)
            or (r.get("extra") or {}).get("initiative") == initiative_row["task_id"]
        ),
        None,
    )
    linked_repo = str((linked.get("extra") or {}).get("repo") or "") if linked is not None else ""
    return str(extra.get("repo") or "") or linked_repo


def launch_input(
    initiative_row: Mapping, task_rows: Sequence[Mapping], repo: str, *, land_at: str = DEFAULT_LAND_AT
) -> dict:
    """The plain data an epic launch takes in place of `work/<id>/`: repo, phases, every ticket, and the ids ready now.

    The initiative row's `extra.land_at` wins over the `land_at` argument when present.
    """
    extra = initiative_row.get("extra") or {}
    return {
        "initiative": initiative_row["task_id"],
        "repo": repo,
        "phases": list(extra.get("phases") or []),
        "tasks": [
            {
                "id": r["task_id"], "phase": r["phase"], "state": r["state"], "needs": list(r.get("needs") or []),
                "surfaces": list(r.get("surfaces") or []), "body": r["body"],
            }
            for r in task_rows
        ],
        "ready": [r["task_id"] for r in ready_tasks(task_rows, land_at=extra.get("land_at") or land_at)],
    }


def launch_from_rows(
    runs_dir: Path, initiative_id: str, repo_flag: str | None = None, *, land_at: str = DEFAULT_LAND_AT
) -> tuple[int, list[str], dict | None]:
    """Edge for store mode: `(exit code, lines to print, launch input)` read from work_items rows only.

    A missing initiative row, or a repo named by neither the flag nor the rows, gives code 2 and no input.
    `read_queue` returns [] for an unreachable store too, so the missing-row message names both causes.
    No workspace file is opened.
    """
    rows = run_store.read_queue(runs_dir, initiative_id)
    head = next((r for r in rows if r.get("kind") == "initiative"), None)
    if head is None:
        return 2, [f"routing: no initiative row {initiative_id} in the store, or the store is unavailable"], None
    repo = repo_flag or initiative_repo(head, run_store.read_queue(runs_dir, kind="intake"))
    if not repo:
        return 2, [f"routing: no --repo given and no repo in the rows for {initiative_id} or its intake"], None
    return 0, [], launch_input(head, [r for r in rows if r.get("kind") == "task"], repo, land_at=land_at)


def next_priority(current: int, flag: int | str) -> int:
    """Pure: an int `flag` is `--set N` and wins; "up" and "down" step `current` by one. No clamping, so a
    negative priority is legal and sorts below the default 0."""
    if isinstance(flag, int):
        return flag
    if flag == "up":
        return current + 1
    if flag == "down":
        return current - 1
    raise ValueError(f"unknown priority flag: {flag!r}")


def current_priority(texts: Sequence[str]) -> int:
    """Pure: the highest `priority:` among item file `texts`; an item with no key, or a value that is not an
    integer, counts as the default 0, and no items at all is 0."""
    def one(text: str) -> int:
        try:
            return int(parse_frontmatter(text)[0].get("priority", 0))
        except (TypeError, ValueError):
            return 0
    return max(map(one, texts), default=0)


def priority_text(original: str, value: int) -> str:
    """Pure: `original` with its `priority:` key set to `value`; every other header line stays byte for byte."""
    opening, closing = "---\n", "\n---\n"
    close_index = original.find(closing, len(opening)) if original.startswith(opening) else -1
    if close_index == -1:
        return f"{opening}priority: {value}\n---\n{original}"
    header_lines = original[len(opening):close_index].split("\n")
    header = "\n".join(_with_header_fields(header_lines, [("priority", value)]))
    return f"{opening}{header}{original[close_index:]}"


def _row_priority(row: Mapping) -> int:
    """A row's top-level `priority`, else the copy in `extra`; a missing or non-integer one reads as 0."""
    extra = row.get("extra")
    top = row.get("priority")
    return _int_or_zero(top if top is not None else extra.get("priority") if isinstance(extra, Mapping) else None)


def priority_rows(stored: Sequence[Mapping]) -> list[open_queue_priority.Row]:
    """Pure: one core `Row` per stored task or intake row, with its `_row_priority`."""
    return [
        open_queue_priority.Row(
            str(r.get("initiative") or r.get("task_id") or ""), str(r["kind"]), str(r.get("state") or ""), _row_priority(r)
        )
        for r in stored
        if r.get("kind") in ("task", "intake")
    ]


def priority_target(rows: Sequence[open_queue_priority.Row], initiative: str, flag: str) -> tuple[int, list[str]]:
    """Pure: `(value, lines to print)` for `top`, `bottom`, `up` or `down`. `top` and `bottom` name the open
    maximum or minimum they measured against, and say so when nothing else is open or the initiative is already
    in place; `up` and `down` pass the nearest open neighbour and print nothing."""
    core = open_queue_priority
    if flag == "up":
        return core.target_for_up(list(rows), initiative), []
    if flag == "down":
        return core.target_for_down(list(rows), initiative), []
    if flag not in ("top", "bottom"):
        raise ValueError(f"unknown priority flag: {flag!r}")
    current = max((r.priority for r in rows if r.initiative_id == initiative), default=0)
    top = flag == "top"
    target = (core.target_for_top if top else core.target_for_bottom)(list(rows), initiative)
    edge = "maximum" if top else "minimum"
    if target.measured is None:
        return target.value, [f"{initiative}: nothing else is open, priority stays {target.value}"]
    in_place = current > target.measured if top else current < target.measured
    if in_place:
        return target.value, [f"{initiative}: already at the {flag}, priority {target.value} against open {edge} {target.measured}"]
    return target.value, [f"{initiative}: open {edge} elsewhere is {target.measured}, setting priority {target.value}"]


def list_open_queue(runs_dir: Path) -> int:
    """Edge: print the open queue, highest priority first, one line per initiative. Reads the store, writes nothing."""
    entries = open_queue_priority.open_queue(priority_rows(run_store.read_queue(runs_dir)))
    if not entries:
        print("routing: the open queue is empty")
        return 0
    for line in open_queue_priority.format_listing(entries):
        print(line)
    return 0


def _queued_intake_row(runs_dir: Path, intake_id: str) -> Mapping | None:
    """Edge: the queued intake row `select_intake_row` finds for `intake_id`, by file stem or frontmatter `id`, else None."""
    queued = [r for r in run_store.read_queue(runs_dir, kind="intake") if r.get("state") == "queued"]
    return select_intake_row(queued, intake_id)


def intake_priority_rows(stored: Sequence[Mapping], intake: Mapping) -> list[open_queue_priority.Row]:
    """Pure: the measured rows for a queued intake. Every stored intake row shares the initiative `intake`, so
    the target is keyed by its own `task_id`, and its stored row is replaced by one carrying its own priority."""
    task_id = str(intake["task_id"])
    others = [r for r in stored if not (r.get("kind") == "intake" and r.get("task_id") == task_id)]
    own = open_queue_priority.Row(task_id, "intake", "queued", priority_rows([intake])[0].priority)
    return [*priority_rows(others), own]


def set_intake_priority(runs_dir: Path, ws: Path, intake: Mapping, flag: int | str) -> int:
    """Edge: `set_initiative_priority` for a queued intake row with no initiative yet. The store takes the new
    priority first; `intake/<stem>.md` follows through `priority_text`, which adds the `priority:` key when the
    file has none, so the priority survives a queue export."""
    task_id = str(intake["task_id"])
    if isinstance(flag, int):
        value = flag
    else:
        value, lines = priority_target(intake_priority_rows(run_store.read_queue(runs_dir), intake), task_id, flag)
        for line in lines:
            print(line)
    path = ws / "intake" / f"{task_id}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = None
    extra = intake.get("extra") or {}
    sent = {**intake, "priority": value, "extra": {**extra, "priority": value} if "priority" in extra else extra}
    if not run_store.upsert_row(runs_dir, sent):
        print(f"routing: the store did not take the priority for intake {task_id}")
        return 1
    if text is not None:
        from agent_tools import queue_export  # imported here: route loads on every cox start

        try:
            path.write_text(priority_text(text, value), encoding="utf-8")
        except OSError as exc:
            print(_filed_write_error(path, True, exc))
            return 1
        queue_export.record_written_paths(ws, [path])
    print(f"{task_id}: priority {value} on 1 item(s)")
    return 0


def set_initiative_priority(runs_dir: Path, ws: Path, initiative: str, flag: int | str) -> int:
    """Edge: one priority on every `work/<initiative>/<phase>/*.md` under `ws`, the store first and the file
    second through `write_filed_item`, as `cox route file` orders them. An int `flag` is `--set`; `top`,
    `bottom`, `up` and `down` measure the open queue in the store through `priority_target`. One call leaves the
    initiative's items all equal. Stops at the first file that fails to write. Returns 0, or non-zero after
    printing one line."""
    root = ws / "work" / initiative
    paths = sorted(root.glob("*/*.md")) if Path(initiative).name == initiative else []
    if not paths:
        intake = _queued_intake_row(runs_dir, initiative) if Path(initiative).name == initiative else None
        if intake is not None:
            return set_intake_priority(runs_dir, ws, intake, flag)
        print(f"routing: unknown initiative {initiative}")
        return 2
    try:
        texts = [p.read_text(encoding="utf-8") for p in paths]
    except OSError as exc:
        print(f"routing: cannot read the items of {initiative}: {exc}")
        return 1
    if isinstance(flag, int):
        value = flag
    else:
        own = [
            open_queue_priority.Row(initiative, "task", str(parse_frontmatter(t)[0].get("state") or ""), current_priority([t]))
            for t in texts
        ]
        value, lines = priority_target([*priority_rows(run_store.read_queue(runs_dir)), *own], initiative, flag)
        for line in lines:
            print(line)
    for path, text in zip(paths, texts, strict=True):
        parts = path.relative_to(ws / "work").parts
        if write_filed_item(runs_dir, path, "task", parts, priority_text(text, value), priority=value) != 0:
            return 1
    print(f"{initiative}: priority {value} on {len(paths)} item(s)")
    return 0
