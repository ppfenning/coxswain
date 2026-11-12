"""The lander's real ports: the store, the work files, the cartridge, the forge and the courier of one workspace.

`lander.py` holds the logic and reaches the world only through `lander.Ports`. This module builds those ports for a
real workspace. Each port is small and reads what is true when it is called, so the lander can be run again at any
point. Nothing here decides anything about landing.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import courier, forge, lander, repo_path, route, run_store, work_state
from agent_tools.store_url import read_provider_profile

GATES = ("level", "ticket")  # check-gate names (harness.check_gate), not `land_at` values


def worker_name() -> str:
    return f"lander:{socket.gethostname()}:{os.getpid()}"


def now_stamp() -> str:
    from harness import landing_ops

    return landing_ops.stamp(datetime.now(UTC))


def connect(runs_dir: Path) -> Any:
    """Edge. The workspace's store, with the landing tables in place. A store another opener already migrated is left alone."""
    from harness.store_dialect import connect as open_url
    from harness.store_migrate import default_modules, migrate

    conn = open_url(run_store._store_url(runs_dir))
    migrate(conn, now_stamp(), default_modules())
    return conn


def _sqlite_records(path: Path) -> list[dict[str, Any]]:
    """Edge. The records of a sqlite store, read-only: no file is created, no journal mode is set, nothing is touched."""
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = conn.execute("SELECT record_json FROM landing_records ORDER BY initiative").fetchall()
    finally:
        conn.close()
    return [dict(json.loads(row[0]) or {}) for row in rows]


