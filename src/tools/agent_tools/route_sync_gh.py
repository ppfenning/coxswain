"""The thin `gh` edge for the one-way GitHub Projects mirror: the only
module in the tree that runs `gh`. Every function here returns `(ok,
value)` or `(ok, detail)` rather than raising, and `execute` walks a plan
one `(kind, ok, detail)` per step, `agent_tools/land.py`'s own edge shape.
Gate resolution is a stand-in for the cartridge `policy.gate` lookup
landing separately: read straight off the item's `gate:` frontmatter.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path

import yaml

from agent_tools import epic, repo_path, route, route_sync, run_store

__all__ = [
    "api_login_ok", "auth_ok", "create_project", "execute", "existing", "existing_item", "find_project",
    "items_from_store", "project_item_from_response",
]

_LABEL = "coxswain"

_STATE_MUTATION = (
    "mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){"
    "updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,"
    "fieldId:$field,value:{singleSelectOptionId:$option}}){projectV2Item{id}}}"
)
_SUB_ISSUE_MUTATION = (
    "mutation($parent:ID!,$child:ID!){addSubIssue(input:{issueId:$parent,subIssueId:$child})"
    "{issue{id}}}"
)
_PARENT_QUERY = "query($id:ID!){node(id:$id){... on Issue{parent{id}}}}"
_SINGLE_SELECT = ("State", "Release")


def auth_ok(run) -> bool:
    """Whether the active `gh` login works; plain `status` exits 1 when any stored login is bad. A `gh` older than
    `--active` (Ubuntu's 2.46 package) rejects the flag and exits 1 too, so a failed status falls back to asking
    `gh api user`, which works on every version and proves the token itself (2026-10-23: every land on such a
    host said "gh is not authenticated" while the login was fine)."""
    if run(["gh", "auth", "status", "--active"], capture_output=True, text=True).returncode == 0:
        return True
    return api_login_ok(run)


def api_login_ok(run) -> bool:
    """Whether `gh api user` names a login; `auth status` can reject a token that still works."""
    try:
        done = run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


def _origin_slug(checkout: str) -> str:
    """`owner/name` from `<checkout>/.git/config`'s origin url, or `""`. A record stores `~/repos/<name>`, so the
    home is expanded first; a literal `~` directory never exists (2026-10-14: every sync refused for lack of a repo)."""
    if not checkout:
        return ""
    config = Path(repo_path.expand(checkout, os.path.expanduser("~"))) / ".git" / "config"
    if not config.is_file():
        return ""
    url = next((ln.split("=", 1)[1].strip() for ln in config.read_text().splitlines()
                if ln.strip().startswith("url")), "")
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else ""


class RepoError(ValueError):
    """A work item names a repo that is neither a checkout with an origin remote nor an `owner/name` slug."""


_SLUG = re.compile(r"[\w.-]+/[\w.-]+")
Resolver = Callable[[str], tuple[str, str]]


def _repo_of(value: str, aliases: Mapping[str, str] | None = None) -> str:
    """`value` may be a checkout path (`route file --repo PATH`'s own contract for intake items) or an
    `owner/name` slug: a checkout resolves to its origin slug, a slug passes through, and an empty value stays
    empty. `aliases` is the profile's `repo_aliases`: it maps a retired repo to a slug, by the value itself or
    by the last path segment of a value that is not already a slug. Anything else raises `RepoError` naming
    it, instead of reaching gh as an owner."""
    slug = _origin_slug(value)
    if slug or not value:
        return slug or value
    table = aliases or {}
    if value in table:
        return table[value]
    if _SLUG.fullmatch(value):
        return value
    named = table.get(Path(value.rstrip("/")).name)
    if named:
        return named
    raise RepoError(f"route sync: {value} is not a checkout with an origin remote nor an owner/name slug")


def _checked_repo(value: str, aliases: Mapping[str, str] | None = None) -> tuple[str, str]:
    """`(repo, error)`: `_repo_of(value)` and `""`, or `("", why)`. An item carries the error rather than raising it,
    so one bad repo stops only a sync that includes that item."""
    try:
        return _repo_of(value, aliases), ""
    except RepoError as exc:
        return "", str(exc)


def _initiative_repo(root: Path, initiative: str, resolve: Resolver = _checked_repo) -> tuple[str, str]:
    """`(repo, error)` of the initiative's `repo:`: `("", "")` when there is no initiative file or no `repo:`."""
    meta = root / "work" / initiative / "initiative.md"
    if not meta.is_file():
        return "", ""
    fields, _ = route.parse_frontmatter(meta.read_text())
    return resolve(fields.get("repo", ""))


def _pid_alive(root: Path, run_id: str) -> bool:
    pid_path = root / "runs" / f"{run_id}.pid"
    if not pid_path.is_file():
        return False
    text = pid_path.read_text().strip()
    return text.isdigit() and epic.run_live(int(text), pid_path)


# Real formats, measured by the chair on 2026-09-24 (a build cannot read the workspace).
# One call from `runs/<run>.usage.json` `calls[]`:
#     {"role": "scope_epic", "task_id": null, "cost_usd": 0.053325, "model": "haiku", "tier": "cheap"}
# The distinct task_ids in one run were [null, "model-router-capability-classes-cartridges-decide-class-ceiling"].
# So `task_id` is exactly the work item's id, or null for a run-level node. A null-task call
# belongs to no task: it is never spread across tasks and never added to a card's Cost.
# One task record, at
#     runs/tools-reads-capability-class-names-in-profile-1/tasks/build/tools-reads-capability-class-names-in-profile.json
# whose keys include
#     {"ticket": "tools-reads-capability-class-names-in-profile", "run_id":
#      "tools-reads-capability-class-names-in-profile-1:build:tools-reads-capability-class-names-in-profile",
#      "phase": "build", "initiative": "tools-reads-capability-class-names-in-profile", "landed": true}
# So the file stem is the item id, the run is the top directory name (`run_id` is a composite and is
# never parsed), and `landed: true` marks the landing run. Both joins are exact equality.


def _json_object(path: Path) -> dict:
    """The file's JSON object, or `{}` when it is unreadable or not an object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _cost_by_task(calls: list) -> dict[str, float]:
    """`{item id: cost_usd}` summed over the calls that name that task. Never a run's total."""
    costs: dict[str, float] = {}
    for call in calls:
        task, cost = call.get("task_id"), call.get("cost_usd")
        if isinstance(task, str) and isinstance(cost, (int, float)):
            costs[task] = costs.get(task, 0.0) + cost
    return costs


def _task_costs(root: Path) -> dict[str, float]:
    """Each task's own spend across every run's usage, from its file or else the run store."""
    usages = [u.get("calls") for u in run_store.usages(root / "runs").values()]
    return _cost_by_task([c for calls in usages if isinstance(calls, list) for c in calls if isinstance(c, dict)])


def _landing_runs(root: Path) -> dict[str, str]:
    """`{item id: run}` for the latest run whose task record has `landed: true`."""
    return {p.stem: p.parent.parent.parent.name for p in sorted(root.glob("runs/*/tasks/*/*.json"))
            if _json_object(p).get("landed") is True}


def _initiative_id(initiative: str) -> str:
    """The initiative card's id; a `route file` initiative shares its directory name with its only task."""
    return f"initiative:{initiative}"


def _initiative_issue(root: Path, initiative: str) -> str | None:
    meta = root / "work" / initiative / "initiative.md"
    issue = route.parse_frontmatter(meta.read_text())[0].get("issue") if meta.is_file() else None
    return str(issue) if issue else None


def _item(fields: dict, body: str, stem: str, *, state: str, phase: str, run_id: str,
          cost: float, gate: str, repo: str, parent: str | None = None,
          parent_issue: str | None = None, initiative: bool = False,
          release: str | None = None, repo_error: str = "", landed_pr: str = "") -> route_sync.Item:
    return route_sync.Item(
        id=fields.get("id", stem), title=fields.get("title", stem), body=body, repo=repo,
        state=state, phase=phase, run=run_id, cost_usd=cost, gate=gate, issue=fields.get("issue"),
        parent=parent, parent_issue=parent_issue, initiative=initiative,
        release=route_sync.parse_release(body) if release is None else release, repo_error=repo_error,
        landed_pr=landed_pr,
    )


def _intake_item(path: Path, resolve: Resolver) -> route_sync.Item:
    fields, body = route.parse_frontmatter(path.read_text())
    repo, repo_error = resolve(fields.get("repo", ""))
    return _item(fields, body, path.stem, state="intake", phase="", run_id="", cost=0.0,
                 gate="", repo=repo, repo_error=repo_error)


def _landed_of(text: str) -> object:
    """The `landed` front-matter value read as yaml, None when absent or unreadable. `route.parse_frontmatter`
    flattens a nested mapping into its values and drops the keys, so `{at, pr}` cannot be read through it."""
    if not text.startswith("---\n") or "\nlanded:" not in text:
        return None
    try:
        header = yaml.safe_load(text[4:].split("\n---\n", 1)[0])
    except yaml.YAMLError:
        return None
    return header.get("landed") if isinstance(header, dict) else None


def _work_item(root: Path, path: Path, landing: dict[str, str], costs: dict[str, float],
               resolve: Resolver) -> route_sync.Item:
    fields, body = route.parse_frontmatter(path.read_text())
    initiative, phase_dir, stem = path.parts[-3], path.parts[-2], path.stem
    item_id = fields.get("id", stem)
    attempts = fields.get("attempts") or []
    latest = route_sync.run_id_of(attempts[-1]) if attempts else ""
    in_flight = bool(latest) and _pid_alive(root, latest)
    state = "in_flight" if in_flight else fields.get("state", "todo")
    has_parent = (root / "work" / initiative / "initiative.md").is_file()
    repo, repo_error = _initiative_repo(root, initiative, resolve)
    return _item(fields, body, stem, state=state, phase=fields.get("phase", phase_dir),
                 run_id=landing.get(item_id) or latest,
                 cost=round(costs.get(item_id, 0.0), 4), gate=fields.get("gate", ""),
                 repo=repo, repo_error=repo_error,
                 parent=_initiative_id(initiative) if has_parent else None,
                 parent_issue=_initiative_issue(root, initiative),
                 landed_pr=route_sync.landed_pr_of(_landed_of(path.read_text())))


def _initiative_state(task_states: list[str]) -> str:
    """`done` only when there is a task and every task is done."""
    if task_states and all(s == "done" for s in task_states):
        return "done"
    return "in_flight" if "in_flight" in task_states else "ready"


def _initiative_item(root: Path, path: Path, tasks: list[route_sync.Item],
                     resolve: Resolver) -> route_sync.Item:
    fields, body = route.parse_frontmatter(path.read_text())
    initiative = path.parent.name
    repo, repo_error = _initiative_repo(root, initiative, resolve)
    return _item({**fields, "id": _initiative_id(initiative)}, body, initiative,
                 state=_initiative_state([t.state for t in tasks]), phase="", run_id="", cost=0.0, gate="",
                 repo=repo, repo_error=repo_error, initiative=True,
                 release=route_sync.epic_release([t.release for t in tasks]))


def _unresolved(value: str) -> tuple[str, str]:
    return value, ""


def resolve_repos(items: list[route_sync.Item], aliases: Mapping[str, str] | None = None) -> list[route_sync.Item]:
    """The items with `repo` resolved, for items loaded `lazy`; one that fails keeps its value in `repo_named`."""
    resolved = [(item, *_checked_repo(item.repo, aliases)) for item in items]
    return [replace(item, repo=repo, repo_error=error, repo_named=item.repo if error else "")
            for item, repo, error in resolved]


def items_from_store(workspace, aliases: Mapping[str, str] | None = None, *, lazy: bool = False) -> list[route_sync.Item]:
    """Every `intake/*.md`, `work/*/initiative.md` and `work/*/*/*.md` item, as a
    `route_sync.Item`. An initiative is listed before its tasks. `aliases` is the profile's `repo_aliases`.
    With `lazy` no repo is resolved and `repo` holds the value as written, for `resolve_repos` to settle later."""
    root = Path(workspace)
    resolve = _unresolved if lazy else partial(_checked_repo, aliases=aliases)
    intake_dir = root / "intake"
    intake = [_intake_item(p, resolve) for p in sorted(intake_dir.glob("*.md"))] if intake_dir.is_dir() else []
    landing, costs = _landing_runs(root), _task_costs(root)
    work = [_work_item(root, p, landing, costs, resolve) for p in sorted(root.glob("work/*/*/*.md"))]
    initiatives = [_initiative_item(root, p, [t for t in work if t.parent == _initiative_id(p.parent.name)], resolve)
                   for p in sorted(root.glob("work/*/initiative.md"))]
    return intake + initiatives + work


# `gh` returns 30 rows unless told otherwise; a missed row is replanned as new on every sync.
_LIMIT = "10000"
_FIELD_TITLES = {t.lower(): t for t in ("State", "Phase", "Run", "Cost", "Gate", "Release")}


def existing(run, repo_names: list[str], project: str | None):
    """`(True, (issues, project_items, item_node_ids))` on success, `(False,
    detail)` on the first failed `gh` call. `project` may be `None` — a
    dry-run preview before any project exists — in which case the item
    listing is skipped rather than run against a project that isn't there."""
    issues: dict[str, dict] = {}
    for repo in repo_names:
        result = run(["gh", "issue", "list", "--repo", repo, "--label", _LABEL, "--state", "all",
                      "--json", "number,title,body,state", "--limit", _LIMIT], capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        for row in json.loads(result.stdout or "[]"):
            issues[str(row["number"])] = {"title": row.get("title", ""), "body": row.get("body", ""),
                                          "state": row.get("state", "")}
    project_items: dict[str, dict] = {}
    item_node_ids: dict[str, str] = {}
    if project is not None:
        owner, _, number = project.partition("/")
        result = run(["gh", "project", "item-list", number, "--owner", owner, "--format", "json", "--limit", _LIMIT],
                     capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        # `gh project item-list --format json` keys field values by the
        # lowercased field title ("state"); plan compares by title ("State").
        for row in json.loads(result.stdout or "{}").get("items", []):
            key = str((row.get("content") or {}).get("number", ""))
            if not key:
                continue
            project_items[key] = {_FIELD_TITLES[k]: v for k, v in row.items() if k in _FIELD_TITLES}
            if row.get("id"):
                item_node_ids[key] = row["id"]
    return True, (issues, project_items, item_node_ids)


def prune_candidates(run, repo_names: list[str], project: str | None):
    """`(True, (issues, cards))` keyed `(repo, number)`, `(False, detail)` on the first failed `gh` call. `issues` holds
    every `coxswain`-labelled issue, open or closed, per repo; `cards` maps `(repo, number)` to the card's node id.
    Numbers repeat across repos, so neither map is keyed by number alone."""
    issues: dict[tuple[str, str], dict] = {}
    for repo in repo_names:
        ok, result = existing(run, [repo], None)
        if not ok:
            return False, result
        issues.update({(repo, number): row for number, row in result[0].items()})
    cards: dict[tuple[str, str], str] = {}
    if project is not None:
        owner, _, number = project.partition("/")
        result = run(["gh", "project", "item-list", number, "--owner", owner, "--format", "json", "--limit", _LIMIT],
                     capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        for row in json.loads(result.stdout or "{}").get("items", []):
            content = row.get("content") or {}
            repo = str(content.get("repository", "")).removeprefix("https://github.com/")
            if content.get("number") and row.get("id"):
                cards[(repo, str(content["number"]))] = row["id"]
    return True, (issues, cards)


def rollup_issue_number(run, rollup: route_sync.Item) -> str | None:
    """Edge. The number of the `<version> fixes` issue on file for `rollup`, None when there is none or `gh` fails."""
    ok, result = existing(run, [rollup.repo], None)
    linked = route_sync.link_rollups([rollup], result[0]) if ok else []
    return linked[0].issue if linked else None


# `gh issue view --json projectItems` carries only the project title and Status, not the
# State/Phase/Run/Cost/Gate values `plan` compares, so the item's own row is one GraphQL read.
_ITEM_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "issue(number:$number){projectItems(first:20){nodes{id project{number owner{"
    "... on Organization{login} ... on User{login}}} fieldValues(first:30){nodes{"
    "... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}} "
    "... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}}}}}}}}}"
)


def project_item_from_response(data: dict, project: str) -> tuple[dict, str]:
    """`(fields, node_id)` of the issue's item in `project` (`owner/number`), or `({}, "")`.
    Fields are keyed by the titles in `_FIELD_TITLES`."""
    owner, _, number = project.partition("/")
    issue = ((data.get("data") or {}).get("repository") or {}).get("issue") or {}
    for node in (issue.get("projectItems") or {}).get("nodes") or []:
        meta = node.get("project") or {}
        if str(meta.get("number")) != number or (meta.get("owner") or {}).get("login", "").lower() != owner.lower():
            continue
        values = [v for v in (node.get("fieldValues") or {}).get("nodes") or [] if v]
        fields = {_FIELD_TITLES[title.lower()]: v.get("text", v.get("name", ""))
                  for v in values if (title := (v.get("field") or {}).get("name", "")).lower() in _FIELD_TITLES}
        return fields, node.get("id", "")
    return {}, ""


def existing_item(run, repo: str, issue: str | None, project: str | None):
    """`existing`'s result shape for the one issue `repo#issue`: one `gh issue view`, plus one
    GraphQL read of its project item when `project` is set. Never lists issues or the project.
    An item with no issue yet, or an issue without the label, reads as absent."""
    if not issue or not repo:
        return True, ({}, {}, {})
    view = run(["gh", "issue", "view", issue, "--repo", repo, "--json", "number,title,body,state,labels"],
               capture_output=True, text=True)
    if view.returncode != 0:
        return False, view.stderr.strip() or view.stdout.strip()
    row = json.loads(view.stdout or "{}")
    if _LABEL not in [label.get("name") for label in row.get("labels") or []]:
        return True, ({}, {}, {})
    issues = {issue: {"title": row.get("title", ""), "body": row.get("body", ""), "state": row.get("state", "")}}
    if project is None:
        return True, (issues, {}, {})
    owner, _, name = repo.partition("/")
    item = run(["gh", "api", "graphql", "-f", f"query={_ITEM_QUERY}", "-f", f"owner={owner}",
                "-f", f"name={name}", "-F", f"number={issue}"], capture_output=True, text=True)
    if item.returncode != 0:
        return False, item.stderr.strip() or item.stdout.strip()
    data = json.loads(item.stdout or "{}")
    # A query the schema rejects can still exit 0 with only `errors`; that must fail loudly, not read as "no item".
    if data.get("errors") or "data" not in data:
        return False, "; ".join(e.get("message", "") for e in data.get("errors") or []) or "graphql: no data"
    fields, node_id = project_item_from_response(data, project)
    if not node_id:
        return True, (issues, {}, {})
    return True, (issues, {issue: fields}, {issue: node_id})


def find_project(run, owner: str, name: str = "Coxswain"):
    """`(True, number)` when a project titled `name` exists under `owner`,
    `(True, None)` when the listing succeeded but none matched, `(False,
    detail)` when the `gh project list` call itself failed."""
    result = run(["gh", "project", "list", "--owner", owner, "--format", "json"],
                 capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    parsed = json.loads(result.stdout or "[]")
    projects = parsed.get("projects", []) if isinstance(parsed, dict) else parsed
    match = next((p for p in projects if p.get("title") == name), None)
    return True, (int(match["number"]) if match is not None else None)


def create_project(run, owner: str, name: str = "Coxswain"):
    """`(True, number)` for a freshly created project, `(False, detail)`
    when `gh project create` fails."""
    result = run(["gh", "project", "create", "--owner", owner, "--title", name, "--format", "json"],
                 capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, int(json.loads(result.stdout)["number"])


def _find_item_file(root: Path, item_id: str):
    candidates = list((root / "intake").glob("*.md")) if (root / "intake").is_dir() else []
    candidates += list(root.glob("work/*/*/*.md")) + list(root.glob("work/*/initiative.md"))
    for path in candidates:
        fields, _ = route.parse_frontmatter(path.read_text())
        own_id = _initiative_id(path.parent.name) if path.name == "initiative.md" else fields.get("id", path.stem)
        if own_id == item_id:
            return path
    return None


def _rewrite_issue_line(text: str, issue: str) -> str:
    """Pure: `text` with only its `issue:` frontmatter line replaced, or
    inserted just before the closing `---` when it never had one."""
    lines = text.splitlines(keepends=True)
    new_line = f"issue: {issue}\n"
    for i, line in enumerate(lines):
        if line.startswith("issue:"):
            return "".join(lines[:i] + [new_line] + lines[i + 1:])
        if line.rstrip("\n") == "---" and i > 0:
            return "".join(lines[:i] + [new_line] + lines[i:])
    return text


def issue_row(rel_parts: tuple[str, ...], text: str) -> dict | None:
    """Pure: the partial key of the row a written file names, with its `issue`, or None when the file names no row
    or carries no issue. The caller merges the issue into the stored row's `extra`."""
    from agent_tools import queue_rows, store_fill

    shape = store_fill.shape_of(rel_parts)
    row = None if shape is None else queue_rows.parse_item(*shape, text)
    if row is None or "issue" not in row["extra"]:
        return None
    return {"kind": row["kind"], "initiative": row["initiative"], "task_id": row["task_id"], "issue": row["extra"]["issue"]}


def _store_issue(root: Path, path: Path, text: str) -> bool:
    """Edge: True when the store row the file names now carries its `issue`, so the export writes the line back
    instead of dropping it. False when the file names no stored row or the store does not take it."""
    from agent_tools import run_store

    try:
        named = issue_row(path.relative_to(root).parts, text)
    except ValueError:
        return False
    if named is None:
        return False
    runs_dir = root / "runs"
    stored = next(
        (
            r for r in run_store.read_queue(runs_dir, named["initiative"])
            if r.get("task_id") == named["task_id"] and r.get("kind") == named["kind"]
        ),
        None,
    )
    if stored is None:
        return False
    extra = {**(stored.get("extra") or {}), "issue": named["issue"]}
    return run_store.upsert_row(runs_dir, {"initiative": named["initiative"], "task_id": named["task_id"], "extra": extra})


def _writeback(root: Path, item_id: str, issue: str) -> None:
    """Write the issue line into the item's file, then into its store row. Only once the store holds it too is the
    file recorded as the workspace's export baseline: an issue the store lacks would be dropped by the next export,
    so that file is left for the export guard to hold and report instead."""
    from agent_tools import queue_export

    if not issue:
        return
    path = _find_item_file(root, item_id)
    if path is None:
        return
    text = _rewrite_issue_line(path.read_text(), issue)
    path.write_text(text)
    if _store_issue(root, path, text):
        queue_export.record_written_paths(root, [path])


def _resolved_issue(step: dict, ctx: dict):
    if step["issue"] is not None:
        return step["issue"]
    return ctx["just_created"]["issue"] if ctx["just_created"] else None


def _step_repo(issue, ctx: dict) -> str:
    just_created = ctx["just_created"]
    if just_created and just_created["issue"] == issue:
        return just_created["repo"]
    return ctx["repo_by_issue"].get(issue, "")


def _issue_node_id(run, repo: str, issue: str) -> tuple[bool, str]:
    result = run(["gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".node_id"],
                 capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    node_id = result.stdout.strip()
    return (True, node_id) if node_id else (False, f"no node id for issue {issue}")


def _graphql(run, query: str, **variables: str) -> tuple[bool, dict | str]:
    """`(True, data)` or `(False, detail)`; a response carrying `errors` is a failure."""
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    result = run(argv + [a for k, v in variables.items() for a in ("-f", f"{k}={v}")],
                 capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    try:
        body = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, f"graphql: unreadable response {result.stdout.strip()!r}"
    if body.get("errors"):
        return False, "; ".join(e.get("message", "") for e in body["errors"])
    return True, body.get("data") or {}


def _link_sub_issue(step: dict, run, ctx: dict) -> tuple[bool, str]:
    """`addSubIssue` of the child's issue under the initiative's, skipped when the
    child already has that parent so a rerun is a no-op."""
    parent = step["parent_issue"] or ctx["issue_by_item"].get(step["parent_item"])
    child = _resolved_issue({"issue": step["child_issue"]}, ctx)
    if not parent or not child:
        return False, f"no issue known for {step['child_item'] if parent else step['parent_item']}"
    repo = step["repo"] or _step_repo(child, ctx)
    ok, parent_node = _issue_node_id(run, repo, parent)
    if not ok:
        return False, parent_node
    ok, child_node = _issue_node_id(run, repo, child)
    if not ok:
        return False, child_node
    ok, data = _graphql(run, _PARENT_QUERY, id=child_node)
    if not ok:
        return False, data
    if ((data.get("node") or {}).get("parent") or {}).get("id") == parent_node:
        return True, f"{child} already under {parent}"
    ok, data = _graphql(run, _SUB_ISSUE_MUTATION, parent=parent_node, child=child_node)
    return (True, f"{child} under {parent}") if ok else (False, data)


def _ensure_milestone(run, repo: str, title: str) -> tuple[bool, str]:
    """The repo's milestone `title`, created when absent. Closed ones count: GitHub refuses a duplicate title."""
    listed = run(["gh", "api", f"repos/{repo}/milestones?state=all&per_page=100", "--paginate",
                  "--jq", ".[].title"], capture_output=True, text=True)
    if listed.returncode != 0:
        return False, listed.stderr.strip() or listed.stdout.strip()
    if title in listed.stdout.splitlines():
        return True, f"milestone {title} exists"
    made = run(["gh", "api", f"repos/{repo}/milestones", "-f", f"title={title}"], capture_output=True, text=True)
    return made.returncode == 0, (f"milestone {title} created" if made.returncode == 0
                                  else made.stderr.strip() or made.stdout.strip())


def _missing_state_option(steps: list[dict], option_ids: dict) -> str | None:
    """The first `State` value a step sets that the board has no option for, else `None`."""
    return next((s["value"] for s in steps if s["kind"] == "project_set" and s["field"] == "State"
                 and ("State", s["value"]) not in option_ids), None)


def _add_release_options(run, project: str, meta: tuple, steps: list[dict]):
    """`(True, meta)` once every Release value the steps set is an option on the board, `(False, detail)` on a
    failed call. The field is created with the missing options when absent. Otherwise `updateProjectV2Field`
    rewrites the option list: the existing names stay and the new ones follow, so option ids and colours are
    reissued. Release names are coxswain's, so that is accepted. The meta is re-read, since the ids changed."""
    _, field_ids, option_ids = meta
    have = [name for field, name in option_ids if field == "Release"]
    missing = sorted({s["value"] for s in steps if s["kind"] == "project_set" and s["field"] == "Release"} - set(have))
    if not missing:
        return True, meta
    owner, _, number = project.partition("/")
    if "Release" in field_ids:
        options = ",".join(f"{{name:{json.dumps(n)},color:GRAY,description:\"\"}}" for n in have + missing)
        ok, data = _graphql(run, f"mutation{{updateProjectV2Field(input:{{fieldId:{json.dumps(field_ids['Release'])},"
                                 f"singleSelectOptions:[{options}]}}){{clientMutationId}}}}")
        if not ok:
            return False, data
    else:
        made = run(["gh", "project", "field-create", number, "--owner", owner, "--name", "Release",
                    "--data-type", "SINGLE_SELECT", "--single-select-options", ",".join(missing)],
                   capture_output=True, text=True)
        if made.returncode != 0:
            return False, made.stderr.strip() or made.stdout.strip()
    return _project_meta(run, project)


def _execute_step(step: dict, run, ctx: dict) -> tuple[bool, str]:
    """One step through `gh`, `(ok, detail)` — `land.py`'s own edge shape."""
    kind = step["kind"]
    if kind == "refuse":
        return False, step["detail"]
    if kind == "issue_create":
        result = run(["gh", "issue", "create", "--repo", step["repo"], "--title", step["title"],
                      "--body", step["body"], "--label", step["label"]], capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        issue = result.stdout.strip().rsplit("/", 1)[-1]
        if not issue:
            return False, "gh issue create returned no issue url"
        ctx["just_created"] = {"issue": issue, "repo": step["repo"]}
        ctx["issue_by_item"][step["item_id"]] = issue
        # Recorded at once, so a later failed step cannot leave the issue unknown and a rerun duplicate it.
        _writeback(ctx["root"], step["item_id"], issue)
        return True, issue
    if kind == "issue_edit":
        repo = _step_repo(step["issue"], ctx)
        result = run(["gh", "issue", "edit", step["issue"], "--repo", repo, "--title", step["title"],
                      "--body", step["body"]], capture_output=True, text=True)
        return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())
    if kind == "issue_close":
        result = run(["gh", "issue", "close", step["issue"], "--repo", _step_repo(step["issue"], ctx)],
                     capture_output=True, text=True)
        return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())
    if kind == "issue_prune":
        comment = f"Closed by `cox route sync --prune-out-of-scope`: task issues are outside tracker_scope {step['scope']}."
        result = run(["gh", "issue", "close", step["issue"], "--repo", step["repo"], "--reason", "not planned",
                      "--comment", comment], capture_output=True, text=True)
        return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())
    if kind == "card_remove":
        owner, _, number = ctx["project"].partition("/")
        result = run(["gh", "project", "item-delete", number, "--owner", owner, "--id", step["card"]],
                     capture_output=True, text=True)
        return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())
    if kind == "project_add":
        issue = _resolved_issue(step, ctx)
        repo = _step_repo(issue, ctx)
        owner, _, number = ctx["project"].partition("/")
        result = run(["gh", "project", "item-add", number, "--owner", owner, "--url",
                      f"https://github.com/{repo}/issues/{issue}", "--format", "json"],
                     capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        node_id = json.loads(result.stdout or "{}").get("id", "")
        if node_id:
            ctx["item_ids"][issue] = node_id
        return True, issue
    if kind == "project_set":
        issue = _resolved_issue(step, ctx)
        item_id = ctx["item_ids"].get(issue, "")
        field_id = ctx["field_ids"].get(step["field"], "")
        if not item_id or not field_id:
            return False, f"no project item/field id known for {step['field']!r} on issue {issue}"
        if step["field"] in _SINGLE_SELECT:
            option_id = ctx["option_ids"].get((step["field"], step["value"]), "")
            if not option_id:
                return False, f"no option id known for {step['field']}={step['value']!r}"
            result = run(["gh", "api", "graphql", "-f", f"query={_STATE_MUTATION}",
                          "-f", f"project={ctx['project_node_id']}", "-f", f"item={item_id}",
                          "-f", f"field={field_id}", "-f", f"option={option_id}"],
                         capture_output=True, text=True)
        else:
            # `gh` refuses an empty `--text` ("no changes to make"); an emptied field is cleared.
            value = ["--text", str(step["value"])] if step["value"] != "" else ["--clear"]
            result = run(["gh", "project", "item-edit", "--id", item_id, "--project-id",
                          ctx["project_node_id"], "--field-id", field_id, *value],
                         capture_output=True, text=True)
        return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())
    if kind == "milestone_set":
        issue = _resolved_issue(step, ctx)
        if not issue:
            return False, "no issue to set the milestone on"
        repo = step["repo"] or _step_repo(issue, ctx)
        ok, detail = _ensure_milestone(run, repo, step["title"])
        if not ok:
            return False, detail
        result = run(["gh", "issue", "edit", issue, "--repo", repo, "--milestone", step["title"]],
                     capture_output=True, text=True)
        return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())
    if kind == "sub_issue_link":
        return _link_sub_issue(step, run, ctx)
    if kind == "writeback":
        issue = _resolved_issue(step, ctx)
        if not issue:
            return False, "no issue to write back"
        _writeback(ctx["root"], step["item_id"], issue)
        return True, issue
    return False, f"unknown step kind {kind!r}"


def _project_meta(run, project: str):
    """`(True, (project_node_id, field_ids, option_ids))`, or `(False,
    detail)` on the first failed call. Every id `gh project item-edit` and
    the GraphQL field-value mutation take is a node id, not the project
    number or the issue number, so this is resolved once per `execute` call
    rather than guessed from data the plan already carries."""
    owner, _, number = project.partition("/")
    view = run(["gh", "project", "view", number, "--owner", owner, "--format", "json"],
               capture_output=True, text=True)
    if view.returncode != 0:
        return False, view.stderr.strip() or view.stdout.strip()
    fields = run(["gh", "project", "field-list", number, "--owner", owner, "--format", "json"],
                 capture_output=True, text=True)
    if fields.returncode != 0:
        return False, fields.stderr.strip() or fields.stdout.strip()
    field_ids: dict[str, str] = {}
    option_ids: dict[tuple[str, str], str] = {}
    for field in json.loads(fields.stdout or "{}").get("fields", []):
        field_ids[field["name"]] = field["id"]
        for option in field.get("options") or []:
            option_ids[(field["name"], option["name"])] = option["id"]
    return True, (json.loads(view.stdout or "{}").get("id", ""), field_ids, option_ids)


def execute(steps: list[dict], run, project: str, workspace, items=(), item_node_ids=None):
    """Walk `steps` through `gh`: one `(kind, ok, detail)` per step. Stops
    after the first failed `gh` call so a later step never builds on state a
    failure left half-written — `land.py`'s own stop-on-failure shape. A
    `refuse` step (the planner's own "this item's state made no sense")
    never calls `gh` and never stops the rest of the run, since it touched
    nothing to begin with. The board's options are checked first: a State value
    with no option returns one `state_option` failure before any `gh` mutation,
    and a missing Release option is created."""
    needs_meta = any(s["kind"] in ("project_add", "project_set") for s in steps)
    if needs_meta and project:
        ok, meta = _project_meta(run, project)
        if not ok:
            return [("project_meta", False, meta)]
        # Every board check runs before the first step, so a refusal never follows an `issue_create`.
        missing = _missing_state_option(steps, meta[2])
        if missing is not None:
            return [("state_option", False, f"the project has no State option {missing!r}; nothing was created")]
        ok, meta = _add_release_options(run, project, meta, steps)
        if not ok:
            return [("release_option", False, meta)]
        project_node_id, field_ids, option_ids = meta
    else:
        project_node_id, field_ids, option_ids = "", {}, {}
    ctx = {
        "project": project,
        "root": Path(workspace),
        "repo_by_issue": {item.issue: item.repo for item in items if item.issue},
        "item_ids": dict(item_node_ids or {}),
        "issue_by_item": {item.id: item.issue for item in items if item.issue},
        "just_created": None,
        "project_node_id": project_node_id,
        "field_ids": field_ids,
        "option_ids": option_ids,
    }
    log = []
    for step in steps:
        ok, detail = _execute_step(step, run, ctx)
        log.append((step["kind"], ok, detail))
        if not ok and step["kind"] != "refuse":
            break
    return log
