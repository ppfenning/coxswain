"""The routing core, lint and merges: the problems a filed initiative can have, and merging same-phase tasks."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from itertools import accumulate
from pathlib import Path
from typing import NamedTuple

from agent_tools import repo_path, run_store
from agent_tools.route_profile import _frontmatter, _header_value, _Raw


class Problem(NamedTuple):
    """work-shape.md §3: one static-lint finding."""

    task: str
    rule: str
    detail: str
    fix: str


# The character before an unsafe path may be whitespace, start-of-string, a
# backtick, an opening parenthesis or a quote — this project's own house
# style wraps every file reference in backticks, so a bare `\s` boundary
# misses a real out-of-repo path inside a code span.
_UNSAFE_PATH = re.compile(r"(?:^|[\s\x60\x28\x27\x22])(/[\w./-]+|workspace/[\w./-]*|~/[\w./-]*)")
_DISALLOWED_COMMANDS = ("cox", "uv", "gh", "git push", "ruff")
# Top-level directories a real absolute path starts with (Linux FHS, macOS, Nix), as graphs' ticket_lint
# `_FS_ROOTS`: a rooted token whose first segment is none of these (`/v1/chat/completions`) is a route, not a file.
_FS_ROOTS = frozenset({
    "bin", "boot", "etc", "home", "lib", "lib64", "media", "mnt", "nix", "opt", "private", "proc", "root", "run",
    "sbin", "snap", "srv", "sys", "tmp", "Users", "usr", "var", "Volumes",
})


# Path prefixes only one Coxswain repository has, by repository basename. `docs/` and `tests/` exist in every
# repository, so they are deliberately absent: naming one is never a cross-repository reference. `coxswain` is the
# monorepo, whose components sit under `src/`; the old component names stay for tickets on an unmigrated checkout.
# Bare `src/` is absent too (other repositories have one): only the monorepo's own component folders count.
_REPO_ROOTS: dict[str, frozenset[str]] = {
    "coxswain-tools": frozenset({"agent_tools"}),
    "coxswain-graphs": frozenset({"harness", "runner", "graphs"}),
    "coxswain-cartridges": frozenset({"core", "providers", "cartridges", "skills-plugins"}),
    "coxswain": frozenset({"src/tools", "src/graphs", "src/cartridges", "devtools", "crew"}),
    "coxswain-plugins": frozenset({"coxswain_plugins"}),
}
_PATH_EDGE = "\x60\x27\x22(),."


def _is_route(path: str) -> bool:
    return path.startswith("/") and path.split("/")[1] not in _FS_ROOTS


def _inside_repo(path: str, repo: str, home: str | None = None) -> bool:
    """§3 excludes a path only when it is inside `repo`; wrong belief this
    guards: that any absolute/workspace/~ path is automatically reach-unsafe.
    A path is a prefix match on `repo` (a filesystem root, not a slug) —
    plain-data comparison, no filesystem read. With `home`, both sides are
    contracted first, so `~/r/x/a.py` and its absolute spelling are both inside `~/r/x`.
    """
    if home is not None:
        path, repo = repo_path.contract(path, home), repo_path.contract(repo, home) if repo else repo
    root = repo.rstrip("/") if repo else ""
    return bool(root) and (path == root or path.startswith(root + "/"))


def _home_paths(text: str, home: str) -> list[str]:
    """Rooted tokens of `text` spelled absolutely under a home directory, first sight, no repeats."""
    rooted = (p for p in _UNSAFE_PATH.findall(text) if p.startswith("/"))
    return list(dict.fromkeys(p for p in rooted if repo_path.contract(p, home) != p))


def _cross_repo_paths(text: str, repo: str | None) -> list[tuple[str, str]]:
    """`(path, owning repository)` for each relative token under another repository's own path prefix.
    Stands down when `repo` is unset or its basename is not in `_REPO_ROOTS`. Order of first sight, no repeats."""
    home = os.path.basename(repo.rstrip("/")) if repo else ""
    if home not in _REPO_ROOTS:
        return []
    owner = {top: name for name, tops in _REPO_ROOTS.items() if name != home for top in tops}
    paths = dict.fromkeys(t.strip(_PATH_EDGE) for t in text.split())
    owned = ((p, next((owner[t] for t in owner if p.startswith(t + "/")), None)) for p in paths)
    return [(p, name) for p, name in owned if name is not None]


# A label opening a run context: a checks list or a Done line, at line start or after a sentence end.
_RUN_LABEL = re.compile(r"(?:^|(?<=[.!?]\s))[\s*#_-]*(?:checks|check(?=:)|done(?:\s+when)?|acceptance(?:\s+(?:checks|criteria))?)(?![\w-])", re.I)
_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s")
_RUN_VERB = re.compile(r"\b(?:run|runs|execute|invoke)\s+(?:(?:the|a|an)\s+)?[\x60'\"]?$", re.I)
_NEGATION = re.compile(r"\b(?:no|not|never|without|cannot|avoid|forbid\w*|prohibit\w*)\b|n't\b", re.I)
_FIXTURE = re.compile(r"\b(?:fixtures?|output|fake|faked|stub|stubbed|mock|mocked|canned)\b", re.I)
_CLAUSE_BREAK = re.compile(r"[.;!?](?:\s|$)|\bbut\b|\bthen\b", re.I)


def _block_step(state: tuple[int | None, bool, bool], line: str) -> tuple[int | None, bool, bool]:
    """`(run-context start column or None, inside a code fence, inside a checks block)` after `line`.
    A label opens a block; list items and fenced lines under it continue it; anything else ends it."""
    _, in_fence, in_block = state
    label = _RUN_LABEL.search(line)
    fence = line.strip().startswith("```")
    item = in_block and (in_fence or fence or _LIST_ITEM.match(line) is not None)
    start = label.start() if label else (0 if item else None)
    return (start, in_fence != fence, label is not None or item)


def _asks_to_run(line: str, start: int, ctx_start: int | None) -> bool:
    """A mention of a command at `start` asks the seat to run it when it sits in a run context or follows
    run/execute/invoke, and is not negated before it or part of a fixture or quoted output in its clause."""
    before = _CLAUSE_BREAK.split(line[:start])[-1]
    clause = before + _CLAUSE_BREAK.split(line[start:], 1)[0]
    if _NEGATION.search(before) or _FIXTURE.search(clause):
        return False
    return (ctx_start is not None and start >= ctx_start) or _RUN_VERB.search(before) is not None


def _run_commands(body: str) -> frozenset[str]:
    """Disallowed commands the body asks the seat to run: named in a checks list or Done line, or after
    run/execute/invoke. Prose naming a tool, a prohibition, a fixture, a `>` quoted output and a code-path
    name pass. Wrong belief this guards: that any mention of a tool is an instruction to use it."""
    lines = body.splitlines()
    states = list(accumulate(lines, _block_step, initial=(None, False, False)))[1:]
    return frozenset(
        cmd
        for line, (ctx_start, _, _) in zip(lines, states)
        if not line.lstrip().startswith(">")
        for cmd in _DISALLOWED_COMMANDS
        for m in re.finditer(rf"(?<!\w){re.escape(cmd)}(?!\w)", line)
        if _asks_to_run(line, m.start(), ctx_start)
    )


def _item_problems(item: dict, repo: str | None, grants, home: str | None = None) -> list:
    """reach, grant, size, cross_repo and (with `home`) home_path, each scoped to one ticket item. `repo` falsy
    (no repo could be resolved) stands the reach rule down instead of
    flagging every path — there is no target to check against. An unknown
    repo basename stands cross_repo down for the same reason."""
    task, body = item["task"], item.get("body", "")
    text = body + " " + " ".join(item.get("surfaces", []))
    allowed = set(grants or ())
    reach = [
        Problem(task, "reach", f"{p!r} is outside {repo!r}",
                "move the artifact into the repository or drop the reference")
        for p in _UNSAFE_PATH.findall(text)
        if not _inside_repo(p, repo, home) and not _is_route(p)
    ] if repo else []
    grant = [
        Problem(task, "grant", f"command {cmd!r} is not in this role's grant",
                "name only pytest, git status, git diff")
        for cmd in _DISALLOWED_COMMANDS
        if cmd not in allowed and cmd in _run_commands(body)
    ]
    words = len(body.split())
    size = [Problem(task, "size", f"body is {words} words",
                     "point at a spec file in the repository")] if words > 700 else []
    cross_repo = [
        Problem(task, "cross_repo", f"names {path}, which lives in {other}",
                "paste the code the build needs into the ticket: a build reads only its own repository")
        for path, other in _cross_repo_paths(text, repo)
    ]
    home_path = [
        Problem(task, "home_path", f"{p!r} is an absolute path under a home directory",
                f"write {repo_path.contract(p, home)!r} instead")
        for p in (_home_paths(text, home) if home is not None else [])
    ]
    return reach + grant + size + cross_repo + home_path


def _coupling_problems(items) -> list:
    """Two tickets in one phase sharing any `surfaces` entry.

    `merge_same_phase` already folds a same-phase `ready`/`todo` pair that
    shares a surface into one ticket before this runs, so a hit here means
    at least one side is `approved`, `in_progress`, or `done` (excluded
    from merging) and still collides with another ticket in its phase — a
    `needs` edge between the two no longer excuses it.

    §3's coupling rule has a second clause — "or whose named modules
    import one another" — not checked here. Wrong belief to avoid: that
    this function covers coupling in full; `items` carries surface paths,
    not import graphs, so the import clause is undetected.
    """
    by_phase: dict = {}
    for item in items:
        by_phase.setdefault(item.get("phase"), []).append(item)
    problems = []
    for group in by_phase.values():
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                shared = sorted(
                    s for s in first.get("surfaces", [])
                    if s in second.get("surfaces", [])
                )
                if shared:
                    problems.append(Problem(
                        first["task"], "coupling",
                        f"{first['task']} and {second['task']} both surface {shared[0]!r}",
                        "merge them, or move one to a later phase",
                    ))
    return problems


def _phase_needs_problems(items) -> list:
    """A `needs` edge between two tickets sharing one `phase` always deadlocks a phase land.

    A `needs` id absent from `items` is skipped. Wrong belief to avoid: that
    some other rule catches it; `reach` checks paths only, and no rule here
    checks that a `needs` id names a real ticket.
    """
    phase_by_task = {item["task"]: item.get("phase") for item in items if "task" in item}
    return [
        Problem(
            item["task"], "phase_needs",
            f"{item['task']} needs {other} in the same phase {item.get('phase')!r}",
            "move it to a later phase that needs this one",
        )
        for item in items
        for other in item.get("needs", [])
        if other in phase_by_task and phase_by_task[other] == item.get("phase")
    ]


def top_dirs(paths) -> frozenset:
    """First component of every path that sits in a directory; root-level files have none."""
    return frozenset(p.split("/", 1)[0] for p in paths if "/" in p)


_NEW_MARK = " (new)"


def _split_surface(surface: str) -> tuple:
    """(path, is_new): a trailing ' (new)' marks a file the ticket creates."""
    if surface.endswith(_NEW_MARK):
        return surface[: -len(_NEW_MARK)], True
    return surface, False


def _in_listing(path: str, paths) -> bool:
    """A file is in the listing by name; a directory surface (trailing '/') when any file sits under it."""
    return any(p.startswith(path) for p in paths) if path.endswith("/") else path in paths


def _surface_problem(task: str, surface: str, paths, own_tops, other_repo_tops) -> list:
    path, is_new = _split_surface(surface)
    top = path.split("/", 1)[0]
    owners = sorted(name for name, tops in other_repo_tops.items() if top in tops)
    if top not in own_tops and len(owners) == 1:
        return [Problem(task, "surface_wrong_repo",
                        f"{task} surfaces {path}, which lives in {owners[0]}",
                        f"file the work in {owners[0]}, or correct the path")]
    present = _in_listing(path, paths)
    if not is_new and not present:
        return [Problem(task, "surface_missing",
                        f"{task} surfaces {path}, which is not in the repository",
                        f"add '{_NEW_MARK.strip()}' after the path if the task creates it, otherwise correct the path")]
    if is_new and present:
        return [Problem(task, "surface_exists",
                        f"{task} marks {path} as new, but it already exists",
                        f"drop the '{_NEW_MARK}' marker, or pick another name")]
    return []


def surface_problems(items, paths, other_repo_tops) -> list:
    """Check each ticket surface against `paths`, the repo's file listing; no I/O.

    A surface in another repo's top directories is surface_wrong_repo and never also surface_missing."""
    own_tops = top_dirs(paths)
    return [
        p
        for item in items
        for surface in item.get("surfaces", [])
        for p in _surface_problem(item.get("task", ""), surface, paths, own_tops, other_repo_tops)
    ]


