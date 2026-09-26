"""The local work store: phases and tasks as files, with no tracker anywhere.

The other I/O edge, beside `ledger.py`. Graphs never touch it — the shell reads
it and passes items in as arguments, exactly as alerts and observed state are
passed today.

    work/<initiative>/initiative.md       the idea, its phases, why
    work/<initiative>/<phase>/<task>.md   frontmatter + prose

Markdown with YAML frontmatter, on purpose. A work item is mostly prose — what
someone picking this up cold needs to know — and the structured part is small
enough to sit in a header. It diffs, it reviews in a pull request, and git is
already the audit trail, so there is no second system to keep honest.

    ---
    id: t2-bench-harness
    phase: p1-foundations
    state: ready
    needs: [t1-schema-probe]
    surfaces: [schema]
    title: Benchmark harness for the vendor join
    priority: 2
    ---

    Prose describing the work.

`state` is one of todo | ready | in_progress | blocked | done, and the
`work_state_arm` is its single writer — the single-writer conviction survives
having no tracker, because the conviction was never about the tracker.

`approved` sits between `in_progress` and `done`: the task's build was
reviewed and approved but its merge was escalated to a person. It is NOT
ready — no run may pick it up — and NOT done — it does not complete its
phase or initiative, the `done + dropped` completion rule from #58 is
unchanged — and the land/recover tooling is what moves it on to `done`.

`priority` is 1..5, 1 highest; an item that omits it defaults to 3. It is
validated like `budget_usd`. Reading exposes it on every parsed item, never
absent, unlike `budget_usd`. Writing omits the key once it equals the
default, the same way `budget_usd` omits a value that is not meaningful, so
a legacy item without `priority:` still saves with no such line.

`verify` is a list of shell commands, or a single command string. It is read
and kept: `read_item` exposes it on every item and `write_item` writes it back,
so a state move never erases it. It is empty when absent. Any other shape
raises `WorkStoreError`.

`tier` maps a role name to `cheap`, `standard` or `deep`, so one ticket can
elevate a role without touching the provider profile. `read_item` exposes it
on every item, `{}` when absent. `write_item` writes it back when non-empty,
so a state move never erases it. Any other shape raises `WorkStoreError`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "STATES",
    "WorkStoreError",
    "foreign_states",
    "phase_complete",
    "phases",
    "read_initiative",
    "read_item",
    "ready_tasks",
    "record_attempt",
    "resolve_surfaces",
    "set_state",
    "surface_problem",
    "validate_dag",
    "write_item",
]

STATES = ("todo", "ready", "in_progress", "approved", "blocked", "done", "dropped")
DONE = "done"
DROPPED = "dropped"
APPROVED = "approved"
_TERMINAL = frozenset({DONE, DROPPED})
_INTO_APPROVED = frozenset({"ready", "in_progress"})
_OUT_OF_APPROVED = frozenset({"done", "ready"})
DEFAULT_PRIORITY = 3
ATTEMPT_KINDS = ("refused", "no_work", "unverified", "infra")
TIERS = ("cheap", "standard", "deep")

_FRONTMATTER = "---"
_NEW_SUFFIX = " (new)"


class WorkStoreError(Exception):
    """The work store on disk could not be read, or describes something impossible."""


def _split_frontmatter(text: str, source: Path) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER:
        raise WorkStoreError(f"{source}: work items must open with a '---' frontmatter block")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == _FRONTMATTER)
    except StopIteration:
        raise WorkStoreError(f"{source}: frontmatter block is never closed") from None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise WorkStoreError(f"{source}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, Mapping):
        raise WorkStoreError(f"{source}: frontmatter must be a mapping")
    return dict(meta), "\n".join(lines[end + 1 :]).strip()


def _coerce_attempts(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    return [
        {
            "run": str(entry.get("run") or ""),
            "phase": str(entry.get("phase") or ""),
            "reason": str(entry.get("reason") or ""),
            "ts": str(entry.get("ts") or ""),
            **({"kind": str(entry["kind"])} if entry.get("kind") else {}),
            **({"patch_kept": True} if entry.get("patch_kept") else {}),
            **({"body_sha": str(entry["body_sha"])} if entry.get("body_sha") else {}),
        }
        for entry in raw
        if isinstance(entry, Mapping)
    ]


def _coerce_patterns(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw]


def _coerce_verify(raw: Any, path: Path | str) -> list[str]:
    """Stripped non-blank command strings; absent is []. Anything but a string or list of strings raises."""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not all(isinstance(c, str) for c in items):
        raise WorkStoreError(f"{path}: verify must be a string or a list of strings, got {raw!r}")
    return [c.strip() for c in items if c.strip()]


def _coerce_tier(raw: Any, path: Path | str) -> dict[str, str]:
    """Role -> tier map; absent is {}. A non-mapping, blank role, or tier outside TIERS raises."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise WorkStoreError(f"{path}: tier must be a mapping of role to tier, got {raw!r}")
    bad_role = [k for k in raw if not isinstance(k, str) or not k.strip()]
    if bad_role:
        raise WorkStoreError(f"{path}: tier roles must be non-empty strings, got {bad_role[0]!r}")
    bad_tier = [v for v in raw.values() if v not in TIERS]
    if bad_tier:
        raise WorkStoreError(f"{path}: tier must be one of {list(TIERS)}, got {bad_tier[0]!r}")
    return {k.strip(): v for k, v in raw.items()}


