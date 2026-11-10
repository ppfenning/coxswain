"""initiative-decompose — a large idea into phases and a task DAG.

    decompose -> adversary -> emit

The front of the pipeline, and the step that makes everything after it
parallelisable. An idea arrives as prose; what comes out is phases, tasks, and
the dependency edges between them — and one `item_create` proposal per task.

The adversarial pass here is the highest-leverage one in the system, and it has
a single job: **attack the dependency edges.** Every edge that is not real
serialises work that could have run at once, and the person who just drew the
graph is the last person likely to notice they drew too many. An edge that
exists because the work "feels sequential" costs a phase its parallelism, and
nothing else in the pipeline will ever question it.

Strictly propose-only, like everything else — the tasks land as proposals, and
the work store is written by an apply arm after a human said yes.
"""

from __future__ import annotations

import json
import posixpath
import re
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from itertools import takewhile
from pathlib import Path
from typing import Any

import yaml

from graphs._contract import ContractViolation, epic_shape, landing_for, proposal, require, require_cartridge
from graphs.delivery.decompose_layout import check_layout, heal_layout, unlisted_artifacts
from graphs.delivery.decompose_order import order_cross_phase_surfaces, order_shared_surfaces
from graphs.delivery.decompose_size import (
    SizeError,
    SizeVerdict,
    apply_override,
    parse_size_verdict,
    single_ticket_initiative,
)
from graphs.delivery.decompose_split import split_foreign
from graphs.delivery.phase_split import split_same_phase_needs
from graphs.delivery.ticket_lint import Problem, heal_idea_reach, lint_tickets
from graphs.delivery.ticket_lint import lint_refusal_text as _lint_refusal_text
from harness.surface_heal import heal_bare_filenames, heal_new_markers, notes_out_of_surfaces
from runner.decision_log import RouterDecision
from runner.decision_source import DecisionSource, NoDecisionSource, ask
from runner.protocol import NodeRunner
from runner.tier_resolution import Hints

__all__ = [
    "GRAPH_NAME",
    "LintRefusal",
    "approved_task_ids",
    "decompose_closing",
    "expected_task_files",
    "first_problem_line",
    "idea_parts",
    "initiative_text",
    "resolve_surfaces",
    "run",
    "surface_problem",
    "unexecuted_item_create_reason",
]

# A `/` or a file suffix, no spaces, optional trailing `(new)` marker — the
# same shape lifecycle_propose._PATH_TOKEN checks. An all-caps bare token
# (LICENSE, README) has neither and is kept on the naming convention alone.
# An optional trailing `/` keeps a directory surface (a snapshots folder), which steering and the layout check both read.
_PATH_TOKEN = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)*/?$")


_NEW = " (new)"


def _looks_like_a_surface(entry: str) -> bool:
    body = entry[: -len(_NEW)] if entry.endswith(_NEW) else entry
    if not _PATH_TOKEN.match(body):
        return False
    return "/" in body or bool(re.search(r"\.[A-Za-z0-9]{1,5}$", body)) or body.isupper()


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance: insert, delete, and substitute each cost 1."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1]


# The declared phase in a task is the seat's own transcription of a plan id it
# was just given — a one- or two-character slip, not a different word. Wider
# than that and a "correction" is a guess dressed as a fix, which is exactly
# what turned 'faulthandring-signal' into a silently accepted 'faulthandler-
# signal' would have been: that pair sits at distance 4, outside this budget,
# and the node fails instead of guessing.
_PHASE_EDIT_DISTANCE_BUDGET = 2


def _resolve_phase(phase: str, declared: Sequence[str]) -> tuple[str, str | None] | None:
    """Resolve `phase` against the plan's declared phases.

    `(phase, None)` on an exact match. `(matched, note)` when exactly one
    declared phase is within `_PHASE_EDIT_DISTANCE_BUDGET` — `note` names the
    correction. `None` when no declared phase is close enough, or more than
    one ties: an ambiguous near-miss is a defect for the caller to refuse,
    never a guess.
    """
    if phase in declared:
        return phase, None
    near = [d for d in declared if _edit_distance(phase, d) <= _PHASE_EDIT_DISTANCE_BUDGET]
    if len(near) == 1:
        return near[0], f'phase "{phase}" corrected to "{near[0]}"'
    return None


GRAPH_NAME = "initiative-decompose"


def _decision(source: DecisionSource, role: str, hints: Hints | None) -> dict[str, RouterDecision]:
    """`router_decision` kwarg for a runner call; empty when the source has no decision, so it defaults to None."""
    decision = ask(source, role, hints)
    return {} if decision is None else {"router_decision": decision}


DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "goal": {"type": "string"}},
                "required": ["id", "goal"],
                "additionalProperties": False,
            },
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "phase": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "needs": {"type": "array", "items": {"type": "string"}},
                    "surfaces": {"type": "array", "items": {"type": "string"}},
                    "repo": {"type": "string"},
                    "interface": {"type": "string"},
                    "side": {"type": "string", "enum": ["contract", "producer", "consumer"]},
                },
                "required": ["id", "phase", "title", "body", "needs", "surfaces"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["phases", "tasks", "rationale"],
    "additionalProperties": False,
}

SIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["task", "epic"]},
        "reason": {"type": "string"},
        "surfaces": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

EDGE_CHALLENGE_SCHEMA = {
    "type": "object",
    "properties": {
        "spurious_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "needs": {"type": "string"},
                    "why_not_real": {"type": "string"},
                },
                "required": ["task", "needs", "why_not_real"],
                "additionalProperties": False,
            },
        },
        "missing_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "needs": {"type": "string"},
                    "why_real": {"type": "string"},
                },
                "required": ["task", "needs", "why_real"],
                "additionalProperties": False,
            },
        },
        "verdict": {"type": "string", "enum": ["accept", "revise"]},
        "summary": {"type": "string"},
    },
    "required": ["spurious_edges", "missing_edges", "verdict", "summary"],
    "additionalProperties": False,
}

UNBUILDABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "surface": {"type": "string"},
                    "replacement": {"type": "string"},
                },
                "required": ["task", "surface", "replacement"],
                "additionalProperties": False,
            },
        },
        "tasks": DECOMPOSE_SCHEMA["properties"]["tasks"],
        "summary": {"type": "string"},
    },
    "required": ["corrections", "summary"],
    "additionalProperties": False,
}


def _threshold_text(cartridge: Mapping[str, Any]) -> str:
    threshold = cartridge.get("epic_threshold") or {}
    return yaml.safe_dump(dict(threshold), sort_keys=False).strip() if threshold else "none configured"


def _size_prompt(intake_text: str, repo_files: Sequence[str], threshold_text: str) -> str:
    return (
        "Size this intake: is it one ticket (`task`) or an epic? Answer `task` only when one "
        "change to a few files does it, and then list those files in `surfaces`. Give a "
        "one-sentence `reason`.\n\n"
        f"Epic threshold:\n{threshold_text}\n\n"
        f"Intake:\n{intake_text}\n\n"
        "Repository files:\n" + "\n".join(repo_files)
    )


def _size_route(raw: Mapping[str, Any], frontmatter: Mapping[str, Any]) -> SizeVerdict | SizeError:
    """The model's verdict with the intake's `size:` override applied; a SizeError means run the epic path."""
    return apply_override(parse_size_verdict(dict(raw)), dict(frontmatter))


def _size_record(verdict: SizeVerdict | SizeError | None) -> dict[str, str] | None:
    """The size outcome to record; None when no size step ran. A SizeError ran the epic path as a fallback."""
    if verdict is None:
        return None
    if isinstance(verdict, SizeError):
        return {"size": "epic", "size_reason": verdict.message, "size_source": "fallback"}
    return {"size": verdict.verdict, "size_reason": verdict.reason, "size_source": verdict.source}


def _size_ledger_entry(record: Mapping[str, str], *, run_id: str, date: str, took_task_path: bool) -> dict[str, Any]:
    """One ledger line; `skipped` is present only on the task path, which never runs scope_epic or phase planning."""
    skipped = {"skipped": ["scope_epic", "phase_planning"]} if took_task_path else {}
    return {"run_id": run_id, "date": date, "graph": GRAPH_NAME, "event": "size", **record, **skipped}


def _append_size_ledger(runner: Any, run_id: str, entry: Mapping[str, Any]) -> None:
    """Append `entry` to `<runs_dir>/<run_id>.size.jsonl`, the runner's runs_dir as the checkpoint ledger uses.

    A no-op when the runner carries no `runs_dir`. Append mode only; a reader lives in coxswain-tools.
    """
    runs_dir = getattr(runner, "runs_dir", None)
    if not runs_dir:
        return
    with (Path(runs_dir) / f"{run_id}.size.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _scalar(value: Any) -> str:
    """One YAML flow scalar, quoted only when YAML requires it."""
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10_000)
    return dumped.strip().removesuffix("...").rstrip()


