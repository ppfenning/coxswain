"""The board files: exporting them, merging initiatives' tickets, the surface lint and the route groups."""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_tools.cli_common import _export_board, _profile_path, _read_text_or_none
from agent_tools.cli_lazy import (
    bounded_run,
    checkout,
    queue_export,
    queue_rows,
    repo_path,
    route,
    run_store,
    workspace_sync,
)
from agent_tools.cli_runs_fetch import _runs_dir_for_land


def _launch_rows_then_export(
    runs_dir: Path, initiative_id: str, repo: str | None, is_chair: bool,
    read_rows: Callable | None = None, export: Callable[[Path, Path], list[str]] | None = None,
    fill: Callable[[Path], object] | None = None, land_at: str = "task",
) -> tuple[int, list[str]]:  # "task" is route.DEFAULT_LAND_AT
    """Edge. Fill the store from the files, read the initiative's rows, then export the whole board only when `is_chair`.

    `land_at` reaches `route.launch_from_rows`; an injected `read_rows` takes three arguments and ignores it.

    The fill runs first for every host, since a decompose writes files and the rows read sees only the store. A fill that
    cannot read the store refuses with code 2 before any rows read or export. A rows error returns before any export.

    The fill and the export each hold the workspace lock (`workspace_lock`), since housekeeping pulls, fills, exports
    and commits the same checkout at the same time. A fill that waited past the lock's bound refuses with code 2, so
    the launch is retried on a later pass; an export that did is a note, since housekeeping's own export follows."""
    from agent_tools import store_fill, workspace_lock  # imported here: cli loads a command module only when it runs

    try:
        (fill or store_fill.fill_workspace)(runs_dir.parent)
    except workspace_lock.WorkspaceBusy as busy:
        return 2, [f"routing: {busy}; launch {initiative_id} on a later pass"]
    except RuntimeError:
        return 2, [f"routing: no initiative row {initiative_id} in the store, or the store is unavailable"]
    code, lines, _ = (read_rows or (lambda *args: route.launch_from_rows(*args, land_at=land_at)))(runs_dir, initiative_id, repo)
    if code:
        return code, lines
    if not route.should_export_board(is_chair):
        return 0, []
    try:
        skipped = (export or _export_board)(runs_dir, runs_dir.parent)
    except workspace_lock.WorkspaceBusy as busy:
        return 0, [f"export: {busy}"]
    return 0, [f"export: skipped {len(skipped)} contentless row(s): {', '.join(skipped)}"] if skipped else []


def export_commit_excludes(held: Sequence[Mapping[str, str]]) -> list[str]:
    """Pure: one `:(exclude)` pathspec per held file, so the export commit stages and commits around it."""
    return [f":(exclude){row['path']}" for row in held if row.get("path")]


def _chair_export_hook(runs_dir: Path, ids: tuple[str, ...], commit: bool = True) -> str:
    """Edge. Export the board, then commit the work/ and intake/ files; returns a tick-line note, "" when nothing is worth saying.

    With `commit` False (`chair.workspace_git: false`) the files are written and nothing is committed or pushed.

    The files the export held are left out of the commit: committing one would make the hand edit this workspace's HEAD
    baseline, and the next export would overwrite it. A git failure raises, so the tick reports it as an export failure.
    A failed push is a note, never a raise.

    The export and the commit are one hold of the workspace lock (`workspace_lock`), so no other writer's pull, fill
    or export lands between them and no two `git` calls race for the index; the push runs after it is released. A lock
    held past its bound raises `WorkspaceBusy`, which the caller reports as a skipped export."""
    from agent_tools import workspace_lock

    workspace = runs_dir.parent
    with workspace_lock.held(workspace, "commit"):
        committed, made, skipped = _export_and_commit(runs_dir, ids, commit)
    if made:
        pushed = workspace_sync.push_workspace(workspace)
        committed += f"; {pushed}" if pushed else ""
    return f"{committed}; skipped {len(skipped)} contentless row(s): {', '.join(skipped)}" if skipped else committed


