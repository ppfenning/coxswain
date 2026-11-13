"""triage — classify a stranded item and emit by class (docs/design/triage.md §1).

    facts -> triage -> emit

The triage role classifies a stranded item against `facts` and must cite the record; `emit` builds the write by class — a `ticket_amend` proposal, an `item_create` proposal, a `decompose` handoff, or a `notify` proposal — and refuses to repeat an already-fired `(class, diagnosis)` pair on the same item, escalating to a person instead.

§1 names the field list and the input shape verbatim:

    `facts` (no model): from the work item and the task records of its
    attempts — quarantine reason and `kind` per attempt, review / adversary /
    arbitration verdicts, `fix_loop.stopped`, the trace's final result
    `subtype` per node, the evidence array, and whether the attempts predate
    the ticket's last revision (`attempts[].ts` vs the item file's last
    commit). Keyed facts: `attempt-<n>|<field>`.

    A cite naming a fact key not in `facts` is a fabricated citation and the
    answer is refused; zero cites is refused; a `diagnosis` that quotes no
    objection text from the record is refused.

`facts` is the harness's own arithmetic, not a model's recollection: it walks
the work item and the task records of its attempts and returns one flat
mapping keyed `attempt-<n>|<field>`, plain data in, plain data out, no clock,
no I/O, no network — the "item file's last commit" arrives as `work_item`'s
own `last_commit`, computed by the caller, never read from git here.

review, adversary and arbitration arrive shaped like a saved lifecycle
result — `{verdict, ...}` — read through `.get("verdict")`.

Per the chair's 2026-09-16 correction: a task record carries no per-node
trace subtype of its own: the trace lives beside the run, outside anything
this pure function is handed. Each attempt record MAY therefore carry an
optional `node_subtypes: {<node>: <subtype>}` mapping — assembled by the
caller (the epic, from the trace's own final result lines) and merely read
here. When an attempt carries it, `facts` emits one key per node,
`attempt-<n>|subtype:<node>`; when the attempt has no `node_subtypes` key (or
it is empty), `facts` emits no `subtype:` key for that attempt and nothing
else about the attempt changes.

The `triage` node that follows must cite exactly the keys `facts` produced; a
cite naming one it did not produce is a fabricated citation, and
`check_citation` is the mechanical check that catches it — alongside zero
cites and a diagnosis that quotes no objection text.

`_objections` collects the record's own words — each attempt's reason,
review finding, adversary claim and counter, and arbitration reasoning — the
vocabulary `check_citation` holds a diagnosis to. `_already_fired` reads
`attempts[].triage` for a prior run's `(class, diagnosis)`: an exact repeat
means the cap was already cleared on this reasoning once, and clearing it
again teaches nothing, so `run` escalates instead of emitting again. `_emit`
is the class table in §1 made mechanical: the graph never invents which
write kind a class deserves.

The chair asks for triage of a quarantine that needs a tweak, so the model
decides from the whole initiative: `initiative_context` carries the goal, the
phases with their goals and every sibling task. Each class names the `effect`
the epic applies: `rebuild` (ticket_defect), `hold` (platform_defect, shape,
hold, and an escalated repeat), `drop` and `combine` (into a named open
sibling). `triaged` carries the triage records the store kept for this task,
which the repeat guard reads beside `attempts[].triage`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphs._contract import ContractViolation, proposal, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["CLASSES", "EFFECTS", "GRAPH_NAME", "check_citation", "check_combine", "facts", "run"]

GRAPH_NAME = "triage"


def _is_stale(ts: Any, last_commit: Any) -> bool:
    """`attempts[].ts` vs the item file's last commit, per §1."""
    if ts is None or last_commit is None:
        return False
    return ts < last_commit