def lint_items(items, repo: str | None, grants, *, checkout=None, home: str | None = None) -> list:
    """work-shape.md §3: reach, grant, size, cross_repo and coupling over a decomposed
    DAG's parsed ticket items; no model, no I/O.

    `home` is a home directory; when given, paths compare in tilde form and home_path is added.

    `checkout` is `(paths, other_repo_tops)`; when given, surface_missing, surface_exists and
    surface_wrong_repo are added. None skips them."""
    problems = [p for item in items for p in _item_problems(item, repo, grants, home)]
    base = problems + _coupling_problems(items) + _phase_needs_problems(items)
    if checkout is None:
        return base
    return base + surface_problems(items, *checkout)


def _ordered_union(lists) -> list:
    """Every value across `lists`, first-appearance order, de-duplicated."""
    seen: set = set()
    result: list = []
    for values in lists:
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
    return result


def _surface_components(items: list, indices: list) -> list:
    """Connected components of `indices` into `items`, joining two indices
    whenever their `surfaces` lists intersect. Each component's members
    come out in ascending index order (file order), and components are
    ordered by their first member's index."""
    parent = {i: i for i in indices}

    def find(node: int) -> int:
        while parent[node] != node:
            node = parent[node]
        return node

    by_surface: dict = {}
    for i in indices:
        for surface in items[i].get("surfaces", []):
            by_surface.setdefault(surface, []).append(i)
    for group in by_surface.values():
        for other in group[1:]:
            parent[find(other)] = find(group[0])

    roots: dict = {}
    components: list = []
    for i in indices:
        root = find(i)
        if root not in roots:
            roots[root] = []
            components.append(roots[root])
        roots[root].append(i)
    return components