def _export_and_commit(runs_dir: Path, ids: tuple[str, ...], commit: bool) -> tuple[str, bool, list[str]]:
    """Edge. `_chair_export_hook` up to the push: its note, whether a commit was made to push, and the skipped ids."""
    workspace = runs_dir.parent
    # Fills the store from the files first; see _export_board. Out of git, a stale copy of a dropped row goes first.
    skipped = _export_board(runs_dir, workspace) if commit else _export_board(runs_dir, workspace, prune_stale=True)
    if not commit:
        return f"export: wrote the files for {len(ids)} changed row(s); the workspace is not in git", False, skipped
    git = ["git", "-C", str(workspace)]
    names = [name for name in ("work", "intake") if (workspace / name).exists()]  # git add refuses a path that matches nothing
    if not names:  # `git add -A --` with no path stages the whole tree
        return "export: no work/ or intake/ directory to commit", False, []
    paths = [*names, *export_commit_excludes(queue_export.read_held_record(workspace))]
    add = bounded_run.completed([*git, "add", "-A", "--", *paths], capture_output=True, text=True)
    if add.returncode != 0:
        raise RuntimeError(f"git add: {add.stderr.strip()}")
    # The staged files by name, not the trees: `git commit -- work` refuses a tree whose every file is held and untracked.
    staged = bounded_run.completed([*git, "diff", "--cached", "--name-only", "-z", "--", *paths], capture_output=True, text=True)
    if staged.returncode != 0:
        raise RuntimeError(f"git diff: {staged.stderr.strip()}")
    files = [name for name in staged.stdout.split("\0") if name]
    if not files:
        return "export: no file changes", False, skipped
    message = f"chair: export {len(ids)} changed row(s)"
    made = bounded_run.completed([*git, "commit", "-m", message, "--", *files], capture_output=True, text=True)
    if made.returncode != 0:
        raise RuntimeError(f"git commit: {(made.stderr or made.stdout).strip()}")
    return f"export: committed {len(ids)} row(s)", True, skipped


def _initiative_head(runs_dir: Path, initiative_id: str) -> dict | None:
    """Edge. The initiative's own store row; None when the store has none."""
    rows = run_store.read_queue(runs_dir, initiative_id, kind="initiative")
    return next((r for r in rows if r.get("task_id") == initiative_id), None)


def _events_of(head: Mapping | None) -> list | None:
    return None if head is None else list((head.get("extra") or {}).get("events", []))


def _store_mode(runs_dir: Path | None, initiative_dir: Path) -> str:
    """Edge. How a merge of `initiative_dir` reaches the store: "up" (an initiative row), "headless" (rows for the
    initiative but no initiative row), "outside" (not the workspace's own `work/<initiative>`, so the store's rows
    are not its rows), else "down": no workspace, or a store that answers with nothing, which cannot be told apart."""
    if runs_dir is None:
        return "down"
    if (runs_dir.parent / "work" / initiative_dir.name).resolve() != initiative_dir:
        return "outside"
    if _initiative_head(runs_dir, initiative_dir.name) is not None:
        return "up"
    return "headless" if run_store.read_queue(runs_dir, initiative_dir.name) else "down"


def _unfiled_notice(state: str, path: Path, initiative_id: str) -> str | None:
    """Pure: the one line printed before `path` is written without a store row behind it; None when it needs none."""
    if state in ("applied", "outside"):
        return None
    if state == "headless":
        return f"routing: the store has no row for initiative {initiative_id}, filing {path} without it"
    return route._unrecorded_row_warning(path, {})


def _rows_before(runs_dir: Path, initiative_id: str, sources: Mapping[str, tuple[Path, str]]) -> dict[str, dict]:
    """Edge. Each ticket's task row as it stands, else the row its file describes: what a rollback puts back."""
    stored = {r["task_id"]: r for r in run_store.read_queue(runs_dir, initiative_id, kind="task")}
    parsed = {task: queue_rows.parse_item("task", (initiative_id, path.parent.name, path.stem), text) for task, (path, text) in sources.items()}
    return {task: row for task in sources if (row := stored.get(task) or parsed[task]) is not None}


def _put_back(runs_dir: Path, rows: Sequence[Mapping]) -> None:
    """Edge. Upserts `rows`, one printed line for each the store refuses."""
    for row in rows:
        if detail := run_store.upsert_row_detail(runs_dir, row):
            print(f"routing: could not put back {row['kind']} row {row['task_id']}: {detail}; the store and the workspace now disagree")


def _store_merge(runs_dir: Path, initiative_id: str, items: list[dict], merge: Mapping, paths: list[Path], before: Mapping[str, dict]) -> str:
    """Edge. One merge to the store: "applied", "refused" (its accepted rows put back), or "unavailable" (store gone)."""
    seen = _events_of(_initiative_head(runs_dir, initiative_id))
    rows, events = route.merge_rows(items, [merge], initiative_id)
    accepted = route.write_merge_rows(runs_dir, rows, events, initiative_id, paths)
    held = _events_of(_initiative_head(runs_dir, initiative_id))
    if seen is None or held is None:
        return "unavailable"
    if all(row["task_id"] in accepted for row in rows) and held[len(seen):] == events:
        return "applied"
    _put_back(runs_dir, [before[task] for task in accepted if task in before])
    return "refused"