def _tilde_repo(repo: str, home: str | None) -> str:
    """`repo` with a leading `home` replaced by `~`; any value not at or under `home` is returned unchanged."""
    root = (home or "").rstrip("/")
    if not root:
        return repo
    if repo == root:
        return "~"
    return "~" + repo[len(root) :] if repo.startswith(root + "/") else repo


_TITLE_MAX = 120


def _unquote(value: str) -> str:
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value


def idea_parts(idea: str) -> tuple[str, str, str]:
    """(title, repo, body) of an idea; an intake file's frontmatter supplies title and repo and is dropped from body.

    The title is one line of at most 120 characters. repo is empty when there is none.
    """
    lines = idea.split("\n")
    close = (
        next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if lines[0].strip() == "---"
        else None
    )
    block = (
        {key.strip(): _unquote(value) for key, sep, value in (line.partition(":") for line in lines[1:close]) if sep}
        if close is not None
        else {}
    )
    body = "\n".join(lines[close + 1 :]).lstrip("\n") if close is not None else idea
    heading = next((line[2:].strip() for line in body.split("\n") if line.startswith("# ")), "")
    first = next((line.strip() for line in body.split("\n") if line.strip()), "")
    title = (block.get("title") or heading or first).strip().split("\n")[0][:_TITLE_MAX]
    return title, block.get("repo", ""), body


def initiative_frontmatter(
    idea: Mapping[str, Any],
    repo: str,
    *,
    intake: str | None = None,
    size: Mapping[str, str] | None = None,
    home: str | None = None,
    epic: str | None = None,
    target_branch: str | None = None,
    lint: Sequence[str] = (),
) -> dict[str, Any]:
    """Pure: the `initiative.md` frontmatter fields, in the order `initiative_text` renders them."""
    carried = (("epic", epic), ("target_branch", target_branch))
    return {
        "id": idea.get("id"),
        "title": idea.get("title"),
        "repo": _tilde_repo(repo, home),
        "budget_usd": idea.get("budget_usd"),
        **({"intake": intake} if intake else {}),
        **({key: size[key] for key in ("size", "size_reason", "size_source")} if size else {}),
        **{key: value for key, value in carried if value is not None},
        **({"lint": list(lint)} if lint else {}),
    }


def initiative_body_text(idea: Mapping[str, Any], phases: Sequence[str], goals: Mapping[str, str]) -> str:
    """Pure: the `initiative.md` text after the frontmatter, the idea and its phase goals."""
    goal_lines = "\n".join(f"- {phase_id}: {goals.get(phase_id, '')}" for phase_id in phases)
    return f"{idea.get('why', '')}\n\nPHASE GOALS, each judged against ITS OWN line:\n{goal_lines}\n"


def initiative_text(
    idea: Mapping[str, Any],
    phases: Sequence[str],
    goals: Mapping[str, str],
    repo: str,
    *,
    intake: str | None = None,
    size: Mapping[str, str] | None = None,
    home: str | None = None,
    epic: str | None = None,
    target_branch: str | None = None,
    lint: Sequence[str] = (),
) -> str:
    """The `initiative.md` shape every hand-written initiative in the workspace carries.

    A non-empty `lint` adds a `lint:` block list to the frontmatter. Raises ContractViolation naming the
    initiative when its title spans more than one line.
    """
    if "\n" in str(idea.get("title") or ""):
        raise ContractViolation(f"initiative {idea.get('id')} has a multi-line title; a title is one line")
    fields = initiative_frontmatter(
        idea, repo, intake=intake, size=size, home=home, epic=epic, target_branch=target_branch, lint=lint
    )
    lines = "".join(
        "lint:\n" + "".join(f"  - {_scalar(note)}\n" for note in value)
        if key == "lint"
        else f"{key}: {_scalar(value)}\n"
        for key, value in fields.items()
    )
    return f"---\n{lines}---\n\n{initiative_body_text(idea, phases, goals)}"