def _shared_surfaces(members: list) -> list:
    """Surfaces that connect at least two of `members`, first-appearance
    order. For a chain A-B-C this is A-B's shared path and B-C's shared
    path, not only a path every member has in common."""
    counts: dict = {}
    order: list = []
    for member in members:
        for surface in dict.fromkeys(member.get("surfaces", [])):
            if surface not in counts:
                counts[surface] = 0
                order.append(surface)
            counts[surface] += 1
    return [surface for surface in order if counts[surface] > 1]


def merge_same_phase(items: list) -> tuple[list, list]:
    """Same-phase tickets whose state is `ready` or `todo` and whose
    `surfaces` intersect, transitively, collapse into one merged ticket per
    connected component; a ticket in `approved`, `in_progress`, or `done`
    never joins a group. Pure: `items` is the in-memory ticket record list
    route.py already builds (id, title, state, phase, surfaces, needs,
    body); no file I/O. Returns the rewritten item list and one merge
    record per merge performed, each carrying the member ids in order, the
    merged id, and the surface(s) that connected the group.
    """
    by_phase: dict = {}
    for index, item in enumerate(items):
        by_phase.setdefault(item.get("phase"), []).append(index)

    components: list = []
    for indices in by_phase.values():
        eligible = [i for i in indices if items[i].get("state") in ("ready", "todo")]
        components.extend(_surface_components(items, eligible))

    merged_by_index: dict = {}
    dropped_by_index: dict = {}
    id_rewrites: dict = {}
    merges: list = []

    for indices in components:
        if len(indices) < 2:
            continue
        members = [items[i] for i in indices]
        member_ids = {member["id"] for member in members}
        merged_id = members[0]["id"]

        merged_by_index[indices[0]] = {
            **members[0],
            "title": "; ".join(member["title"] for member in members),
            "surfaces": _ordered_union(member.get("surfaces", []) for member in members),
            "needs": [
                needed
                for needed in _ordered_union(member.get("needs", []) for member in members)
                if needed not in member_ids
            ],
            "body": "\n\n".join(
                f"## {member['title']}\n{member.get('body', '')}" for member in members
            ),
        }
        for i, member in zip(indices[1:], members[1:]):
            dropped_by_index[i] = {**items[i], "state": "dropped", "merged_into": merged_id}
            id_rewrites[member["id"]] = merged_id

        merges.append({
            "members": [member["id"] for member in members],
            "into": merged_id,
            "surfaces": _shared_surfaces(members),
        })

    def rewritten_needs(needs: list) -> list:
        result: list = []
        for needed in needs:
            replacement = id_rewrites.get(needed, needed)
            if replacement not in result:
                result.append(replacement)
        return result

    result_items: list = []
    for index, item in enumerate(items):
        if index in dropped_by_index:
            result_items.append(dropped_by_index[index])
        elif index in merged_by_index:
            merged = merged_by_index[index]
            result_items.append({**merged, "needs": rewritten_needs(merged["needs"])})
        else:
            needs = item.get("needs", [])
            if any(needed in id_rewrites for needed in needs):
                result_items.append({**item, "needs": rewritten_needs(needs)})
            else:
                result_items.append(item)

    return result_items, merges