def _settle_merges(
    runs_dir: Path, initiative_id: str, items: list[dict], sources: Mapping[str, tuple[Path, str]]
) -> tuple[list[dict], list[dict], list[str]]:
    """Edge. `(merged_items, merges, states)` once no merge is refused. Each round sends every merge to the store; when
    any is refused, the round's applied merges are put back, the refused tickets are held out of the merge, and the
    rest is merged again. `merge_same_phase` rewrites a `needs` from a dropped id to its survivor across all groups,
    so a refused group's rewrite must not reach the groups that still apply."""
    before = _rows_before(runs_dir, initiative_id, sources)
    held_out: set[str] = set()
    while True:
        merged, merges = route.merge_same_phase([{**i, "state": "held"} if i["id"] in held_out else i for i in items])
        head = _initiative_head(runs_dir, initiative_id)
        states = [
            _store_merge(runs_dir, initiative_id, merged, merge, [sources[m][0] for m in merge["members"]], before) for merge in merges
        ]
        refused = [merge for merge, state in zip(merges, states) if state == "refused"]
        if not refused:
            states_of = {i["id"]: i["state"] for i in items}
            return [{**m, "state": states_of[m["id"]]} if m["id"] in held_out else m for m in merged], merges, states
        applied = [merge for merge, state in zip(merges, states) if state == "applied"]
        _put_back(runs_dir, [before[m] for merge in applied for m in merge["members"] if m in before])
        if applied and head is not None:
            _put_back(runs_dir, [head])
        for merge in refused:
            held_out.update(merge["members"])
            print(f"refused: {' + '.join(merge['members'])} -> {merge['into']}")


_MERGE_RUNS_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar("merge_runs_dir", default=None)


@contextlib.contextmanager
def _merging_into(runs_dir: Path | None):
    """Names the runs directory (the profile's `workspace_dir/runs`) that `_merge_initiative_tickets` calls in the
    block send their merges to. A context, not an argument, because `route launch` calls the merge with the
    initiative directory alone. Outside any block, or given None, the merge has no store and writes files only."""
    token = _MERGE_RUNS_DIR.set(runs_dir)
    try:
        yield
    finally:
        _MERGE_RUNS_DIR.reset(token)


def _merge_initiative_tickets(initiative_dir: Path) -> list[dict]:
    """`route.merge_same_phase` on the tickets under `initiative_dir`, each merge sent to the store before its files.
    The store is `runs/` under the profile's `workspace_dir` (`_merging_into`), and only for that workspace's own
    `work/<initiative>`. A refused merge writes no file; the rest are merged without it (`_settle_merges`).
    Without a store the files are written as before, each after the unrecorded warning `write_filed_item` prints.
    The returned list drops the `dropped` entries; each item carries `file` for `route.launch_blockers`.
    """
    root = initiative_dir.resolve()  # absolute, so the export baselines below are not skipped
    paths = sorted(root.glob("*/*.md"))
    texts = [path.read_text(encoding="utf-8") for path in paths]
    items = [
        {
            "task": fields.get("id", path.stem),
            "id": fields.get("id", path.stem),
            "title": fields.get("title", fields.get("id", path.stem)),
            "state": fields.get("state", "todo"),
            "phase": fields.get("phase", path.parent.name),
            "surfaces": fields.get("surfaces", []),
            "body": body,
            "needs": fields.get("needs", []),
            "file": f"{path.parent.name}/{path.name}",
        }
        for path, text in zip(paths, texts)
        for fields, body in [route.parse_frontmatter(text)]
    ]
    sources = {item["id"]: (path, text) for path, text, item in zip(paths, texts, items)}
    runs_dir = _MERGE_RUNS_DIR.get()
    mode = _store_mode(runs_dir, root)
    if mode == "up":
        merged_items, merges, states = _settle_merges(runs_dir, root.name, items, sources)
    else:
        merged_items, merges = route.merge_same_phase(items)
        states = [mode] * len(merges)
        if merges and mode == "outside":
            print(f"routing: {root} is not an initiative of the workspace {runs_dir.parent}; merging its files without the store")
    merged_of = {item["id"]: item for item in merged_items}
    for merge, state in zip(merges, states):
        written = [(sources[m][0], route.merged_ticket_text(sources[m][1], merged_of[m])) for m in merge["members"]]
        for path, _ in written:
            if notice := _unfiled_notice(state, path, root.name):
                print(notice)
        for path, text in written:
            path.write_text(text, encoding="utf-8")
        if runs_dir is not None:
            queue_export.record_written_paths(runs_dir.parent, [path for path, _ in written])
        for line in route.merge_report_lines([merge]):
            print(line)
    return [item for item in merged_items if item.get("state") != "dropped"]


_SURFACE_RULES = ("surface_missing", "surface_exists", "surface_wrong_repo")