def resolve_surfaces(surfaces: Sequence[str], tree: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Match each prose surface against a real path in tree. (resolved paths, unresolved prose).

    Same name and signature as `core.workstore.resolve_surfaces` so a later
    `from core.workstore import resolve_surfaces, surface_problem` is a one-line
    swap — but a graph never imports `core` itself, so this stays the real
    implementation here, not a stand-in.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    for surface in surfaces:
        if surface.endswith("/") and any(str(row.get("path")).startswith(surface) for row in tree):
            resolved.append(surface)  # a directory surface names the folder, not the first file found under it
            continue
        hit = next(
            (str(row.get("path")) for row in tree if surface == row.get("path") or surface in str(row.get("path"))),
            None,
        )
        (resolved if hit else unresolved).append(hit or surface)
    return resolved, unresolved


def repo_listing(repo: str) -> list[dict[str, str]]:
    """Tracked files of the checkout at `repo` as tree rows; empty when it is not a readable git checkout."""
    root = Path(repo).expanduser() if repo else None
    if root is None or not root.is_dir():
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except OSError, subprocess.SubprocessError:
        return []
    return [{"repo": repo, "path": line} for line in out.splitlines() if line]


def with_named_artifacts(task: Mapping[str, Any], tree: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Adds each unlisted named artifact: its one tree path, or a path-shaped name as `(new)`; anything else stays."""
    added: list[str] = []
    for token in unlisted_artifacts(task):
        hits = sorted({p for p in (str(r.get("path")) for r in tree) if p == token or p.endswith("/" + token)})
        if len(hits) == 1:
            added.append(hits[0])
        elif not hits and "/" in token:
            added.append(token + _NEW)
    if not added:
        return task
    return dict(
        task,
        surfaces=[*(task.get("surfaces") or []), *added],
        lint=[*(task.get("lint") or []), *(f"added to surfaces: {a} (named in the body)" for a in added)],
    )


def _declared_generated_files() -> Mapping[str, Any]:
    """Edge: the tools package's declaration of generated files; empty when that package is not installed."""
    try:
        from agent_tools.land import GENERATED_FILES  # imported here: the graphs package stays importable without it
    except ImportError:
        return {}
    return GENERATED_FILES


def with_generated_files(task: Mapping[str, Any], generated: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Adds each declared generated file whose generator source is a surface; the body never names it, by design."""
    declared = _declared_generated_files() if generated is None else generated
    surfaces = [str(s) for s in task.get("surfaces") or []]
    listed = {s.removesuffix(_NEW) for s in surfaces}
    added = [
        (path, source)
        for path, spec in declared.items()
        if path not in listed
        for source in [next((s for s in spec.sources if s in listed), None)]
        if source is not None
    ]
    if not added:
        return task
    return dict(
        task,
        surfaces=[*surfaces, *(path for path, _ in added)],
        lint=[
            *(task.get("lint") or []),
            *(f"added to surfaces: {path} (generated from {source}, which is a surface)" for path, source in added),
        ],
    )


def surface_problem(unresolved: Sequence[str]) -> str | None:
    """One line per unresolved surface; `None` when there is nothing unbuildable."""
    if not unresolved:
        return None
    return "\n".join(f"unbuildable: surfaces are prose — {item}" for item in unresolved)


class LintRefusal(ContractViolation):
    """Ticket lint refused the decomposition for reach or coupling; the message is one problem per line."""


def first_problem_line(lines: Sequence[str]) -> str | None:
    """The first line with text, stripped of surrounding whitespace; None when every line is blank."""
    return next((s for s in (line.strip() for line in lines) if s), None)


def _refused_surfaces(tasks: Sequence[Mapping[str, Any]], problems: Sequence[Problem]) -> str:
    """The surfaces each refused task has now; a correction replaces one only by naming it exactly."""
    refused = dict.fromkeys(p.task for p in problems)
    lines = [
        f"{t['id']}: {', '.join(str(s) for s in t.get('surfaces') or []) or '(none)'}"
        for t in tasks
        if str(t["id"]) in refused
    ]
    return (
        "Surfaces of the refused tasks as they stand. A correction names one of them exactly as written:\n"
        + "\n".join(lines)
    )


def _contract_fixture_rule(
    tasks: Sequence[Mapping[str, Any]], problems: Sequence[Problem], tree: Sequence[Mapping[str, Any]]
) -> str:
    """The .json fixture rule and the path to use per refused interface; "" when no refusal is a contract one."""
    refused = {p.task for p in problems if p.rule == "contract"}
    if not refused:
        return ""
    interfaces = dict.fromkeys(str(t["interface"]) for t in tasks if str(t["id"]) in refused and t.get("interface"))
    held = {(str(e.get("path")), e.get("repo")) for e in tree}
    held_paths = {path for path, _ in held}

    def line(name: str) -> str:
        path = f"tests/fixtures/{name}_v1.json"
        repos = {
            t.get("repo") for t in tasks if t.get("interface") == name and t.get("side") == "producer" and t.get("repo")
        }
        # With no producer task the repository is unknown, so any repository holding the path counts.
        present = any((path, r) in held for r in repos) if repos else path in held_paths
        return f"{name}: {path}" + ("" if present else " (new)")

    return (
        "A contract task's surface is a fixture file ending in `.json`, committed in the producer "
        "repository, as the dash feed's `dash_feed_v1.json` is. Give the contract task of each "
        "interface this path:\n" + "\n".join(line(name) for name in interfaces)
    )


# ticket_lint reports these as advisory. Here they are refusals: a ticket naming a command the build
# was not granted, or a file in another repository, must not be stored ready.
_REWRITE_RULES = frozenset({"grant", "cross_repo"})


def _is_refusal(problem: Problem) -> bool:
    """Reach, coupling and contract by severity; grant and cross_repo by rule name. Size stays advisory."""
    return problem.severity == "refusal" or problem.rule in _REWRITE_RULES


def _noted(tasks: Sequence[Mapping[str, Any]], problems: Sequence[Problem], why: str) -> list[dict[str, Any]]:
    """The tasks with each problem a `lint:` entry on its ticket instead of a refusal: lint never stops a decompose.
    The human at the approval gate reads them there."""
    notes: dict[str, list[str]] = {}
    for p in problems:
        notes.setdefault(p.task, []).append(f"{p.rule}: {p.detail} ({p.fix}) [unresolved {why}]")
    return [
        dict(t, lint=[*(t.get("lint") or []), *notes[str(t["id"])]]) if str(t["id"]) in notes else dict(t)
        for t in tasks
    ]


def _rewrite_correction(problems: Sequence[Problem], grants: Sequence[str]) -> str:
    """The rewrite prompt's tail: one line per refused ticket naming its rule, then the build grant."""
    return (
        "Ticket lint refused these tickets. Rewrite the plan so each problem no longer holds, "
        "and keep every other ticket as it is.\n\n"
        + "\n".join(f"{p.task}: {p.rule} — {p.detail} ({p.fix})" for p in problems)
        + "\n\n"
        + _grant_sentence(grants)
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The leading `---` block of `text` as a mapping, and the text after it.

    No opening line, or no closing line, is no block: `({}, text)`. A block that is not
    YAML, or not a mapping, still comes off the body and gives `{}`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    close = next((i for i, line in enumerate(lines) if i and line.strip() == "---"), None)
    if close is None:
        return {}, text
    try:
        loaded = yaml.safe_load("\n".join(lines[1:close]))
    except yaml.YAMLError:
        loaded = None
    return (loaded if isinstance(loaded, dict) else {}), "\n".join(lines[close + 1 :])


def _frontmatter_block(text: str) -> str:
    """The leading `---` block of `text`, delimiters and line endings included; empty when there is none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return ""
    close = next((i for i, line in enumerate(lines) if i and line.strip() == "---"), None)
    return "" if close is None else "".join(lines[: close + 1])


def _reach_allow(args: Mapping[str, Any], idea_frontmatter: Mapping[str, Any]) -> list[str]:
    """`reach_allow` from the intake's frontmatter when an intake is given, else the idea's own.

    Anything other than a list of strings is an empty allow-list."""
    intake = args.get("intake")
    source = intake.get("frontmatter") if isinstance(intake, Mapping) else idea_frontmatter
    value = source.get("reach_allow") if isinstance(source, Mapping) else None
    return list(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def _core_validators() -> dict[str, Callable[[Any], str]]:
    """The substrate's validators for the carried keys.

    Imported here, and only when a key is set, because a graph does not otherwise import `core`:
    a run that carries neither key must work where `core.epics` is absent."""
    from core.epics import valid_epic_id
    from core.landing import valid_branch

    return {"epic": valid_epic_id, "target_branch": lambda value: valid_branch(value, "initiative")}


def _carry(
    args: Mapping[str, Any],
    idea_frontmatter: Mapping[str, Any],
    validators: Mapping[str, Callable[[Any], str]] | None = None,
) -> dict[str, str]:
    """The valid `epic` and `target_branch` the intake's frontmatter names, else the idea's own; unset keys are absent.

    Raises LintRefusal naming the key when a value is set but invalid. `validators` default to core's."""
    intake = args.get("intake")
    source = intake.get("frontmatter") if isinstance(intake, Mapping) else idea_frontmatter
    found = source if isinstance(source, Mapping) else {}
    keys = [key for key in ("epic", "target_branch") if found.get(key) is not None]
    checks = (validators or _core_validators()) if keys else {}
    return {key: _checked(key, checks[key], found[key]) for key in keys}


def _checked(key: str, check: Callable[[Any], str], value: Any) -> str:
    """`check(value)`, with a ValueError re-raised as a LintRefusal that starts with `key`."""
    try:
        return check(value)
    except ValueError as error:
        raise LintRefusal(f"{key}: {error}") from error


def _apply_surface_resolutions(tasks: list[dict[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """New task dicts; each task's surfaces resolved against tree. The caller's tasks are untouched.

    Anything that does not resolve keeps its prose rather than being dropped —
    an unresolved surface is a defect for `run` to act on, never one this
    function silently discards.
    """
    new_tasks = []
    for task in tasks:
        resolved, unresolved = resolve_surfaces(task.get("surfaces") or [], tree)
        new_tasks.append(dict(task, surfaces=resolved + unresolved))
    return new_tasks


def _unresolved_pairs(tasks: Sequence[Mapping[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[str]:
    """`"task: prose"` for every surface still unresolved against tree, one per line-to-be."""
    return [
        f"{task['id']}: {surface}"
        for task in tasks
        for surface in resolve_surfaces([s for s in task.get("surfaces") or [] if not s.endswith(_NEW)], tree)[1]
    ]


def _tests_replacement(surface: str, tree: Sequence[Mapping[str, Any]]) -> str | None:
    """The `tests/`-rooted tree path whose file name matches `surface`'s, else None."""
    name = surface.rsplit("/", 1)[-1]
    return next(
        (
            str(row.get("path"))
            for row in tree
            if str(row.get("path")).startswith("tests/") and str(row.get("path")).rsplit("/", 1)[-1] == name
        ),
        None,
    )


def _mark_new_surfaces(tasks: Sequence[Mapping[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """New task dicts; a path-shaped surface matching no tree path gets ' (new)'.

    The parent directory must exist in the tree (some path starts with it plus
    a slash) or be the repository root. Otherwise the surface names the wrong
    place, and ContractViolation says the parent directory is missing.
    """
    paths = [str(row.get("path")) for row in tree]

    def mark(task_id: Any, surface: str) -> str:
        if surface.endswith(_NEW) or not _looks_like_a_surface(surface) or not resolve_surfaces([surface], tree)[1]:
            return surface
        parent = surface.rsplit("/", 1)[0] if "/" in surface else ""
        if not parent or any(path.startswith(parent + "/") for path in paths):
            return surface + _NEW
        raise ContractViolation(
            f"{task_id}: {surface}: not in the checkout and its parent directory {parent} is missing"
        )

    return [dict(task, surfaces=[mark(task["id"], s) for s in task.get("surfaces") or []]) for task in tasks]


def _validate_plan_surfaces(
    tasks: list[dict[str, Any]], tree: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """New task dicts with a real `tests/` path swapped in for a misplaced one, then new files marked; `(task, surface)` failures.

    Runs ahead of the prose-oriented resolution below: a surface that is
    `(new)`, is not path-shaped, or already resolves against `tree` is left
    for that code to handle exactly as it does today. What is left is a
    well-formed path: a `tests/` rename is corrected here, and any other
    unmatched path is marked `(new)` or, with no parent directory, refused.
    The failure list is kept for the callers' signature and is always empty.
    """
    new_tasks: list[dict[str, Any]] = []
    failures: list[str] = []
    for task in tasks:
        surfaces = task.get("surfaces") or []
        resolved, unresolved = resolve_surfaces(surfaces, tree)
        still_prose = set(unresolved)
        fixes: list[str] = []
        new_surfaces: list[str] = []
        for surface in surfaces:
            candidate = surface
            eligible = (
                candidate in still_prose and not candidate.endswith(" (new)") and _looks_like_a_surface(candidate)
            )
            if not eligible:
                new_surfaces.append(surface)
                continue
            replacement = _tests_replacement(candidate, tree)
            if replacement is None:
                new_surfaces.append(surface)
                continue
            fixes.append(f"{surface} -> {replacement}")
            new_surfaces.append(replacement)
        new_tasks.append(dict(task, surfaces=new_surfaces, surface_fixes=fixes))
    return _mark_new_surfaces(new_tasks, tree), failures


def _drop_foreign_needs(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New task dicts; a `needs` entry naming no id in this plan is removed and recorded.

    A foreign id names real work, just not work this plan orders by an edge —
    it lands on its own schedule, so dropping the edge is correct, not a
    workaround for a decompose mistake.
    """
    ids = {str(t["id"]) for t in tasks}
    new_tasks: list[dict[str, Any]] = []
    for task in tasks:
        needs = task.get("needs") or []
        kept = [n for n in needs if n in ids]
        dropped = [n for n in needs if n not in ids]
        new_tasks.append(dict(task, needs=kept, needs_dropped=dropped))
    return new_tasks


def _apply_corrections(
    tasks: list[dict[str, Any]],
    corrections: Sequence[Mapping[str, Any]],
    added: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """New task dicts; a corrected surface replaces the prose the adversary named.

    From `added`, a task whose id is already in the plan contributes its `needs` and nothing else;
    any other task joins the plan ahead of the rest.
    """
    by_task: dict[str, dict[str, str]] = {}
    for c in corrections:
        by_task.setdefault(str(c.get("task")), {})[str(c.get("surface"))] = str(c.get("replacement"))
    known = {str(t["id"]) for t in tasks}
    restated = {str(t["id"]): list(t.get("needs") or []) for t in added if str(t["id"]) in known}
    fixed = [
        dict(
            t,
            surfaces=[by_task.get(str(t["id"]), {}).get(s, s) for s in t.get("surfaces") or []],
            needs=restated.get(str(t["id"]), list(t.get("needs") or [])),
        )
        for t in tasks
    ]
    joined = [
        dict(t, needs=list(t.get("needs") or []), surfaces=list(t.get("surfaces") or []))
        for t in added
        if str(t["id"]) not in known
    ]
    return [*joined, *fixed]


def _gate_added_surfaces(
    tasks: list[dict[str, Any]], known: set[str], tree: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The first plan's surface gates, run on tasks whose id is not in `known`; `(tasks, failures)`."""
    checked, failures = _validate_plan_surfaces([t for t in tasks if str(t["id"]) not in known], tree)
    resolved = {str(t["id"]): t for t in _apply_surface_resolutions(checked, tree)}
    merged = [resolved.get(str(t["id"]), t) for t in tasks]
    return _split_cross_repo(merged, tree), [*failures, *_unresolved_pairs(checked, tree)]


def _new_surface_repo(
    task: Mapping[str, Any], surface: str, parts: Sequence[str], tree: Sequence[Mapping[str, Any]]
) -> str:
    """The part a `(new)` surface joins: the task's own repo, else the tree path sharing most parent directories, else the first part."""
    if str(task.get("repo")) in parts:
        return str(task["repo"])
    wanted = surface.removesuffix(_NEW).split("/")[:-1]

    def shared(row: Mapping[str, Any]) -> int:
        have = str(row.get("path")).split("/")[:-1]
        return sum(1 for _ in takewhile(lambda pair: pair[0] == pair[1], zip(wanted, have)))

    best = max((row for row in tree if str(row.get("repo")) in parts), key=shared, default=None)
    return str(best.get("repo")) if best is not None and shared(best) else parts[0]


def _split_cross_repo(tasks: list[dict[str, Any]], tree: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One task per repo when a task's resolved surfaces span more than one tree in the stack.

    A task that never touches more than one repo passes through unchanged. A
    task that was split is replaced everywhere it was named in `needs`, by
    every part it became — a downstream task waited on the whole thing, and
    now the whole thing is more than one task.
    """
    repo_of = {str(row.get("path")): str(row.get("repo")) for row in tree}
    parts: dict[str, list[str]] = {}
    split: list[dict[str, Any]] = []
    for task in tasks:
        repos = sorted({repo_of[s] for s in task.get("surfaces") or [] if s in repo_of})
        if len(repos) <= 1:
            parts[str(task["id"])] = [str(task["id"])]
            split.append(task)
            continue
        new_ids = [f"{task['id']}--{repo}" for repo in repos]
        parts[str(task["id"])] = new_ids
        split.extend(
            dict(
                task,
                id=new_id,
                surfaces=[
                    s
                    for s in task["surfaces"]
                    if repo_of.get(s) == repo
                    or (s not in repo_of and s.endswith(_NEW) and _new_surface_repo(task, s, repos, tree) == repo)
                ],
            )
            for repo, new_id in zip(repos, new_ids, strict=True)
        )
    return [dict(t, needs=[n for need in t["needs"] for n in parts.get(need, [need])]) for t in split]


def _stack_repo_count(tasks: Sequence[Mapping[str, Any]], tree: Sequence[Mapping[str, Any]]) -> int:
    """The number of repos the stack actually touches, read off the tree rather than guessed."""
    if not tree:
        return 1
    repo_of = {str(row.get("path")): str(row.get("repo")) for row in tree}
    repos = {repo_of[s] for t in tasks for s in t.get("surfaces") or [] if s in repo_of}
    return len(repos) or 1


def _prefixed(initiative_id: str, task_id: str) -> str:
    """Scope a task id to its initiative once; an id the model already prefixed is kept."""
    return task_id if task_id.startswith(f"{initiative_id}-") else f"{initiative_id}-{task_id}"


def _apply_challenge(tasks: list[dict[str, Any]], challenge: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Drop edges the adversary showed were not real; add ones it showed were.

    Both directions matter. Dropping a spurious edge buys parallelism; adding a
    missing one prevents a task starting on ground that is not there yet, which
    is the failure the parallelism would otherwise cause.
    """
    by_id = {t["id"]: t for t in tasks}

    for edge in challenge.get("spurious_edges") or []:
        task = by_id.get(str(edge.get("task")))
        if task and str(edge.get("needs")) in task["needs"]:
            task["needs"] = [n for n in task["needs"] if n != str(edge.get("needs"))]

    for edge in challenge.get("missing_edges") or []:
        task, need = by_id.get(str(edge.get("task"))), str(edge.get("needs"))
        # Only between tasks that exist, and never a self-edge — the adversary
        # does not get to invent a task or stall one on itself.
        if task and need in by_id and need != task["id"] and need not in task["needs"]:
            task["needs"].append(need)

    return list(by_id.values())


def _ordinal_needs_problems(tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """One line per `needs` entry that names no id among the tasks the model listed.

    Ordinal mode asks the model for `t1`, `t2`, ... keys instead of full slugs;
    this is the check that a `needs` entry actually lands on one of them before
    anything downstream mints an id or writes a proposal from it.
    """
    ids = {str(t.get("id")) for t in tasks}
    return [
        f"{task.get('id')}: needs unknown key {need!r}"
        for task in tasks
        for need in task.get("needs") or []
        if need not in ids
    ]


def _local_cycle(tasks: list[dict[str, Any]]) -> list[str]:
    """Cheap cycle check before anything is proposed. The store checks again."""
    by_id = {t["id"]: t for t in tasks}
    problems: list[str] = []
    colour: dict[str, int] = dict.fromkeys(by_id, 0)

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = 1
        for need in by_id[node].get("needs") or []:
            if need not in by_id:
                continue
            if colour[need] == 1:
                problems.append(" -> ".join([*trail, need]))
            elif colour[need] == 0:
                visit(need, [*trail, need])
        colour[node] = 2

    for node in sorted(by_id):
        if colour[node] == 0:
            visit(node, [node])
    return problems


def _split_plan(
    decomposition: Mapping[str, Any],
    tasks: list[dict[str, Any]],
    own_listing: Sequence[Mapping[str, Any]],
    repo_trees: Mapping[str, Collection[str]],
    title: str,
) -> tuple[Mapping[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(decomposition, kept tasks, follow-ups, dropped, notes) once tasks in other repositories are moved out.

    The own listing is the whole stack tree, so a stack sibling's file stays here for `_split_cross_repo`;
    `repo_trees` names repositories outside the stack. A surface that is still prose or a directory counts as own
    when it resolves in that listing, as the later resolution would. A phase left with no task is removed.
    """
    if not repo_trees:
        return decomposition, tasks, [], [], []
    paths = [str(row["path"]) for row in own_listing if row.get("path")]
    resolvable = [s for t in tasks for s in t.get("surfaces") or [] if resolve_surfaces([s], own_listing)[0]]
    split = split_foreign(tasks, [*paths, *resolvable] if paths else [], repo_trees, title=title)
    if tasks and not split.kept:
        owners = ", ".join(str(f["repo"]) for f in split.follow_ups)
        raise ContractViolation(
            f"every task in this plan belongs to {owners}; the intake belongs there, not in this repository"
            if owners
            else "no task in this plan touches a file in this repository or a registered one: "
            + "; ".join(str(d["reason"]) for d in split.dropped)
        )
    emptied = {str(t.get("phase")) for t in tasks} - {str(t.get("phase")) for t in split.kept}
    phases = [p for p in decomposition.get("phases") or [] if str(p.get("id")) not in emptied]
    return {**decomposition, "phases": phases}, split.kept, split.follow_ups, split.dropped, split.notes


def unexecuted_item_create_reason(diffs: Sequence[Mapping[str, Any]]) -> str | None:
    """The arm's own refusal, when every `item_create` diff was approved but none applied.

    `None` when there are no `item_create` diffs, or at least one landed —
    the run summary already counts that case correctly. `apply_decisions`
    (harness/gate.py) never passes the arm's free-text detail into
    `gate_diff` (core.manifest) — only `item`, `decision`, `applied`, and
    `edited` cross that boundary — so the refusal named here is the diff's
    own `outcome` field, the word `apply_decisions`'s docstring already
    promises for this case: `"skipped"`.
    """
    item_creates = [d for d in diffs if d.get("kind") == "item_create"]
    if not item_creates:
        return None
    if any(d.get("applied") for d in item_creates):
        return None
    if not all(d.get("decision") == "approved" for d in item_creates):
        return None
    return str(item_creates[0].get("outcome"))


def _phase_of(proposal_: Mapping[str, Any]) -> str:
    return next((str(e.get("output")) for e in proposal_["evidence"] if e.get("check") == "phase"), "")


def approved_task_ids(
    proposals: Sequence[Mapping[str, Any]],
    diffs: Sequence[Mapping[str, Any]],
    auto_applied: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Targets of the task `item_create` proposals that were cleared: approved at the gate or applied by policy.

    `diffs` holds only the gated decisions; a proposal the policy auto-applies never reaches it.
    The initiative's own `item_create` (target `initiative`) is not a task.
    """
    gated = {d.get("target") for d in diffs if d.get("kind") == "item_create" and d.get("decision") == "approved"}
    auto = {p.get("target") for p in auto_applied if p.get("kind") == "item_create"}
    return {
        str(p["target"])
        for p in proposals
        if p.get("kind") == "item_create" and p.get("target") != "initiative" and p.get("target") in gated | auto
    }


def expected_task_files(
    proposals: Sequence[Mapping[str, Any]],
    approved: Collection[str],
    *,
    landing: str,
    initiative_id: str | None,
) -> dict[str, str]:
    """Task id to store-relative file path, for each task id in `approved`.

    The phase is the one the proposal's `phase` evidence carries, which is the phase `_item_action` put in the path.
    """
    return {
        str(p["target"]): "/".join(part for part in (landing, initiative_id, _phase_of(p), f"{p['target']}.md") if part)
        for p in proposals
        if p.get("kind") == "item_create" and p.get("target") in approved
    }


def phase_dirs(
    proposals: Sequence[Mapping[str, Any]],
    *,
    landing: str,
    initiative_id: str | None,
) -> list[str]:
    """Sorted store-relative directory of every task `item_create`, approved or not: it must exist before approval."""
    tasks = {str(p["target"]) for p in proposals if p.get("kind") == "item_create" and p.get("target") != "initiative"}
    paths = expected_task_files(proposals, tasks, landing=landing, initiative_id=initiative_id)
    return sorted({posixpath.dirname(path) for path in paths.values()})


def withdrawal(expected: Collection[str], in_store_before: Collection[str], missing: Collection[str]) -> list[str]:
    """Task ids to withdraw after a failed decompose: those new to this run, so started work keeps its row and file."""
    return sorted(task for task in expected if task not in in_store_before) if missing else []


def decompose_closing(
    expected: Collection[str], in_store: Collection[str], on_disk: Collection[str]
) -> tuple[str, int]:
    """The closing line and the missing count. A task is written only when it is in the store and on disk."""
    missing = sorted(task for task in expected if task not in in_store or task not in on_disk)
    line = f"decompose: {len(expected) - len(missing)} task(s) written, {len(missing)} missing: {', '.join(missing)}"
    return line.rstrip(), len(missing)


def _grant_sentence(grants: Sequence[str]) -> str:
    """The prompt's statement of the build grant, built from the list lint_tickets checks."""
    named = ", ".join(grants)
    return (
        f"A ticket's checks and done-criteria may name only the build grant: {named}. "
        if grants
        else "A ticket's checks and done-criteria may name no command. "
    ) + "No other command may be named."


def _plan_from_output(
    raw: Mapping[str, Any], task_ids: str, initiative_id: Any
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """`(decomposition, reported moves, tasks)` from one decompose output; the same steps for every call.

    Same-phase edges are split, ordinal keys are checked, and ids are scoped to the initiative.
    """
    decomposition, moves = split_same_phase_needs(
        {**raw, "phases": list(raw.get("phases") or []), "tasks": list(raw.get("tasks") or [])}
    )
    reported_moves = (
        [
            dict(m, task=_prefixed(initiative_id, m["task"]), needs=[_prefixed(initiative_id, n) for n in m["needs"]])
            for m in moves
        ]
        if initiative_id
        else moves
    )
    tasks = [
        dict(t, needs=list(t.get("needs") or []), surfaces=list(t.get("surfaces") or []))
        for t in decomposition.get("tasks") or []
    ]
    if not tasks:
        raise ContractViolation("decompose returned no tasks; there is nothing to propose")
    if task_ids == "ordinal":
        ordinal_problems = _ordinal_needs_problems(tasks)
        if ordinal_problems:
            raise ContractViolation(
                "the decomposed graph used a needs key that names no listed task: " + "; ".join(ordinal_problems)
            )
    if initiative_id:
        tasks = [
            dict(t, id=_prefixed(initiative_id, t["id"]), needs=[_prefixed(initiative_id, n) for n in t["needs"]])
            for t in tasks
        ]
    return decomposition, reported_moves, tasks


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. The idea arrives as an argument; nothing is read from disk."""
    cartridge = require_cartridge(args)
    run_id, date, idea = require(args, "run_id", "date", "idea")

    bound = cartridge.get("skills") or {}
    if "decompose" not in bound:
        raise ContractViolation(
            "this graph needs the optional role 'decompose' bound in the cartridge; "
            "a team that has not bound it cannot decompose an initiative"
        )

    source = args.get("decision_source") or NoDecisionSource()
    context = list(cartridge.get("context") or [])

    task_ids = str(args.get("task_ids") or "slug")
    grants = list(args.get("grants") or [])
    tree = list(args.get("tree") or [])
    repo_name = str(args.get("repo") or "")

    # A path the idea itself puts outside the repo is rewritten and noted; only an idea with nothing else is refused, before any model call.
    idea_frontmatter, idea_body = _split_frontmatter(str(idea))
    reach_allow = _reach_allow(args, idea_frontmatter)
    # An empty allow-list is lint_tickets' own default, so it is left unpassed.
    allow_kwargs = {"reach_allow": reach_allow} if reach_allow else {}
    heal = heal_idea_reach(idea_body, tree, repo_name, reach_allow)
    if heal.refusal is not None:
        raise LintRefusal(heal.refusal)
    # The original frontmatter plus the healed body: no rooted path reaches the model or initiative.md.
    planned = _frontmatter_block(str(idea)) + heal.text if heal.notes else str(idea)
    carried = _carry(args, idea_frontmatter)

    decompose_hints = Hints(judgment="high")
    decompose_prompt = (
        f"Break this idea into phases and tasks.\n\nIdea: {planned}\nDate: {date}\n\n"
        "Phases are ordered; tasks within a phase are not necessarily. Draw a "
        "dependency edge ONLY where order genuinely matters — an edge that exists "
        "because the work feels sequential blocks work that could have run in "
        "parallel. Name the surfaces each task touches. When the initiative splits one "
        "interface across repositories, open the plan with a contract task, and give "
        "every task of that interface `repo`, `interface` and `side` (`contract`, "
        "`producer` or `consumer`). The contract task commits a JSON fixture of the "
        "interface in the producer repository; the producer's tests must emit that "
        "fixture exactly and the consumer's tests must parse it. Producer and consumer "
        "tasks list the contract task in `needs`. The dash feed's shared v1 fixture "
        "is the model. "
        + _grant_sentence(grants)
        + " Do not name ruff, cox, uv, gh, grep or any other command in a check line, "
        "a code fence or a done-criterion. State the fact to verify in prose, or leave "
        "it to the reviewer. A ticket's surfaces and body stay inside the repository the "
        "initiative names. A file in another repository is that repository's own task, "
        "written with its own `repo` value, never a surface of this ticket."
    )
    if task_ids == "ordinal":
        decompose_prompt += (
            " Key each task t1, t2, t3, ... in the order you list them, rather than "
            "writing a full id yourself, and write every `needs` entry using those "
            "same t-keys instead of a full slug."
        )
    # The size node is optional like the adversary: unbound, or no intake given,
    # and the graph runs as it always has. A verdict that does not parse, or an
    # override that is not task or epic, is an epic.
    intake = args.get("intake")
    single: dict[str, Any] | None = None
    verdict: SizeVerdict | SizeError | None = None
    if isinstance(intake, Mapping) and "size" in bound:
        repo_files = sorted({str(row.get("path")) for row in args.get("tree") or [] if row.get("path")})
        intake_text = f"{intake.get('title', '')}\n\n{intake.get('body', '')}"
        sized = dict(
            runner.run(
                role="size",
                tier="cheap",
                **_decision(source, "size", None),
                schema=SIZE_SCHEMA,
                context=context,
                prompt=_size_prompt(intake_text, repo_files, _threshold_text(cartridge)),
            )
        )
        verdict = _size_route(sized, intake.get("frontmatter") or {})
        if isinstance(verdict, SizeVerdict) and verdict.verdict == "task":
            built = single_ticket_initiative(dict(intake), verdict, set(repo_files))
            if isinstance(built, SizeError):
                verdict = (
                    built  # the task verdict could not become a ticket: the epic path runs, recorded as a fallback
                )
            else:
                single = built

    size_record = _size_record(verdict)
    if size_record is not None:
        entry = _size_ledger_entry(size_record, run_id=str(run_id), date=str(date), took_task_path=single is not None)
        _append_size_ledger(runner, str(run_id), entry)

    raw = (
        single
        if single is not None
        else dict(
            runner.run(
                role="decompose",
                hints=decompose_hints,
                **_decision(source, "decompose", decompose_hints),
                schema=DECOMPOSE_SCHEMA,
                context=context,
                prompt=decompose_prompt,
            )
        )
    )
    # A deterministic rewrite, not a model call. It runs before the adversary,
    # so the adversary sees the split phases.
    initiative_id = args.get("initiative_id")
    decomposition, reported_moves, tasks = _plan_from_output(raw, task_ids, initiative_id)

    def challenge_edges(phases: Any, plan: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(
            runner.run(
                role="review_adversary",
                tier="deep",
                **_decision(source, "review_adversary", None),
                schema=EDGE_CHALLENGE_SCHEMA,
                context=context,
                prompt=(
                    "Attack this dependency graph. Your job is to find edges that are "
                    "not real — every one of them serialises work that could have run at "
                    "the same time.\n\n"
                    f"Idea: {planned}\nPhases: {phases}\nTasks: {plan}\n\n"
                    "For each edge you challenge, say why the dependency does not "
                    "actually hold. Also name any edge that IS real and is missing."
                ),
            )
        )

    challenge: dict[str, Any] | None = None
    if "review_adversary" in bound and single is None:  # one ticket has no edges to attack
        challenge = challenge_edges(decomposition.get("phases"), tasks)
        tasks = _apply_challenge(tasks, challenge)

    cycles = _local_cycle(tasks)
    if cycles:
        raise ContractViolation(
            "the decomposed graph contains a dependency cycle, so nothing in it could "
            "ever become ready: " + "; ".join(cycles)
        )

    # Own listing: the stack tree when given, else the checkout's tracked files. Read once, reused for named artifacts.
    own_listing = tree or repo_listing(str(args.get("checkout") or repo_name))

    # A task whose files live in another registered repository is moved out before the lint can see a foreign
    # surface: the lint's cross-repo rewrite would send the whole plan back to the model.
    repo_trees = args.get("repo_trees") or {}
    split_title = idea_parts(str(planned))[0]
    decomposition, tasks, follow_ups, dropped_tasks, split_notes = _split_plan(
        decomposition, tasks, own_listing, repo_trees, split_title
    )

    if tree:
        tasks, surface_failures = _validate_plan_surfaces(tasks, tree)
        if surface_failures:
            raise ContractViolation("plan surface not found in the checkout: " + "; ".join(surface_failures))
        problems = _unresolved_pairs(tasks, tree)
        if problems:
            # An unresolved surface is a decompose defect, never a task to
            # discard: nothing is proposed until every surface names a real
            # path, or the run is quarantined for a human to look at.
            if "review_adversary" not in bound:
                raise ContractViolation(surface_problem(problems))
            unbuildable = dict(
                runner.run(
                    role="review_adversary",
                    tier="deep",
                    **_decision(source, "review_adversary", None),
                    schema=UNBUILDABLE_SCHEMA,
                    context=context,
                    prompt=(
                        "These task surfaces are prose, not real paths in the tree. Resolve "
                        "each to the actual path it means.\n\n" + (surface_problem(problems) or "")
                    ),
                )
            )
            tasks = _apply_corrections(tasks, unbuildable.get("corrections") or [])
            problems = _unresolved_pairs(tasks, tree)
            if problems:
                raise ContractViolation(surface_problem(problems))
        tasks = _split_cross_repo(_apply_surface_resolutions(tasks, tree), tree)

    lint_problems = lint_tickets(tasks, tree, grants, repo_name, **allow_kwargs)
    rewrite = [p for p in lint_problems if p.rule in _REWRITE_RULES]
    if rewrite and single is not None:
        # The single ticket came from the intake, not from decompose; a decompose
        # rewrite would replace it with an invented epic, so it keeps its body and carries the problems as notes.
        tasks = _noted(tasks, rewrite, "on the intake's single ticket")
        lint_problems = [p for p in lint_problems if p not in rewrite]
        rewrite = []
    if rewrite:
        # The adversary's corrections cannot reword a body, so decompose rewrites
        # the plan once. Its edges are challenged afresh: the first verdicts
        # named the old plan's edges.
        rewritten = dict(
            runner.run(
                role="decompose",
                hints=decompose_hints,
                **_decision(source, "decompose", decompose_hints),
                schema=DECOMPOSE_SCHEMA,
                context=context,
                prompt=decompose_prompt + "\n\n" + _rewrite_correction(rewrite, grants),
            )
        )
        decomposition, reported_moves, tasks = _plan_from_output(rewritten, task_ids, initiative_id)
        if challenge is not None:
            challenge = challenge_edges(decomposition.get("phases"), tasks)
            tasks = _apply_challenge(tasks, challenge)
        cycles = _local_cycle(tasks)
        if cycles:
            raise ContractViolation(
                "the rewritten graph contains a dependency cycle, so nothing in it could "
                "ever become ready: " + "; ".join(cycles)
            )
        # The rewritten plan replaces the first one outright, foreign tasks included: split it afresh, and let
        # its follow-ups, drops and notes replace the first plan's, which no longer describe anything.
        decomposition, tasks, follow_ups, dropped_tasks, split_notes = _split_plan(
            decomposition, tasks, own_listing, repo_trees, split_title
        )
        if tree:
            tasks, surface_failures = _validate_plan_surfaces(tasks, tree)
            if surface_failures:
                raise ContractViolation("plan surface not found in the checkout: " + "; ".join(surface_failures))
            problems = _unresolved_pairs(tasks, tree)
            if problems:
                raise ContractViolation(surface_problem(problems))
            tasks = _split_cross_repo(_apply_surface_resolutions(tasks, tree), tree)
        lint_problems = lint_tickets(tasks, tree, grants, repo_name, **allow_kwargs)
        # Only the rewrite rules quarantine here; reach, coupling and contract
        # fall through to the adversary round below.
        still = [p for p in lint_problems if p.rule in _REWRITE_RULES]
        if still:
            tasks = _noted(tasks, still, "after the rewrite")
            lint_problems = [p for p in lint_problems if p not in still]
    refusals = [p for p in lint_problems if _is_refusal(p)]
    if refusals and not ("review_adversary" in bound or all(p.rule == "contract" for p in refusals)):
        # No seat can correct reach or coupling here: the problems ride on the tickets for the approval gate.
        tasks = _noted(tasks, refusals, "with no adversary bound to correct them")
        lint_problems = [p for p in lint_problems if p not in refusals]
        refusals = []
    if refusals:
        # reach, coupling and contract block the run, same as an unresolved
        # surface does: quarantine outright with no adversary bound, otherwise
        # ask it for a correction and re-lint before trusting the result. The
        # correction rewrites declared `surfaces`, restates the `needs` of a
        # task already in the plan, and adds new tasks (see _apply_corrections
        # below). It never edits body prose, so a reach hit sourced from the
        # body has nothing to match and quarantines on the second lint. The
        # wrong belief is that a correction only touches `surfaces`: it can
        # now redraw edges and add tasks, so its result goes back through the
        # surface gates and the cycle check before the re-lint.
        #
        # Unbound, a contract refusal alone goes to the decompose seat, once, with the
        # idea and the refused tasks' surfaces: it answers in this block's correction
        # schema, and a correction replaces a surface only on an exact match. Reach and
        # coupling still quarantine outright.
        if "review_adversary" in bound:
            seat, seat_tier, seat_hints, lead = "review_adversary", "deep", None, ""
        elif all(p.rule == "contract" for p in refusals):
            seat, seat_tier, seat_hints = "decompose", None, decompose_hints
            lead = (
                f"Idea: {planned}\n\nYour task list was refused by ticket lint. Do not break the idea down again.\n\n"
            )
        else:  # pragma: no cover - noted above; kept so a new branch cannot fall through unseated
            raise LintRefusal(_lint_refusal_text(refusals))
        correction = dict(
            runner.run(
                role=seat,
                tier=seat_tier,
                hints=seat_hints,
                **_decision(source, seat, seat_hints),
                schema=UNBUILDABLE_SCHEMA,
                context=context,
                prompt=(
                    lead + "Ticket lint refused these tasks for reach, coupling or contract. Resolve "
                    "each so the problem no longer holds. A contract refusal is resolved in "
                    "`tasks`: add the contract task, and restate each task of the interface "
                    "with the contract task in its `needs`.\n\n"
                    + (_lint_refusal_text(refusals) or "")
                    + "\n\n"
                    + _refused_surfaces(tasks, refusals)
                    + "".join(f"\n\n{rule}" for rule in [_contract_fixture_rule(tasks, refusals, tree or [])] if rule)
                ),
            )
        )
        added = list(correction.get("tasks") or [])
        if initiative_id:
            added = [
                dict(
                    t,
                    id=_prefixed(initiative_id, t["id"]),
                    needs=[_prefixed(initiative_id, n) for n in t.get("needs") or []],
                )
                for t in added
            ]
        known = {str(t["id"]) for t in tasks}
        tasks = _apply_corrections(tasks, correction.get("corrections") or [], added)
        if tree:
            tasks, added_failures = _gate_added_surfaces(tasks, known, tree)
            if added_failures:
                raise ContractViolation(
                    "a task the correction added names a surface not in the checkout: " + "; ".join(added_failures)
                )
        cycles = _local_cycle(tasks)
        if cycles:
            raise ContractViolation(
                "the corrected graph contains a dependency cycle, so nothing in it could "
                "ever become ready: " + "; ".join(cycles)
            )
        lint_problems = lint_tickets(tasks, tree, grants, repo_name, **allow_kwargs)
        refusals = [p for p in lint_problems if _is_refusal(p)]
        if refusals:
            tasks = _noted(tasks, refusals, "after the correction")
            lint_problems = [p for p in lint_problems if p not in refusals]

    advisories = [p for p in lint_problems if not _is_refusal(p)]
    if advisories:
        # size never blocks; it lands on the ticket as a `lint:`
        # entry for the human at the gate to see.
        notes: dict[str, list[str]] = {}
        for p in advisories:
            notes.setdefault(p.task, []).append(f"{p.rule}: {p.detail} ({p.fix})")
        tasks = [
            dict(t, lint=[*(t.get("lint") or []), *notes[str(t["id"])]]) if str(t["id"]) in notes else t for t in tasks
        ]

    # The seat's own structured output sometimes copies an advisory sentence
    # into `surfaces` after seeing one in a retry prompt (build-output-valid,
    # limit-pause). Same path shape as lifecycle_propose._PATH_TOKEN: a `/` or
    # a file suffix, no spaces, optional trailing `(new)`; an all-caps root
    # file like LICENSE has neither and is kept on that alone. What doesn't
    # look like a path moves to `lint` here, the one place every task passes.
    tasks = [
        dict(
            t,
            surfaces=[s for s in t.get("surfaces") or [] if _looks_like_a_surface(str(s))],
            lint=[
                *(t.get("lint") or []),
                *(f"dropped from surfaces: {s}" for s in t.get("surfaces") or [] if not _looks_like_a_surface(str(s))),
            ],
        )
        for t in tasks
    ]

    # This plan's own task ids are the only valid `needs` targets — a foreign
    # id names real work, just ordered by landing rather than by an edge.
    tasks = _drop_foreign_needs(tasks)

    # A test, migration or script the body names but surfaces omit is the most common layout refusal, and the fix is
    # mechanical when the name points at one file: add it, and say so on the ticket.
    # Launches pass no --tree, so a bare test name could never resolve; the checkout's own listing stands in, here only.
    # `repo` (--target-repo) is often unset on a launch; `checkout` is the harness's own --repo, the seats' readable copy.
    named_tree = own_listing
    tasks = [with_named_artifacts(t, named_tree) for t in tasks]

    # The layout check never stops a decompose: a body that does not match its surfaces is healed (the unnamed
    # surface goes on a `Surfaces:` line, a named artifact joins the surfaces) and the repair rides as a `lint:`
    # entry; an edge with no data flow in the body only lands as a `lint:` entry.
    # The missing-marker heal rides with it: a surface absent from the tree gets `(new)` and a `lint:` entry.
    # An empty tree says nothing about what exists, so the heal is skipped and no surface is marked new.
    # heal_layout can add a body-named test by its bare name. heal_bare_filenames writes its repository path, or leaves
    # it out with a lint note, and runs on an empty tree too, where only a body-named path can resolve it.
    tree_paths = frozenset(str(row["path"]) for row in named_tree if row.get("path"))
    tasks = [heal_bare_filenames(heal_layout(t), tree_paths) for t in tasks]
    tasks = [heal_new_markers(t, tree_paths) for t in tasks] if tree_paths else tasks
    layout = check_layout(tasks)
    layout_notes: dict[str, list[str]] = {}
    for f in layout.findings:
        if f.rule != "edge_unsupported":
            continue  # the other advisories ride in the outcome's `layout`, not on every ticket
        layout_notes.setdefault(f.task, []).append(f"{f.rule}: {f.detail} ({f.fix})")
    tasks = [
        dict(t, lint=[*(t.get("lint") or []), *layout_notes[str(t["id"])]]) if str(t["id"]) in layout_notes else t
        for t in tasks
    ]
    tasks = [notes_out_of_surfaces(t) for t in tasks]

    # A generated file whose generator source is a surface changes with it. check_layout flags a surface the body
    # does not name and this one is never named, so the step runs after the layout check, before surfaces are read.
    tasks = [with_generated_files(t) for t in tasks]

    # Validated before anything is proposed: a typo written the same way in
    # both the task and the plan reaches the work-item arm looking
    # consistent, and the arm rightly refuses to fix it silently. A
    # near-miss that matches more than one declared phase is not corrected
    # either — that is a guess, not a fix — so the node fails here and its
    # existing retry runs.
    phase_order = [str(p.get("id")) for p in decomposition.get("phases") or []]
    resolved_tasks = []
    for task in tasks:
        match = _resolve_phase(str(task.get("phase")), phase_order)
        if match is None:
            raise ContractViolation(
                f"task {task['id']} names phase {task.get('phase')!r}, which is not within "
                f"edit distance {_PHASE_EDIT_DISTANCE_BUDGET} of exactly one declared phase "
                f"({', '.join(phase_order) or 'none declared'})"
            )
        resolved_phase, note = match
        resolved_tasks.append(dict(task, phase=resolved_phase, phase_correction=note) if note else task)
    tasks = resolved_tasks
    tasks, ordering_notes = order_shared_surfaces(tasks)
    tasks, cross_phase_notes = order_cross_phase_surfaces(tasks, phase_order)
    ordering_notes = [*ordering_notes, *cross_phase_notes]

    shape = epic_shape(
        cartridge,
        phases=len({t["phase"] for t in tasks}),
        tickets=len(tasks),
        repos=_stack_repo_count(tasks, tree),
    )
    landing = landing_for(cartridge, "planned")

    goals = {str(p.get("id")): str(p.get("goal") or "") for p in decomposition.get("phases") or []}
    idea_title, idea_repo, idea_body = idea_parts(planned)
    idea_doc = {
        "id": initiative_id or run_id,
        "title": idea_title,
        "budget_usd": args.get("budget_usd"),
        "why": idea_body,
        **({"repo": idea_repo} if idea_repo else {}),
    }
    intake_path = args.get("intake_path")
    initiative_body = initiative_text(
        idea_doc,
        phase_order,
        goals,
        str(args.get("repo") or idea_doc.get("repo") or ""),
        intake=intake_path,
        size=size_record,
        home=str(Path.home()),
        epic=carried.get("epic"),
        target_branch=carried.get("target_branch"),
        lint=[*heal.notes, *ordering_notes, *split_notes],
    )
    initiative_where = "/".join(part for part in (landing, initiative_id) if part)
    # One cleaned body per task, bound before the proposals and the returned plan read it, so the
    # rationale, `apply.item.body` and the plan task cannot differ.
    tasks = [
        {**t, "body": _strip_trailing_tag(str(t.get("body") or decomposition.get("rationale", "")))} for t in tasks
    ]
    # The item's body is the text after the frontmatter, not `initiative_body`: that string
    # already carries its own `---` block, and the store writes the frontmatter itself.
    initiative_apply = {
        "path": f"{initiative_where}/initiative.md",
        "item": {
            **initiative_frontmatter(
                idea_doc,
                str(args.get("repo") or idea_doc.get("repo") or ""),
                intake=intake_path,
                size=size_record,
                home=str(Path.home()),
                epic=carried.get("epic"),
                target_branch=carried.get("target_branch"),
                lint=[*heal.notes, *ordering_notes, *split_notes],
            ),
            "body": initiative_body_text(idea_doc, phase_order, goals),
        },
    }

    proposals = (
        [
            proposal(
                cartridge,
                kind="item_create",
                target=str(task["id"]),
                evidence=[
                    {"check": "phase", "output": str(task.get("phase"))},
                    *(
                        [{"check": "phase correction", "output": task["phase_correction"]}]
                        if task.get("phase_correction")
                        else []
                    ),
                    {"check": "depends on", "output": ", ".join(task["needs"]) or "nothing — can start immediately"},
                    {"check": "surfaces", "output": ", ".join(task.get("surfaces") or []) or "none declared"},
                    *(
                        [
                            {
                                "check": "adversary on the DAG",
                                "output": "adversary edges applied: " + str(challenge.get("summary")),
                            }
                        ]
                        if challenge
                        else []
                    ),
                    *([{"check": "lint", "output": "; ".join(task.get("lint") or [])}] if task.get("lint") else []),
                    *({"check": "surface corrected", "output": fix} for fix in task.get("surface_fixes") or []),
                    *(
                        {
                            "check": "needs dropped",
                            "output": f"{task['id']}: dropped {dropped!r}, not a task id in this plan",
                        }
                        for dropped in task.get("needs_dropped") or []
                    ),
                ],
                rationale=task["body"],
                # The whole item, in the action. The arm sees the proposal and
                # nothing else, so an action that named only an id would leave it
                # inventing the title and guessing the initiative — the first live
                # run proved exactly that. `initiative_id` is optional: absent, the
                # store root's name is the initiative, as `read_initiative` reads it.
                suggested_action=_item_action(task, landing=landing, initiative_id=initiative_id),
                apply=_task_apply(task, task["body"], landing=landing, initiative_id=initiative_id),
            )
            for task in sorted(tasks, key=lambda t: str(t["id"]))
        ]
        + [
            proposal(
                cartridge,
                kind="item_create",
                target="initiative",
                evidence=[{"check": "phases", "output": ", ".join(phase_order) or "none"}],
                rationale=str(decomposition.get("rationale") or ""),
                suggested_action=f"create {initiative_where}/initiative.md with body =\n{initiative_body}",
                apply=initiative_apply,
            )
        ]
        + (
            [
                proposal(
                    cartridge,
                    kind="state_move",
                    target=intake_path,
                    evidence=[{"check": "initiative", "output": f"{initiative_where}/initiative.md"}],
                    rationale="the initiative now exists; the intake file it came from retires out of the queue",
                    suggested_action=(
                        f"call `cox route file --from-intake {intake_path}` if that CLI is present; otherwise "
                        f"set {intake_path}'s frontmatter to `initiative: {initiative_id or run_id}` and move it "
                        f"to `intake/done/{intake_path.rsplit('/', 1)[-1]}`"
                    ),
                )
            ]
            if intake_path
            else []
        )
    )

    unblocked = [t["id"] for t in tasks if not t["needs"]]
    return {
        "run_id": run_id,
        "date": date,
        "idea": idea,
        "shape": shape,
        "phases": decomposition.get("phases") or [],
        "tasks": sorted(tasks, key=lambda t: str(t["id"])),
        "moves": reported_moves,
        "challenge": challenge,
        "layout": layout.as_outcome(),
        "follow_ups": follow_ups,
        "dropped": dropped_tasks,
        "proposals": proposals,
        "totals": {
            "phases": len({t["phase"] for t in tasks}),
            "tasks": len(tasks),
            "edges": sum(len(t["needs"]) for t in tasks),
            "edges_dropped": len((challenge or {}).get("spurious_edges") or []),
            "edges_added": len((challenge or {}).get("missing_edges") or []),
            "immediately_startable": len(unblocked),
        },
    }


def _strip_trailing_tag(body: str) -> str:
    """Drop a trailing closing tag on its own line — the seat's own output wrapper, not the ticket."""
    return re.sub(r"\n?</[^\n>]+>\s*$", "", body)


def _task_path(task: Mapping[str, Any], *, landing: str, initiative_id: str | None) -> str:
    """Pure: the store-relative ticket path, landing, initiative id when set, phase, then `<id>.md`."""
    where = "/".join(part for part in (landing, initiative_id, str(task.get("phase"))) if part)
    return f"{where}/{task['id']}.md"


def _task_apply(task: Mapping[str, Any], rationale: str, *, landing: str, initiative_id: str | None) -> dict[str, Any]:
    """Pure: the payload the workstore arm writes, the action's frontmatter plus the rationale as body."""
    return {
        "path": _task_path(task, landing=landing, initiative_id=initiative_id),
        "item": {**_task_frontmatter(task), "body": rationale},
    }


def _item_action(task: Mapping[str, Any], *, landing: str, initiative_id: str | None) -> str:
    """Pure: the create action, carrying every field the work-item arm must write."""
    # Real YAML, not a hand-quoted guess: the arm copies this block verbatim
    # into the ticket's frontmatter, and a title carrying `: ` broke exactly
    # that the first time nothing here quoted it.
    block = yaml.safe_dump(_task_frontmatter(task), sort_keys=False, allow_unicode=True, width=1000)
    path = _task_path(task, landing=landing, initiative_id=initiative_id)
    return f"create {path} with frontmatter\n{block}body = the rationale"


def _task_frontmatter(task: Mapping[str, Any]) -> dict[str, Any]:
    """Pure: the ticket frontmatter the prose action renders and the apply payload writes."""
    # Empty stays `[]`, never a placeholder word: the arm copies this text into
    # frontmatter verbatim, and `needs: [none]` is an edge to a task that does
    # not exist — the third live run landed exactly that and the DAG refused.
    # Ticket lint's grant/size warnings land here too — docs/design/work-shape.md
    # §3 puts them in the ticket's own `lint:` frontmatter list. A retried seat
    # can see the same advisory more than once; dedupe keeps it to one.
    task = notes_out_of_surfaces(task)
    return {
        "id": task["id"],
        "title": task.get("title") or task["id"],
        "phase": task.get("phase"),
        "state": "ready",
        "needs": [str(n) for n in task.get("needs") or []],
        "surfaces": [str(x) for x in task.get("surfaces") or []],
        "lint": list(dict.fromkeys(str(x) for x in task.get("lint") or [])),
        # Only a one-ticket initiative carries checks; an epic task writes none, as before.
        **({"checks": [str(x) for x in task["checks"]]} if task.get("checks") else {}),
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="decompose",
    graph_name=GRAPH_NAME,
    run=run,
    summary="an idea into phases and a task DAG, with the edges attacked before anyone trusts them",
    needs=(
        Need(
            "idea", flag="--idea", kind="text_or_path", help="the initiative, as prose or a path to a file holding it"
        ),
        Need(
            "initiative_id",
            flag="--initiative-id",
            required=False,
            help="directory name for the initiative under the work store (default: the store root itself)",
        ),
        Need(
            "task_ids",
            flag="--task-ids",
            required=False,
            choices=("ordinal",),
            help="`ordinal` names the graph's own tasks <initiative-id>-t1, -t2, ... (default: slugs from titles)",
        ),
        Need(
            "repo",
            flag="--target-repo",
            required=False,
            help="the repository the initiative targets, for initiative.md's frontmatter",
        ),
        Need(
            "budget_usd",
            flag="--budget-usd",
            required=False,
            help="the initiative's budget in dollars, for initiative.md's frontmatter",
        ),
        Need(
            "tree",
            flag="--tree",
            kind="jsonl_file",
            required=False,
            help="rows of {repo, path} across the target repo(s); resolves task surfaces to "
            "real paths and derives the stack (default: surfaces are not resolved)",
        ),
        Need(
            "intake_path",
            flag="--from-intake",
            required=False,
            help="path to the intake file this idea came from, when it did; links initiative.md "
            "and the intake file both ways",
        ),
        Need(
            "intake",
            flag="--intake-json",
            kind="json_file",
            required=False,
            help="JSON object {id, title, body, checks, frontmatter} of the intake; with the `size` role "
            "bound, a one-file intake becomes a one-ticket initiative (default: always decompose)",
        ),
        Need(
            "grants",
            flag="--grants",
            kind="json_file",
            required=False,
            help="JSON array of commands the build seat's sandbox actually permits, for the "
            "ticket-lint grant rule (default: none granted)",
        ),
        Need(
            "repo_trees",
            flag="--repo-trees",
            kind="json_file",
            required=False,
            help="JSON object mapping a repository path to the list of that repository's tracked file paths; "
            "a task whose files live in one of them is returned as a follow-up (default: no split)",
        ),
    ),
)