def read_records(runs_dir: Path, log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Edge. Every landing record, for a reader that only looks (the chair's tick). Never migrates and never raises:
    a store with no landing tables yet, or one that cannot be reached, reads as no records, so nothing reads as merged."""
    try:
        from harness import landing_store
        from harness.store_dialect import connect as open_url

        url = run_store._store_url(Path(runs_dir))
        if url.startswith("sqlite:"):
            return _sqlite_records(run_store._sqlite_path(url))
        conn = open_url(url)
        try:
            return landing_store.list_records(conn)
        finally:
            conn.close()
    except Exception as exc:
        if log is not None:
            log(f"note: landing records not read ({type(exc).__name__}: {exc}); no phase reads as merged this tick")
        return []


def _frontmatter(path: Path) -> dict[str, Any]:
    try:
        return dict(route.parse_frontmatter(path.read_text(encoding="utf-8"))[0])
    except (OSError, ValueError):
        return {}


def task_rows(store_rows: Sequence[Mapping[str, Any]], files: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pure. The lander's task rows. The store's row wins for state, phase, needs and the merged commit; the work
    file adds the title, and the needs when the row holds none. A task only the files know (a workspace that keeps state in files) is read from its file."""
    rows = []
    stored = {str(r["task_id"]): r for r in store_rows}
    for ident in sorted({*stored, *files}):
        row, item = stored.get(ident) or {}, files.get(ident) or {}
        rows.append({
            "id": ident,
            "phase": str(row.get("phase") or item.get("phase") or ""),
            "state": str(row.get("state") or item.get("state") or ""),
            "needs": [str(n) for n in row.get("needs") or item.get("needs") or []],
            "merged_commit": row.get("merged_commit") or item.get("merged_commit"),
            "title": str(item.get("title") or ident),
        })
    return rows


OPEN_STATES = frozenset({"todo", "ready", "in_progress", "blocked", "quarantined"})


def rectify_task(phase: str, tasks: Sequence[Mapping[str, Any]]) -> tuple[str, bool]:
    """Pure. (the rectify task's id for `phase`, whether it is one already open).

    A phase has at most one open rectify task: a merge that fails again before it is built names the same task.
    A new one is `<phase>-rectify`, then `-2`, `-3`: the first id no task of the initiative has."""
    stem = f"{phase}-rectify"
    mine = [t for t in tasks if str(t.get("phase")) == phase and (str(t["id"]) == stem or str(t["id"]).startswith(f"{stem}-"))]
    still_open = [str(t["id"]) for t in mine if str(t.get("state")) in OPEN_STATES]
    if still_open:
        return sorted(still_open)[-1], True
    taken = {str(t["id"]) for t in tasks}
    number = 1
    while (ident := stem if number == 1 else f"{stem}-{number}") in taken:
        number += 1
    return ident, False


def pr_merged_verdict(listed: Sequence[Mapping[str, Any]] | str) -> tuple[bool, str]:
    """Pure. Whether the branch's pull request merged, from the forge's listing of every PR whose head it is."""
    if isinstance(listed, str):
        return False, listed
    merged = [p for p in listed if p.get("state") == "merged"]
    return (True, f"#{merged[0]['number']}") if merged else (False, "")


class Forge:
    """The forge module the profile names, plus the one question the lander adds: whether a branch's PR merged."""

    def __init__(self, module: Any) -> None:
        self._module = module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    def pr_merged(self, repo: Path, branch: str) -> tuple[bool, str]:
        return pr_merged_verdict(self._module.find_branch_prs(repo, branch))


def fetch_branch(repo: Path, branch: str) -> str | None:
    """Edge. Bring `branch` up to origin's copy, forward only. A reason only when it diverged: it must not be moved here."""
    from harness.phase_remote import Kind, materialise_local
    from harness.phase_remote import fetch_branch as fetch

    outcome = fetch(repo, branch)
    if outcome.kind == Kind.FETCHED:
        materialise_local(repo, branch)
    if outcome.kind == Kind.DIVERGED:
        return f"{branch} has diverged from origin and is not moved here: {outcome.detail}"
    return None


def run_gate(repo: Path, tree: Path, gate: str, landing: Mapping[str, Any], error: str | None) -> tuple[bool, str]:
    """Edge. Run one gate's checks in `tree`. A cartridge that does not resolve fails the gate: it would run fewer checks."""
    from harness.check_gate import gate_checks
    from harness.checks import run_checks

    if error:
        return False, f"cartridge checks unavailable ({error})"
    checks = gate_checks(landing, gate)
    if not checks:
        return True, ""
    results = run_checks(tree, checks)
    failed = [r for r in results if not r.get("passed")]
    if not failed:
        return True, ""
    return False, "\n\n".join(
        f"{r.get('name')}: {r.get('cmd')}\n{str(r.get('excerpt') or r.get('output_tail') or r.get('error') or '').strip()}" for r in failed
    )


def send_back(ws: Path, conn: Any, worker: str, mode: str) -> Callable[[str, str, str], str | None]:
    """Edge. Adopt's way to return an approved task whose work cannot be found to `ready`: the store row first (only
    from `approved`), then the work file. The reason is logged by the caller; why it was refused, else None."""
    from core import workstore
    from harness import store_work_state

    def move(initiative: str, task: str, reason: str) -> str | None:
        paths = sorted((Path(ws) / "work" / initiative).glob(f"*/{task}.md"))
        try:
            if mode == "store":
                done = store_work_state.set_state(conn, initiative, task, "ready", worker, None, now_stamp(), expected="approved")
                if not isinstance(done, dict):
                    return f"the store did not move it from approved ({done})"
            if paths and workstore.read_item(paths[0])["state"] == "approved":
                workstore.set_state(paths[0], "ready")
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    return move


def initiatives_in(ws: Path) -> list[str]:
    """Edge. Every initiative with an `initiative.md` under the workspace's `work/`, by name."""
    return sorted(p.parent.name for p in (Path(ws) / "work").glob("*/initiative.md"))


def build(ws: Path, profile: Mapping[str, Any], *, worker: str | None = None, log: Callable[[str], None] = print,
          conn: Any = None, forge_module: Any = None) -> lander.Ports:
    """Edge. The ports for the workspace `ws` under routing profile `profile`."""
    from agent_tools import cli
    from core import workstore
    from core.cartridge import OVERLAY_ALLOWED_KEYS
    from harness import store_read

    ws = Path(ws)
    runs_dir, work = ws / "runs", ws / "work"
    home = str(Path.home())
    name = worker or worker_name()
    conn = conn if conn is not None else connect(runs_dir)
    module = forge_module or forge.forge_for(forge.forge_name(dict(profile)))
    provider = read_provider_profile(profile.get("provider_profile") or "") if profile.get("provider_profile") else {}
    mode = work_state.work_state_mode(provider)

    def repo_of(record: Mapping[str, Any]) -> Path:
        named = _frontmatter(work / str(record["initiative"]) / "initiative.md").get("repo")
        if isinstance(named, str) and named:
            return Path(repo_path.expand(named, home))
        return Path(str(record.get("repo") or "")).expanduser()  # `lander._repo` refuses an empty or relative one

    def files_of(initiative: str) -> dict[str, dict[str, Any]]:
        found = {}
        for path in sorted((work / initiative).glob("*/*.md")):
            try:
                fields = dict(workstore.read_item(path))
            except Exception:  # a task file that does not parse still names its task; the store row carries the rest
                fields = _frontmatter(path)
            found[str(fields.get("id") or path.stem)] = {**fields, "phase": path.parent.name}
        return found

    def tasks(initiative: str) -> list[dict[str, Any]]:
        return task_rows(store_read.work_items(conn, initiative), files_of(initiative))

    def meta(initiative: str) -> dict[str, Any]:
        own = {**_frontmatter(work / initiative / "initiative.md"), "id": initiative}
        repo = Path(repo_path.expand(str(own["repo"]), home)) if own.get("repo") else None
        cartridge, error = cli._team_cartridge(dict(profile), repo, OVERLAY_ALLOWED_KEYS)
        epic = cli._read_epics(ws)[0].get(str(own.get("epic"))) if own.get("epic") else None
        return {"initiative": own, "cartridge": cartridge, "cartridge_error": error,
                "epic_issue": (epic or {}).get("issue"), "gate": cli._resolved_gate_level(runs_dir)}

    def checks(repo: Path, tree: Path, gate: str) -> tuple[bool, str]:
        landing, error = cli._cartridge_landing(repo, dict(profile))
        return run_gate(repo, tree, gate, landing, error)

    def mark_done(initiative: str, task: str, pr: str) -> str | None:
        phase = next((t["phase"] for t in tasks(initiative) if t["id"] == task), "")
        item = work / initiative / phase / f"{task}.md"
        return cli._close_approved_item(str(item), runs_dir=runs_dir, by=name, merged=True, mode=mode)

    def file_rectify(initiative: str, phase: str, title: str, body: str) -> str | None:
        """One ready task in `phase`, as a work file and a store row. The phase's branch is the tree it builds on."""
        from agent_tools import queue_export
        from harness import store_write

        ident, exists = rectify_task(phase, tasks(initiative))
        if exists:
            return ident
        path = work / initiative / phase / f"{ident}.md"
        try:
            workstore.write_item({"id": ident, "phase": phase, "state": "ready", "needs": [], "surfaces": [],
                                  "title": title, "body": body}, path)
            store_write.upsert_work_item(conn, initiative, ident, phase, "ready", [], now_stamp(), name)
        except Exception as exc:
            log(f"lander: rectify task for {initiative}/{phase} was not filed: {type(exc).__name__}: {exc}")
            return None
        queue_export.record_written_paths(ws, [path])
        return ident

    def notify(kind: str, fields: Mapping[str, Any]) -> None:
        initiative = str(fields.get("initiative") or "")
        note = f"lander: {kind}: {initiative}" + (f" phase {fields['phase']}" if fields.get("phase") else "") + f": {fields.get('reason') or ''}"
        entry = courier.send(
            courier.Reference("initiative", initiative), name, "chair", note.strip(), uuid.uuid4().hex,
            event="needs_attention", fields={k: v if isinstance(v, str) else json.dumps(v) for k, v in fields.items()},
        )
        courier.append_file(ws / "courier.jsonl", entry)

    return lander.Ports(
        conn=conn, worker=name, now=now_stamp, tasks=tasks, meta=meta, forge=Forge(module), checks=checks,
        mark_merged=lambda initiative, task, tip: None,  # the landing record is the one place that says a phase merged
        mark_done=mark_done, file_rectify=file_rectify, notify=notify, repo=repo_of, fetch=fetch_branch, log=log,
    )