def _lint_checkout(repo: str | None, items: list[dict]) -> tuple[frozenset[str], dict[str, frozenset]] | None:
    """`(paths, other_repo_tops)` for `lint_items`; None when `repo` is unset or lists no files."""
    paths = checkout.read_checkout(repo) if repo else frozenset()
    if not paths:
        return None
    home = os.path.basename(repo.rstrip("/"))
    surface_tops = {s.split("/", 1)[0] for item in items for s in item.get("surfaces", []) if "/" in s}
    foreign = surface_tops - route.top_dirs(paths)
    # Stands down like cross_repo: an unregistered repo, or no surface outside the repo's own top directories,
    # reads no other repo. Wrong belief this guards: that every repo with Coxswain siblings is a Coxswain repo.
    # Other repos are found only as flat siblings of `repo`; in a worktree with no siblings each listing is
    # empty, other_repo_tops is {}, and surface_wrong_repo stays silent rather than misfiring.
    names = [name for name in route._REPO_ROOTS if name != home] if home in route._REPO_ROOTS and foreign else []
    tops = {name: route.top_dirs(checkout.read_checkout(Path(repo).parent / name)) for name in names}
    return paths, {name: found for name, found in tops.items() if found}


def _route_lint_dir(a: argparse.Namespace) -> Path | None:
    """Edge. The lint argument as an existing directory, else as an initiative id under the profile's
    `workspace_dir/work`. None, after printing an error that names both forms, when neither exists."""
    given = Path(a.initiative_dir)
    if given.is_dir():
        return given
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        profile = {}
    workspace = profile.get("workspace_dir", "")
    by_id = Path(workspace).expanduser() / "work" / a.initiative_dir if workspace else None
    if by_id is not None and by_id.is_dir():
        return by_id
    print(
        f"routing: {a.initiative_dir} is neither an initiative directory nor a known initiative id "
        "(pass a directory path, or an initiative id found under work/ in the profile's workspace_dir)"
    )
    return None


def _route_lint(a: argparse.Namespace) -> int:
    """work-shape.md §3: `cox route lint <initiative>`, before dispatch.
    `--repo` falls back to the initiative's own `repo:` frontmatter; only
    when neither names one does the reach rule stand down, and that is
    said once here rather than left for `lint_items` to flag every path.
    A skipped reach check with any surface listed fails: skipping is never a pass.
    """
    initiative_dir = _route_lint_dir(a)
    if initiative_dir is None:
        return 2
    runs_dir, _ = _runs_dir_for_land(argparse.Namespace(runs_dir=getattr(a, "runs_dir", None), profile=getattr(a, "profile", None)))
    with _merging_into(runs_dir):
        items = _merge_initiative_tickets(initiative_dir)
    repo = a.repo
    if not repo:
        init_path = initiative_dir / "initiative.md"
        if init_path.exists():
            init_fields, _ = route.parse_frontmatter(init_path.read_text(encoding="utf-8"))
            repo = init_fields.get("repo")
    skipped_with_surfaces = not repo and any(item.get("surfaces") for item in items)
    if not repo:
        print(f"routing: no repo found for {a.initiative_dir}; reach check skipped")
    home = os.path.expanduser("~")
    # `lint_items` compares the recorded form against `home`; the checkout listing needs a real directory.
    checkout_repo = repo_path.expand(repo, home) if repo else repo
    problems = route.lint_items(
        items, repo, ("pytest", "git status", "git diff"), checkout=_lint_checkout(checkout_repo, items), home=home,
    )
    for problem in problems:
        print(f"{problem.task} {problem.rule}: {problem.detail} -> {problem.fix}")
    if skipped_with_surfaces:
        print("routing: surfaces are listed but the reach check was skipped; name a repo with repo: or --repo")
    failing = ("reach", "coupling", "phase_needs", "home_path", *_SURFACE_RULES)
    return 2 if skipped_with_surfaces or any(p.rule in failing for p in problems) else 0


def _route_groups(a: argparse.Namespace) -> int:
    """work-shape.md §5: `cox route groups` prints the newest
    `plans/intake-groups/<date>.md` file under the profile's workspace, or
    `no groups filed` when none exist.
    """
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    groups_dir = Path(workspace).expanduser() / "plans" / "intake-groups"
    names = sorted(p.name for p in groups_dir.glob("*.md")) if groups_dir.is_dir() else []
    latest = route.latest_groups_file(names)
    if latest is None:
        print("no groups filed")
        return 0
    print(str(groups_dir / latest))
    return 0


def _route_launch_sweep(a: argparse.Namespace) -> int:
    """work-shape.md §1: no harness graph exists yet, so only `--dry-run` runs."""
    argv = route.build_sweep_argv(a.idea, a.initiative_id, a.label)
    if a.dry_run:
        print(f"dry-run: {' '.join(argv)}")
        return 0
    print("routing: sweep has no harness graph to launch yet; use --dry-run")
    return 2
