"""The pure model behind `cox runs detail`; `NODE_ORDER` is the graph's node sequence, not a clock, because trace-name events carry no start order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from agent_tools import lease_standing
from agent_tools.events import Event
from agent_tools.progress_read import TIME_FORMAT, NodeProgress, Progress

__all__ = ["NODE_ORDER", "Detail", "NodeCall", "NodeView", "detail", "render"]

NODE_ORDER = (
    "plan",
    "scope_epic",
    "build",
    "handoff",
    "review_charter",
    "review_adversary",
    "arbitrate",
    "validate_chunk",
    "validate_phase",
)


@dataclass(frozen=True)
class NodeCall:
    node: str
    attempt: int
    turns: int
    cost_usd: float
    verdict: str = ""


@dataclass(frozen=True)
class NodeView:
    role: str
    task: str
    tier: str
    status: str
    attempt: int
    turns: int
    cost_usd: float | None
    last_tool: str
    last_tool_age_s: float | None
    worktree: str
    branch: str | None


@dataclass(frozen=True)
class Detail:
    run: str
    alive: bool
    status: str
    timeline: tuple[NodeCall, ...]
    objection: str
    quarantine_reason: str
    files_touched: tuple[str, ...]
    changed_lines: int
    last_calls: tuple[str, ...]
    check_excerpt: str = ""
    check_log: str = ""
    flaky_notes: tuple[str, ...] = ()
    nodes: tuple[NodeView, ...] = ()


def _flaky_notes(record: dict) -> tuple[str, ...]:
    """The `flaky: <ids> passed on serial rerun` notes of the checks that passed only on a serial rerun."""
    checks = (record.get("change_facts") or {}).get("checks") or ()
    return tuple(str(c["note"]) for c in checks if isinstance(c, dict) and c.get("note"))


def _first_failed_check(record: dict) -> dict:
    """The first failed configured check; the harness stores them under `change_facts["checks"]`."""
    checks = (record.get("change_facts") or {}).get("checks") or ()
    return next((c for c in checks if isinstance(c, dict) and not c.get("passed", True)), {})


def _status(events: list[Event], alive: bool, standing: str = lease_standing.NONE) -> str:
    if any(e.kind == "task_quarantined" for e in events):
        return "quarantined"
    if any(e.kind == "budget_stop" for e in events):
        return "budget"
    if not alive or any(e.kind == "run_exited" for e in events):
        return "exited"
    if lease_standing.status(True, standing) == lease_standing.FENCED:
        return "fenced"
    return "running"

def _order_key(node: str) -> int:
    return NODE_ORDER.index(node) if node in NODE_ORDER else len(NODE_ORDER)


def _timeline(events: list[Event], calls: list[dict]) -> tuple[NodeCall, ...]:
    seen: list[tuple[str, int]] = []
    for e in events:
        if e.kind != "node_started":
            continue
        key = (e.detail["node"], e.detail["attempt"])
        if key not in seen:
            seen.append(key)
    calls_by_key = {(c["node"], c["attempt"]): c for c in calls}
    last_verdict: dict[str, str] = {}
    for e in events:
        if e.kind == "verdict":
            last_verdict[e.detail["node"]] = e.detail["verdict"]
    max_attempt: dict[str, int] = {}
    for node, attempt in seen:
        max_attempt[node] = max(max_attempt.get(node, 0), attempt)
    entries = [
        NodeCall(
            node=node,
            attempt=attempt,
            turns=calls_by_key.get((node, attempt), {}).get("turns", 0),
            cost_usd=calls_by_key.get((node, attempt), {}).get("cost_usd", 0.0),
            verdict=last_verdict.get(node, "") if attempt == max_attempt[node] else "",
        )
        for node, attempt in seen
    ]
    return tuple(sorted(entries, key=lambda nc: (nc.attempt, _order_key(nc.node), nc.node)))


def _first_sentence(text: str) -> str:
    first, _, _ = text.strip().partition(".")
    return first.strip()


def _objection(events: list[Event], record: dict) -> str:
    verdicts = [e for e in events if e.kind == "verdict"]
    if not verdicts or verdicts[-1].detail["verdict"] != "revise":
        return ""
    arbitration = record.get("arbitration")
    if isinstance(arbitration, dict):
        reasoning = arbitration.get("reasoning", "")
    else:
        reasoning = getattr(arbitration, "reasoning", "") if arbitration is not None else ""
    if reasoning:
        return _first_sentence(reasoning)
    adversary = record.get("adversary") or []
    findings = adversary if isinstance(adversary, list) else [adversary]
    for finding in findings:
        why_wrong = (finding or {}).get("why_wrong") if isinstance(finding, dict) else None
        if why_wrong:
            return _first_sentence(why_wrong)
    return ""


def _quarantine_reason(events: list[Event]) -> str:
    quarantined = [e for e in events if e.kind == "task_quarantined"]
    return quarantined[-1].detail.get("reason", "") if quarantined else ""


def _age_s(timestamp: str, now: datetime | None) -> float | None:
    """Seconds from `timestamp` to `now`; None when either is missing or the timestamp does not parse."""
    if now is None:
        return None
    try:
        then = datetime.strptime(timestamp, TIME_FORMAT).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return (now - then).total_seconds()


def _node_view(node: NodeProgress, events: list[Event], calls_by_key: dict, now: datetime | None) -> NodeView:
    call = calls_by_key.get((node.node, node.attempt))
    quarantined = any(e.kind == "task_quarantined" and e.detail.get("task") == node.task for e in events)
    return NodeView(
        role=node.node,
        task=node.task,
        tier=node.tier,
        status="quarantined" if quarantined else node.status,
        attempt=node.attempt,
        turns=call["turns"] if call else node.turns,
        cost_usd=call["cost_usd"] if call else node.cost_usd,
        last_tool=node.last_tool,
        last_tool_age_s=_age_s(node.last_tool_at, now),
        worktree=node.worktree,
        branch=node.branch,
    )


def _nodes(progress: Progress | None, events: list[Event], calls: list[dict],
           now: datetime | None) -> tuple[NodeView, ...]:
    if progress is None:
        return ()
    calls_by_key = {(c["node"], c["attempt"]): c for c in calls}
    return tuple(_node_view(n, events, calls_by_key, now) for n in progress.nodes)


def detail(run: str, alive: bool, events: list[Event], calls: list[dict], record: dict, tail: list[str],
           standing: str = lease_standing.NONE, progress: Progress | None = None,
           now: datetime | None = None) -> Detail:
    """Pure: the one Detail a run's events, finished calls, task record and
    trace tail make. `record` may be `{}`; every derived field is then ""
    or empty, never a raise. `nodes` is empty without `progress`; `now` is
    passed in, and without it a node's last-tool age is None."""
    change_facts = record.get("change_facts") or {}
    return Detail(
        run=run,
        alive=alive,
        status=_status(events, alive, standing),
        timeline=_timeline(events, calls),
        objection=_objection(events, record),
        quarantine_reason=_quarantine_reason(events),
        files_touched=tuple(change_facts.get("files_touched") or ()),
        changed_lines=int(change_facts.get("changed_lines") or 0),
        last_calls=tuple(tail[-3:]),
        check_excerpt=str(_first_failed_check(record).get("excerpt") or ""),
        check_log=str(_first_failed_check(record).get("log") or ""),
        flaky_notes=_flaky_notes(record),
        nodes=_nodes(progress, events, calls, now),
    )