def merge_report_lines(merges: list) -> list[str]:
    """One `merged: <id> + <id> -> <into> (shared <surface>)` line per
    `merge_same_phase` merge record, member ids joined in order, naming
    only the first surface the group shares."""
    return [
        f"merged: {' + '.join(merge['members'])} -> {merge['into']} (shared {merge['surfaces'][0]})"
        for merge in merges
    ]


def _merge_task_row(initiative_id: str, item: Mapping, extra: Mapping) -> dict:
    """Pure: the store row for one task item, `extra` carrying the frontmatter keys the row shape leaves there."""
    phase = item.get("phase", "")
    return {
        "kind": "task",
        "initiative": initiative_id,
        "task_id": item["id"],
        "phase": phase,
        "state": item.get("state", "todo"),
        "needs": list(item.get("needs", [])),
        "title": item["title"],
        "surfaces": list(item.get("surfaces", [])),
        "body": item.get("body", ""),
        "extra": {"id": item["id"], "phase": phase, **extra},
    }


def merge_rows(items: Sequence[Mapping], merges: Sequence[Mapping], initiative_id: str) -> tuple[list[dict], list[dict]]:
    """`(rows, events)` for the `merge_same_phase` result `items` and `merges`: per merge, the surviving task row
    then one dropped row per folded member, and one `tasks_merged` event naming the members in order.
    A folded row carries `merged_into` at the top level and in its extra, beside the note `folded into <id>`:
    the store has no `merged_into` column, so `extra` is the copy a rendered file is rebuilt from."""
    by_id = {item["id"]: item for item in items}
    rows = [
        row
        for merge in merges
        for row in (
            _merge_task_row(initiative_id, by_id[merge["into"]], {}),
            *(
                {
                    **_merge_task_row(
                        initiative_id, by_id[member], {"note": f"folded into {merge['into']}", "merged_into": merge["into"]}
                    ),
                    "state": "dropped",
                    "merged_into": merge["into"],
                }
                for member in merge["members"]
                if member != merge["into"]
            ),
        )
    ]
    events = [{"event": "tasks_merged", "into": merge["into"], "tasks": list(merge["members"])} for merge in merges]
    return rows, events