def _attempt_facts(attempt: Mapping[str, Any], last_commit: Any) -> dict[str, Any]:
    base = {
        "reason": attempt.get("reason"),
        "kind": attempt.get("kind"),
        "review": (attempt.get("review") or {}).get("verdict"),
        "adversary": (attempt.get("adversary") or {}).get("verdict"),
        "arbitration": (attempt.get("arbitration") if isinstance(attempt.get("arbitration"), Mapping) else {}).get("verdict"),
        "stopped": (attempt.get("fix_loop") or {}).get("stopped"),
        "evidence": attempt.get("evidence", ()),
        "stale": _is_stale(attempt.get("ts"), last_commit),
    }
    subtypes = attempt.get("node_subtypes") or {}
    return base | {f"subtype:{node}": subtype for node, subtype in subtypes.items()}


def facts(work_item: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Keyed facts for `triage` to cite: `attempt-<n>|<field>`, 1-indexed.

    `attempt-<n>|subtype:<node>` appears only when that attempt's record
    carries `node_subtypes`; absent, not defaulted, when it does not.
    """
    last_commit = work_item.get("last_commit")
    return {
        f"attempt-{n}|{field}": value
        for n, attempt in enumerate(attempts, start=1)
        for field, value in _attempt_facts(attempt, last_commit).items()
    }


def check_citation(
    cites: Sequence[str],
    diagnosis: str,
    facts_: Mapping[str, Any],
    objections: Sequence[str],
) -> str | None:
    """None when the citation stands; otherwise the reason it is refused, per §1."""
    if not cites:
        return "zero cites: a triage answer must cite at least one fact"
    fabricated = [cite for cite in cites if cite not in facts_]
    if fabricated:
        return f"fabricated citation: {fabricated[0]!r} is not a key `facts` produced"
    if not any(objection and objection in diagnosis for objection in objections):
        return "diagnosis quotes no objection text verbatim from the record"
    return None


CLASSES = ("ticket_defect", "platform_defect", "shape", "drop", "combine", "hold")
# `genuine_reject` was the class for an objection that stood; a person now decides it through the hold mark.
_ALIASES = {"genuine_reject": "hold"}
# What the epic does with the task for each class. `rebuild` builds it again on its amended body; `hold` leaves it for a
# person with the mark; `drop` drops it; `combine` folds it into the sibling `into` names and drops it.
EFFECTS = {
    "ticket_defect": "rebuild",
    "platform_defect": "hold",
    "shape": "hold",
    "hold": "hold",
    "drop": "drop",
    "combine": "combine",
}
# A sibling in one of these states is built or gone: there is nothing left to fold scope into.
_CLOSED = frozenset({"done", "dropped", "approved"})

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "class": {"type": "string", "enum": list(CLASSES)},
        "diagnosis": {"type": "string"},
        "cites": {"type": "array", "items": {"type": "string"}},
        "action": {"type": "string"},
        "into": {"type": "string"},
    },
    "required": ["class", "diagnosis", "cites", "action"],
    "additionalProperties": False,
}

_CLASS_GUIDE = (
    "Classes, pick the one that finishes the initiative:\n"
    "- ticket_defect: the ticket was missing an instruction; `action` is the exact text to append to it, quoting the objection.\n"
    "- platform_defect: the platform, not the ticket, failed; `action` names the mechanism to fix. A person is told.\n"
    "- shape: the task is really several tasks; `action` is the split. A person is told.\n"
    "- drop: the initiative's goal does not need this task; it is dropped and tasks that need it go on.\n"
    "- combine: this task's scope belongs in a sibling; set `into` to that sibling's id (one not done, approved or dropped).\n"
    "- hold: a person must decide; `action` says what they must decide."
)


def _class_of(raw: Any) -> str:
    """The model's class, with a retired name read as the class that replaced it."""
    name = str(raw or "")
    return _ALIASES.get(name, name)


def check_combine(into: str, task_id: str, context: Mapping[str, Any] | None) -> str | None:
    """None when `into` names an open sibling of the task in `context`; otherwise why the combine is refused."""
    if not into:
        return "combine names no sibling in `into`"
    if into == task_id:
        return f"combine names the task itself ({into!r}) in `into`"
    siblings = {str(s.get("id")): s for s in (context or {}).get("siblings") or [] if isinstance(s, Mapping)}
    sibling = siblings.get(into)
    if sibling is None:
        return f"combine names {into!r}, which is not a sibling in this initiative"
    if str(sibling.get("state") or "") in _CLOSED:
        return f"combine names {into!r}, which is {sibling.get('state')}; scope can only fold into an open task"
    return None


def _context_text(context: Mapping[str, Any] | None) -> str:
    """The initiative as the prompt shows it: its goal, its phases with their goals, and every sibling task."""
    if not context:
        return "Initiative: not given."
    phases = "\n".join(f"  - {p.get('id')}: {p.get('goal') or ''}" for p in context.get("phases") or [] if isinstance(p, Mapping))
    siblings = "\n".join(
        f"  - {s.get('id')} [{s.get('phase')}, {s.get('state')}] {s.get('title') or ''}; needs {list(s.get('needs') or [])}"
        for s in context.get("siblings") or []
        if isinstance(s, Mapping)
    )
    return f"Initiative goal: {context.get('goal') or ''}\nPhases:\n{phases or '  none'}\nSibling tasks:\n{siblings or '  none'}"


def _objections(attempts: Sequence[Mapping[str, Any]]) -> list[str]:
    """The record's own words: what a diagnosis must quote from, per §1."""
    texts: list[str] = []
    for attempt in attempts:
        if attempt.get("reason"):
            texts.append(str(attempt["reason"]))
        for finding in (attempt.get("review") or {}).get("findings") or []:
            if isinstance(finding, Mapping) and finding.get("detail"):
                texts.append(str(finding["detail"]))
        adversary = attempt.get("adversary") or {}
        if adversary.get("strongest_objection"):
            texts.append(str(adversary["strongest_objection"]))
        for objection in adversary.get("objections") or []:
            if isinstance(objection, Mapping):
                texts.extend(str(objection[key]) for key in ("claim", "why_wrong") if objection.get(key))
        arbitration = attempt.get("arbitration")
        arbitration = arbitration if isinstance(arbitration, Mapping) else {}
        if arbitration.get("reasoning"):
            texts.append(str(arbitration["reasoning"]))
    return texts


def _already_fired(
    attempts: Sequence[Mapping[str, Any]], class_: str, diagnosis: str, triaged: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any] | None:
    """A prior `attempts[].triage` entry, or a prior record the store kept (`triaged`), with this exact `(class, diagnosis)`,
    or None, per §1. A record under a retired class name compares as the class that replaced it."""
    records = [*(attempt.get("triage") or {} for attempt in attempts), *triaged]
    for triage in records:
        if not isinstance(triage, Mapping):
            continue
        if _class_of(triage.get("class")) == class_ and triage.get("diagnosis") == diagnosis:
            return dict(triage)
    return None


def _emit(
    cartridge: Mapping[str, Any],
    *,
    class_: str,
    diagnosis: str,
    action: str,
    evidence: Sequence[Mapping[str, Any]],
    target: str,
) -> dict[str, Any] | None:
    """The write (or handoff) a class earns, per §1's table; None for a class whose effect the epic applies itself."""
    if class_ == "ticket_defect":
        return proposal(
            cartridge,
            kind="ticket_amend",
            target=target,
            evidence=evidence,
            rationale=diagnosis,
            suggested_action=action,
        )
    if class_ == "platform_defect":
        return proposal(
            cartridge,
            kind="item_create",
            target=target,
            evidence=evidence,
            rationale=diagnosis,
            suggested_action=action,
            subject_new=True,
        )
    if class_ == "shape":
        return {
            "handoff": "decompose",
            "target": target,
            "evidence": [dict(item) for item in evidence],
            "rationale": diagnosis,
            "suggested_action": action,
        }
    if class_ in ("drop", "combine", "hold"):
        return None
    raise ContractViolation(f"triage returned an unknown class {class_!r}")


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """facts -> triage -> emit, per docs/design/triage.md §1."""
    cartridge = require_cartridge(args)
    run_id, date = require(args, "run_id", "date")
    work_item, raw_attempts = require(args, "work_item", "attempts")
    attempts = list(raw_attempts)
    initiative_context = args.get("initiative_context") if isinstance(args.get("initiative_context"), Mapping) else None
    prior_records = [r for r in args.get("triaged") or () if isinstance(r, Mapping)]

    facts_ = facts(work_item, attempts)
    objections = _objections(attempts)
    context = list(cartridge.get("context") or [])

    triaged = dict(
        runner.run(
            role="triage",
            tier="deep",
            schema=TRIAGE_SCHEMA,
            context=context,
            prompt=(
                "Classify this stranded item from its attempt record and decide what finishes the "
                "initiative it belongs to, not just this task.\n\n"
                f"{_context_text(initiative_context)}\n\n"
                f"Item: {work_item}\nFacts: {facts_}\n"
                f"Earlier triage of this item: {prior_records or 'none'}\n\n"
                f"{_CLASS_GUIDE}\n\n"
                "Cite the fact key(s) that support your diagnosis, quote the "
                "objection text verbatim in the diagnosis, and return the class, "
                "diagnosis, cites and the action to take."
            ),
        )
    )

    class_ = _class_of(triaged.get("class"))
    diagnosis = str(triaged.get("diagnosis") or "")
    cites = list(triaged.get("cites") or [])
    action = str(triaged.get("action") or "")
    into = str(triaged.get("into") or "")
    target = str(work_item.get("id"))

    refusal = check_citation(cites, diagnosis, facts_, objections)
    if refusal is not None:
        raise ContractViolation(f"triage answer refused: {refusal}")
    if class_ not in EFFECTS:
        raise ContractViolation(f"triage returned an unknown class {class_!r}")
    if class_ == "combine" and (why := check_combine(into, target, initiative_context)) is not None:
        raise ContractViolation(f"triage answer refused: {why}")

    prior = _already_fired(attempts, class_, diagnosis, prior_records)
    if prior is not None:
        return {
            "run_id": run_id,
            "date": date,
            "escalated": True,
            "class": class_,
            "diagnosis": diagnosis,
            "effect": "hold",
            "prior": prior,
        }

    evidence = [{"check": cite, "output": str(facts_.get(cite))} for cite in cites]
    emitted = _emit(
        cartridge,
        class_=class_,
        diagnosis=diagnosis,
        action=action,
        evidence=evidence,
        target=target,
    )

    return {
        "run_id": run_id,
        "date": date,
        "escalated": False,
        "class": class_,
        "diagnosis": diagnosis,
        "action": action,
        "effect": EFFECTS[class_],
        **({"into": into} if class_ == "combine" else {}),
        **({"emit": emitted} if emitted is not None else {}),
        "attempts_triage_entry": {"class": class_, "diagnosis": diagnosis, "run": run_id},
    }


# How a harness offers this graph as a subcommand. See graphs/_spec.py for why
# the spec is declarative and why the types live with the graphs. SPEC.name is
# "triage-quarantine", not "triage": `graphs/ops/triage_propose.py` already
# holds the "triage" subcommand for alert triage, and a subcommand name must
# be unique across the registry.
from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="triage-quarantine",
    graph_name=GRAPH_NAME,
    run=run,
    summary="classifies a stranded item from its attempt record and emits by class",
    needs=(
        Need(
            "work_item",
            flag="--work-item",
            kind="json_file",
            help="the work item's own record, including last_commit",
        ),
        Need(
            "attempts",
            flag="--attempts",
            kind="json_file",
            help="the task's attempt records this item accumulated",
        ),
        Need(
            "initiative_context",
            flag="--initiative-context",
            kind="json_file",
            help="the initiative's goal, phases with their goals, and sibling tasks (id, phase, state, title, needs)",
            required=False,
        ),
        Need(
            "triaged",
            flag="--triaged",
            kind="json_file",
            help="earlier triage records of this item, {class, diagnosis, run} each",
            required=False,
        ),
    ),
)