def _cut(line: str, width: int) -> str:
    return line[: max(width, 0)]


def _node_line(n: NodeView) -> str:
    cost = "-" if n.cost_usd is None else f"${n.cost_usd:.2f}"
    age = "" if n.last_tool_age_s is None else f" {int(n.last_tool_age_s)}s ago"
    tool = f"{n.last_tool}{age}" if n.last_tool else "-"
    return f"  {n.role} {n.task} {n.tier} {n.status} {n.turns} {cost} {tool} {n.branch or n.worktree}"


def render(detail: Detail, width: int) -> list[str]:
    lines = [f"run {detail.run} [{detail.status}]"]
    lines += [f"{nc.node}#{nc.attempt}  {nc.turns}  ${nc.cost_usd:.2f}  {nc.verdict}" for nc in detail.timeline]
    lines += [_node_line(n) for n in detail.nodes]
    if detail.objection:
        lines.append(f"objection: {detail.objection}")
    if detail.quarantine_reason:
        lines.append(f"quarantine: {detail.quarantine_reason}")
    if detail.check_excerpt:
        lines.extend(f"  {ln}" for ln in detail.check_excerpt.splitlines())
    if detail.check_log:
        lines.append(f"  full output: runs/{detail.run}/{detail.check_log}")
    lines.extend(detail.flaky_notes)
    if detail.files_touched:
        lines.append(f"files: {', '.join(detail.files_touched)}")
    if detail.last_calls:
        lines.append(f"last: {', '.join(detail.last_calls)}")
    return [_cut(line, width) for line in lines]