def write_merge_rows(
    runs_dir: Path, rows: Sequence[Mapping], events: Sequence[Mapping], initiative_id: str, paths: Sequence[Path]
) -> list[str]:
    """Edge: the store first for a merge, `merge_rows`'s output. Each row goes through `run_store.upsert_row_detail`
    one at a time, then `events` are appended to the initiative row's `extra["events"]`, its other keys kept.
    Returns the task ids the store accepted. A refusal, or no initiative row, prints one line and returns
    what was accepted so far, before the baselines are recorded; the file side stays the caller's job.
    A task row already in the store keeps its `extra` keys (attempts, budget_usd, requires) and its priority, since an
    upsert replaces `extra` whole and `merge_rows` only knows the keys the item list carries.
    The written `paths` are recorded as export baselines, so the export never holds a merged file."""
    from agent_tools import queue_export  # imported here: route loads on every cox start

    stored = {r["task_id"]: r for r in run_store.read_queue(runs_dir, initiative_id, kind="task")}
    accepted: list[str] = []
    for planned in rows:
        old = stored.get(planned["task_id"]) or {}
        row = {
            **({"priority": old["priority"]} if "priority" in old else {}),
            **planned,
            "extra": {**(old.get("extra") or {}), **planned["extra"]},
        }
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            print(f"routing: store refused {row['kind']} row {row['task_id']}: {detail}")
            return accepted
        accepted.append(row["task_id"])
    if events:
        head = next((r for r in run_store.read_queue(runs_dir, initiative_id, kind="initiative") if r.get("task_id") == initiative_id), None)
        if head is None:
            print(f"routing: no initiative row {initiative_id} to record the merge on")
            return accepted
        extra = dict(head.get("extra") or {})
        detail = run_store.upsert_row_detail(runs_dir, {**head, "extra": {**extra, "events": [*extra.get("events", []), *events]}})
        if detail:
            print(f"routing: store refused initiative row {initiative_id}: {detail}")
            return accepted
    queue_export.record_written_paths(Path(runs_dir).parent, list(paths))
    return accepted