def _coerce_budget_usd(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _coerce_priority(raw: Any) -> int:
    """1..5, 1 highest. Anything else — absent, non-numeric, or out of range — is the default."""
    if isinstance(raw, bool):
        return DEFAULT_PRIORITY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY
    return value if 1 <= value <= 5 else DEFAULT_PRIORITY


def resolve_surfaces(surfaces: Sequence[str], tree: frozenset[str]) -> tuple[list[str], list[str]]:
    """Split surfaces into those that are real paths in `tree` and those that are not."""
    resolved: list[str] = []
    unresolved: list[str] = []
    for surface in surfaces:
        is_new = surface.endswith(_NEW_SUFFIX)
        path = surface[: -len(_NEW_SUFFIX)] if is_new else surface
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        under_dir = bool(tree) if not parent else any(p == parent or p.startswith(f"{parent}/") for p in tree)
        if path in tree or (is_new and under_dir):
            resolved.append(surface)
        else:
            unresolved.append(surface)
    return resolved, unresolved


def surface_problem(unresolved: Sequence[str]) -> str | None:
    """The one-line refusal both callers print; None when every surface resolved."""
    if not unresolved:
        return None
    return "surfaces do not resolve against the checkout: " + ", ".join(unresolved)


def read_item(path: Path | str) -> dict[str, Any]:
    """Read one work item. Its `id` defaults to the filename, never invented."""
    path = Path(path)
    try:
        meta, body = _split_frontmatter(path.read_text(encoding="utf-8"), path)
    except OSError as exc:
        raise WorkStoreError(f"cannot read work item {path}: {exc}") from exc

    budget_usd = _coerce_budget_usd(meta.get("budget_usd"))
    item = {
        "id": str(meta.get("id") or path.stem),
        "phase": str(meta.get("phase") or path.parent.name),
        "state": str(meta.get("state") or "todo"),
        "needs": [str(n) for n in (meta.get("needs") or [])],
        "surfaces": [str(s) for s in (meta.get("surfaces") or [])],
        "patterns": _coerce_patterns(meta.get("patterns")),
        "verify": _coerce_verify(meta.get("verify"), path),
        "tier": _coerce_tier(meta.get("tier"), path),
        "title": str(meta.get("title") or path.stem),
        "attempts": _coerce_attempts(meta.get("attempts")),
        **({"budget_usd": budget_usd} if budget_usd is not None else {}),
        "priority": _coerce_priority(meta.get("priority")),
        "body": body,
        "path": str(path),
    }
    if item["state"] not in STATES:
        raise WorkStoreError(f"{path}: unknown state '{item['state']}'; expected one of {list(STATES)}")
    return item


def write_item(item: Mapping[str, Any], path: Path | str) -> Path:
    """Write one work item, frontmatter first. Round-trips through `read_item`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    attempts = list(item.get("attempts") or [])
    budget_usd = _coerce_budget_usd(item.get("budget_usd"))
    patterns = _coerce_patterns(item.get("patterns"))
    verify = _coerce_verify(item.get("verify"), path)
    tier = _coerce_tier(item.get("tier"), path)
    priority = _coerce_priority(item.get("priority"))
    meta = {
        "id": item["id"],
        "phase": item.get("phase", ""),
        "state": item.get("state", "todo"),
        "needs": list(item.get("needs") or []),
        "surfaces": list(item.get("surfaces") or []),
        **({"patterns": patterns} if patterns else {}),
        **({"verify": verify} if verify else {}),
        **({"tier": tier} if tier else {}),
        "title": item.get("title", item["id"]),
        **({"budget_usd": budget_usd} if budget_usd is not None else {}),
        **({"priority": priority} if priority != DEFAULT_PRIORITY else {}),
        **({"attempts": attempts} if attempts else {}),
    }
    if meta["state"] not in STATES:
        raise WorkStoreError(f"unknown state '{meta['state']}'; expected one of {list(STATES)}")
    front = yaml.safe_dump(meta, sort_keys=False, default_flow_style=None).strip()
    path.write_text(f"{_FRONTMATTER}\n{front}\n{_FRONTMATTER}\n\n{item.get('body', '').strip()}\n", encoding="utf-8")
    return path


def body_sha(body: str) -> str:
    """First 12 hex characters of the sha256 of the stripped body."""
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:12]


def attempts_on_current_body(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Attempts made on the item's current text. An attempt with no `body_sha` predates stamping and counts."""
    current = body_sha(item["body"])
    return [a for a in item["attempts"] if a.get("body_sha", current) == current]


def record_attempt(
    path: Path | str,
    *,
    run: str,
    phase: str,
    reason: str,
    ts: str,
    kind: str | None = None,
    patch_kept: bool = False,
) -> dict[str, Any]:
    """Append one attempt to an item's history, preserving everything else.

    The caller supplies `ts`; this module reads no clock.
    """
    if kind is not None and kind not in ATTEMPT_KINDS:
        raise WorkStoreError(f"unknown attempt kind '{kind}'; expected one of {list(ATTEMPT_KINDS)}")
    item = read_item(path)
    entry = {
        "run": str(run),
        "phase": str(phase),
        "reason": str(reason),
        "ts": str(ts),
        "body_sha": body_sha(item["body"]),
    }
    if kind is not None:
        entry["kind"] = kind
    if patch_kept:
        entry["patch_kept"] = True
    write_item({**item, "attempts": [*item["attempts"], entry]}, path)
    return read_item(path)


def set_state(path: Path | str, state: str) -> dict[str, Any]:
    """Move one item's state, preserving everything else. The single writer's move."""
    if state not in STATES:
        raise WorkStoreError(f"unknown state '{state}'; expected one of {list(STATES)}")
    item = read_item(path)
    current = item["state"]
    if state == APPROVED and current not in _INTO_APPROVED:
        raise WorkStoreError(f"cannot move '{current}' -> 'approved'; only ready or in_progress may")
    if current == APPROVED and state != APPROVED and state not in _OUT_OF_APPROVED:
        raise WorkStoreError(f"cannot move 'approved' -> '{state}'; only done or ready may follow approved")
    item["state"] = state
    write_item(item, path)
    return item


def read_initiative(root: Path | str) -> dict[str, Any]:
    """Read an initiative and every task under it, validating the DAG before returning.

    Validation happens HERE, not at execution: a cycle discovered halfway
    through a phase has already burned half the work, and the whole point of
    decomposing first is to find that kind of problem while it is still cheap.
    """
    root = Path(root)
    if not root.is_dir():
        raise WorkStoreError(f"no initiative at {root}")

    overview = root / "initiative.md"
    meta: dict[str, Any] = {}
    body = ""
    if overview.is_file():
        meta, body = _split_frontmatter(overview.read_text(encoding="utf-8"), overview)

    items = [read_item(p) for p in sorted(root.glob("*/*.md")) if p.name != "initiative.md"]
    own = {str(item["id"]) for item in items}
    dangling = any(need not in own for item in items for need in item["needs"])
    foreign = foreign_states(root.parent, root.name) if dangling else {}
    validate_dag(items, foreign)

    return {
        "id": str(meta.get("id") or root.name),
        "title": str(meta.get("title") or root.name),
        "body": body,
        "root": str(root),
        "phases": phases(items),
        "items": items,
        "foreign": foreign,
    }


def foreign_states(store_root: Path | str, own_initiative: str) -> dict[str, str]:
    """Task id to state for every task in an initiative other than `own_initiative`."""
    return {
        item["id"]: item["state"]
        for item in (
            read_item(p)
            for p in sorted(Path(store_root).glob("*/*/*.md"))
            if p.parts[-3] != own_initiative and p.name != "initiative.md"
        )
    }


def phases(items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Phase names, in order. Ordering is the phase name's job — prefixes stay short."""
    return sorted({str(item.get("phase") or "") for item in items} - {""})


def phase_complete(items: Sequence[Mapping[str, Any]], phase: str) -> bool:
    """A phase is complete once every one of its items is done or dropped."""
    return all(item.get("state") in _TERMINAL for item in items if item.get("phase") == phase)


def validate_dag(items: Iterable[Mapping[str, Any]], foreign: Mapping[str, str] | None = None) -> None:
    """Refuse a dangling edge or a cycle. Loudly, and listing every problem.

    `foreign` maps ids of tasks in other initiatives to their state. A need
    found there is not dangling, and the cycle walk never enters it.
    """
    items = list(items)
    foreign = foreign or {}
    by_id = {str(item["id"]): item for item in items}

    problems = [
        f"'{item['id']}' needs '{need}', which does not exist"
        for item in items
        for need in item.get("needs") or []
        if need not in by_id and need not in foreign
    ]
    duplicates = sorted({i["id"] for i in items if sum(1 for j in items if j["id"] == i["id"]) > 1})
    problems.extend(f"duplicate work item id '{dup}'" for dup in duplicates)

    # Depth-first cycle detection. A cycle is not a scheduling inconvenience —
    # it means nothing in it can ever become ready, so the phase would simply
    # stall with no explanation.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(by_id, WHITE)

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = GREY
        for need in by_id[node].get("needs") or []:
            if need not in by_id:
                continue
            if colour[need] == GREY:
                loop = trail[trail.index(need) :] if need in trail else [need]
                problems.append("dependency cycle: " + " -> ".join([*loop, need]))
            elif colour[need] == WHITE:
                visit(need, [*trail, need])
        colour[node] = BLACK

    for node in sorted(by_id):
        if colour[node] == WHITE:
            visit(node, [node])

    if problems:
        raise WorkStoreError(
            f"work store DAG is invalid ({len(problems)} problem(s)):\n  - " + "\n  - ".join(sorted(set(problems)))
        )


def ready_tasks(
    items: Sequence[Mapping[str, Any]], *, phase: str | None = None, foreign: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Every task whose dependencies are all done, and which is not itself finished.

    A need in another initiative (`foreign`) is met only by state `done`; `dropped` does not satisfy it.

    This is the parallelism: whatever comes back can run at the same time,
    because nothing in the set depends on anything else in it. An edge that
    exists only because the work "feels sequential" costs exactly this — it
    keeps a task out of this list for no reason.
    """
    own = {str(item["id"]) for item in items}
    done = {str(item["id"]) for item in items if item.get("state") in _TERMINAL}
    done |= {tid for tid, state in (foreign or {}).items() if state == DONE and tid not in own}
    return sorted(
        (
            dict(item)
            for item in items
            if item.get("state") not in _TERMINAL
            and (phase is None or item.get("phase") == phase)
            and all(need in done for need in item.get("needs") or [])
        ),
        key=lambda item: (_coerce_priority(item.get("priority")), str(item["id"])),
    )