def _is_key_line(line: str, key: str) -> bool:
    return line == f"{key}:" or line.startswith(f"{key}: ")


def _with_header_fields(header_lines: list, changes: list) -> list:
    """`header_lines` with each `(key, value)` in `changes` set: a key already
    present has its line and any block-list continuation lines under it
    replaced in place; a new key is appended. Every other line is kept byte
    for byte, so fields this module cannot parse, such as `attempts` as a
    block list of mappings, survive."""
    if not changes:
        return header_lines
    (key, value), rest = changes[0], changes[1:]
    new_line = f"{key}: {_header_value(value)}"
    start = next((i for i, line in enumerate(header_lines) if _is_key_line(line, key)), None)
    if start is None:
        return _with_header_fields([*header_lines, new_line], rest)
    end = next(
        (i for i in range(start + 1, len(header_lines))
         if not (header_lines[i][:1].isspace() or header_lines[i].startswith("- "))),
        len(header_lines),
    )
    return _with_header_fields([*header_lines[:start], new_line, *header_lines[end:]], rest)


def merged_ticket_text(original: str, item: dict) -> str:
    """`original`, a ticket file's text, with only the fields a
    `merge_same_phase` result changed rewritten. A `dropped` member gets
    `state: dropped` and `merged_into`, body untouched; a survivor gets its
    merged `title`, `surfaces`, `needs` and body. Wrong belief this guards:
    that a ticket can be rebuilt from the fields the merge reads. Real
    tickets also carry `budget_usd`, `attempts` and `lint`, and the chair's
    quarantine and cause readers depend on `attempts`, so every header line
    the merge did not change is kept verbatim."""
    dropped = item.get("state") == "dropped"
    changes = (
        [("state", "dropped"), ("merged_into", item["merged_into"])]
        if dropped
        else [("title", item["title"]), ("surfaces", list(item.get("surfaces", []))),
              ("needs", list(item.get("needs", [])))]
    )
    opening, closing = "---\n", "\n---\n"
    close_index = original.find(closing, len(opening)) if original.startswith(opening) else -1
    if close_index == -1:
        body = original if dropped else item.get("body", "")
        return _frontmatter([(key, _Raw(_header_value(value))) for key, value in changes], body)
    header_lines = original[len(opening):close_index].split("\n")
    header = "\n".join(_with_header_fields(header_lines, changes))
    after = original[close_index + len(closing):] if dropped else f"\n{item.get('body', '')}\n"
    return f"{opening}{header}{closing}{after}"
