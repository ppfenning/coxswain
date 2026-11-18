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
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from agent_tools import courier, forge, lander, repo_path, route, run_store, work_state
from agent_tools.forge import ForgeModule
from agent_tools.store_url import read_provider_profile
from core.jsontypes import is_mapping
from core.landing_record import LandingRecord, record_from_store
from harness.store_dialect import Connection

WAIT_CHECKS_S = 1800.0  # the bound on a wait for a forge that cannot poll its checks once
# The lander's lanes run on threads of one process. A move of a work file and its record in the workspace's
# written-paths manifest (`queue_export.record_written`) is a read-modify-write of files every lane shares, so the
# ports that make one (mark_done, file_rectify, split_blocked) take this lock.
WORKSPACE_WRITES = threading.Lock()
GATES = ("level", "ticket")  # check-gate names (harness.check_gate), not `land_at` values


def worker_name() -> str:
    return f"lander:{socket.gethostname()}:{os.getpid()}"


def now_stamp() -> str:
    from harness import landing_ops

    return landing_ops.stamp(datetime.now(UTC))


def connect(runs_dir: Path) -> Connection:
    """Edge. The workspace's store, with the landing tables in place. A store another opener already migrated is left alone."""
    from harness.store_dialect import connect as open_url
    from harness.store_migrate import default_modules, migrate

    conn = open_url(run_store.store_url_for(runs_dir))
    migrate(conn, now_stamp(), default_modules())
    return conn


def _sqlite_records(path: Path) -> list[LandingRecord]:
    """Edge. The records of a sqlite store, read-only: no file is created, no journal mode is set, nothing is touched."""
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = conn.execute("SELECT record_json FROM landing_records ORDER BY initiative").fetchall()
    finally:
        conn.close()
    return [record_from_store(json.loads(row[0]) or {}) for row in rows]


def read_records(runs_dir: Path, log: Callable[[str], None] | None = None) -> list[LandingRecord]:
    """Edge. Every landing record, for a reader that only looks (the chair's tick). Never migrates and never raises:
    a store with no landing tables yet, or one that cannot be reached, reads as no records, so nothing reads as merged."""
    try:
        from harness import landing_store
        from harness.store_dialect import connect as open_url

        url = run_store.store_url_for(Path(runs_dir))
        if url.startswith("sqlite:"):
            return _sqlite_records(run_store.sqlite_path(url))
        conn = open_url(url)
        try:
            return landing_store.list_records(conn)
        finally:
            conn.close()
    except Exception as exc:
        if log is not None:
            log(f"note: landing records not read ({type(exc).__name__}: {exc}); no phase reads as merged this tick")
        return []


def _frontmatter(path: Path) -> dict[str, object]:
    try:
        fields: dict[str, object] = dict(route.parse_frontmatter(path.read_text(encoding="utf-8"))[0])
        return fields
    except (OSError, ValueError):
        return {}


def _ids(value: object) -> list[str]:
    """A row's `needs` as ids: none when absent or empty; TypeError, as iterating it raised, when it is not iterable."""
    raw: object = value or []
    if not isinstance(raw, Iterable):
        raise TypeError(f"'{type(raw).__name__}' object is not iterable")
    return [str(n) for n in cast(Iterable[object], raw)]


def _extra(value: object) -> Mapping[str, object]:
    """A row's `extra`: empty when absent; TypeError, as spreading it raised, when it is not a mapping. Read as empty,
    it would drop the initiative's repo, on_blocked and held."""
    raw: object = value or {}
    if not is_mapping(raw):
        raise TypeError(f"an extra must be a mapping, not {type(raw).__name__}")
    return raw


def task_rows(store_rows: Sequence[Mapping[str, object]], files: Mapping[str, Mapping[str, object]]) -> list[dict[str, object]]:
    """Pure. The lander's task rows. The store's row wins for state, phase, needs and the merged commit; the work
    file adds the title, and the needs when the row holds none. A task only the files know (a workspace that keeps state in files) is read from its file."""
    rows: list[dict[str, object]] = []
    stored = {str(r["task_id"]): r for r in store_rows}
    for ident in sorted({*stored, *files}):
        row, item = stored.get(ident) or {}, files.get(ident) or {}
        rows.append({
            "id": ident,
            "phase": str(row.get("phase") or item.get("phase") or ""),
            "state": str(row.get("state") or item.get("state") or ""),
            "needs": _ids(row.get("needs") or item.get("needs") or []),
            "merged_commit": row.get("merged_commit") or item.get("merged_commit"),
            "title": str(item.get("title") or ident),
        })
    return rows


def pr_merged_verdict(listed: Sequence[Mapping[str, object]] | str) -> tuple[bool, str]:
    """Pure. Whether the branch's pull request merged, from the forge's listing of every PR whose head it is."""
    if isinstance(listed, str):
        return False, listed
    merged = [p for p in listed if p.get("state") == "merged"]
    return (True, f"#{merged[0]['number']}") if merged else (False, "")


def waited_state(green: bool, detail: str) -> str:
    """Pure. A blocking `wait_checks` answer as a poll state: one that ran out of time, or saw only pending checks, is
    `pending`; anything else not green is `failed`."""
    if green:
        return "green"
    return "pending" if "pending" in detail or "timed out" in detail or "no checks reported" in detail else "failed"


class Forge:
    """The forge module the profile names, plus the one question the lander adds: whether a branch's PR merged."""

    def __init__(self, module: ForgeModule | None) -> None:
        self._module = module

    def _forge(self) -> ForgeModule:
        if self._module is None:
            raise AttributeError("no forge module is installed under the profile's forge name")
        return self._module

    def push(self, repo: Path, branch: str) -> tuple[bool, str]:
        return self._forge().push(repo, branch)

    def find_open_prs(self, repo: Path, branch: str) -> Sequence[object] | str:
        return self._forge().find_open_prs(repo, branch)

    def open_pr(self, repo: Path, title: str, body: str, *, head: str | None = None,
                base: str | None = None) -> tuple[bool, str]:
        return self._forge().open_pr(repo, title, body, head=head, base=base)

    def merge(self, repo: Path, step: Mapping[str, object]) -> tuple[bool, str]:
        return self._forge().merge(repo, step)

    def pr_state(self, url: str) -> Mapping[str, object]:
        return self._forge().pr_state(url)

    def pr_merged(self, repo: Path, branch: str) -> tuple[bool, str]:
        # Through the registry's dispatcher: the local forge has no pull requests to list, and says so.
        return pr_merged_verdict(forge.branch_prs(self._module, repo, branch))

    def delete_branch(self, repo: Path, branch: str) -> tuple[bool, str]:
        return forge.delete_branch(self._module, repo, branch)

    def poll_checks(self, repo: Path, *, ref: str, memo: Mapping[str, object] | None = None) -> tuple[str, str, dict[str, object]]:
        """One read of `ref`'s checks: (state, detail, memo), as `forge.py` describes. A forge with no single read is
        waited on as before, bounded by `WAIT_CHECKS_S`; that wait holds only its own repository's lane."""
        poll = getattr(self._module, "poll_checks", None)
        if poll is not None:
            return poll(repo, ref=ref, memo=memo)
        green, detail = self._forge().wait_checks(repo, WAIT_CHECKS_S, ref=ref)
        return waited_state(green, detail), detail, dict(memo or {})

    def has_ci(self, repo: Path, ref: str) -> bool:
        """Whether `ref` carries a GitHub Actions workflow, the only CI this forge can report on. A read that fails
        answers yes, so a land never skips checks it could not rule out."""
        listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", ref, ".github/workflows/"],
                                capture_output=True, text=True, timeout=30)
        return listed.returncode != 0 or any(
            name.endswith((".yml", ".yaml")) for name in listed.stdout.split()
        )

    def retarget(self, repo: Path, branch: str, base: str) -> tuple[bool, str]:
        """Point the open pull request from `branch` at a new base: how a stack's layer moves down when the one below lands."""
        edit = getattr(self._module, "_gh_on_open_pr", None)
        if edit is None:
            return False, "this forge cannot retarget a pull request"
        return edit(repo, branch, ["edit", "--base", base])


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


def run_gate(repo: Path, tree: Path, gate: str, landing: Mapping[str, object], error: str | None) -> tuple[bool, str]:
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


def follow_up_for(work: Path, initiative: str) -> str:
    """Edge (reads). The follow-up initiative a split of `initiative` uses: the one an earlier, unfinished split of it
    made (its `initiative.md` says `split_from`), else the first free `<id>-2`, `-3`."""
    from core import landing_record as lr

    taken = sorted(p.parent.name for p in Path(work).glob("*/initiative.md"))
    made = [name for name in taken if _frontmatter(Path(work) / name / "initiative.md").get("split_from") == initiative]
    return made[-1] if made else lr.follow_up_id(initiative, taken)


def split_initiative(ws: Path, conn: Connection, repo: Path, initiative: str, phases: Sequence[str], *, worker: str,
                     push: Callable[[Path, str], tuple[bool, str]] | None = None) -> str:
    """Edge. Move `phases` of `initiative` to a follow-up initiative. Returns the follow-up's id.

    Nothing built is lost or rebuilt: each phase branch is copied under the follow-up's name at the same tip (the
    original is left where it is), its task files move with their state, and their store rows move with their state
    and merged commit. The moved tasks keep their needs, which now name tasks of the first initiative: a need in
    another initiative is met only once that task is done, so the follow-up waits for the first to land.
    Every step reads what is already so, so a split that died part-way finishes when it runs again."""
    import shutil

    import yaml

    from agent_tools import queue_export
    from core import workstore
    from harness import landing_git as lg
    from harness import landing_store

    work = Path(ws) / "work"
    follow = follow_up_for(work, initiative)
    for phase in phases:
        old, new = f"epic/{initiative}/{phase}", f"epic/{follow}/{phase}"
        tip = lg.rev(repo, f"refs/heads/{old}")
        if tip is not None and lg.rev(repo, f"refs/heads/{new}") is None:
            ok, why = lg.git(repo, "branch", "--no-track", new, tip)
            if not ok:
                raise RuntimeError(f"could not copy {old} to {new}: {why}")
            if push is not None:
                push(repo, new)
    overview = work / follow / "initiative.md"
    if not overview.is_file():
        meta, body = workstore.split_frontmatter((work / initiative / "initiative.md").read_text(encoding="utf-8"), overview)
        meta = {**meta, "id": follow, "title": f"{meta.get('title') or initiative} (follow-up)", "split_from": initiative}
        note = f"Split from `{initiative}`: phases {', '.join(phases)} were blocked there and moved here with what they had built."
        overview.parent.mkdir(parents=True, exist_ok=True)
        overview.write_text(f"---\n{yaml.safe_dump(meta, sort_keys=False).strip()}\n---\n\n{note}\n\n{body}\n", encoding="utf-8")
    moved_files = [overview]
    for phase in phases:
        source, target = work / initiative / phase, work / follow / phase
        if source.is_dir() and not target.exists():
            shutil.move(str(source), str(target))
        moved_files += sorted(target.glob("*.md")) if target.is_dir() else []
    mark = conn.dialect.placeholder
    stamp = now_stamp()
    from harness import store_read

    for row in [r for r in store_read.work_items(conn, initiative) if str(r.get("phase")) in set(phases)]:
        task = str(row["task_id"])
        # The row moves whole, in one statement: its text, its state and its merged commit all go with it.
        conn.execute(
            f"UPDATE work_items SET initiative = {mark}, updated_at = {mark}, updated_by = {mark} "
            f"WHERE initiative = {mark} AND task_id = {mark}",
            (follow, stamp, worker, initiative, task),
        )
    from harness import store_queue

    head = next(iter(store_queue.read(conn, initiative, "initiative")), None)
    if head is not None and not store_queue.read(conn, follow, "initiative"):
        # The follow-up's own row, so the store holds it and not only the file written above.
        store_queue.upsert(conn, {
            **{k: v for k, v in head.items() if k not in ("holder", "epoch", "expires_at", "issue_number", "project_item_id")},
            "initiative": follow, "task_id": follow, "title": f"{head.get('title') or initiative} (follow-up)",
            "extra": {**_extra(head.get("extra")), "id": follow, "split_from": initiative},
            "updated_at": stamp, "updated_by": worker,
        })
    record = landing_store.get_record(conn, initiative)
    if record is not None and any(p in (record.get("phases") or {}) for p in phases):
        kept = {p: e for p, e in (record.get("phases") or {}).items() if p not in set(phases)}
        landing_store.put_record(conn, {**record, "phases": kept, "updated_at": stamp})
    for phase in phases:
        conn.execute(
            f"UPDATE land_queue SET state = 'done', last_error = {mark}, updated_at = {mark} "
            f"WHERE initiative = {mark} AND phase = {mark} AND state IN ('queued', 'failed')",
            (f"moved to {follow}", stamp, initiative, phase),
        )
    queue_export.record_written_paths(ws, moved_files)
    return follow


def needed_initiatives(work: Path, initiative: str) -> list[str]:
    """Edge (reads). The other initiatives whose tasks this initiative's tasks need, in first-need order."""
    from core import workstore

    own = {p.stem: p for p in sorted((Path(work) / initiative).glob("*/*.md"))}
    found: list[str] = []
    for path in own.values():
        try:
            needs = [str(n) for n in workstore.read_item(path).get("needs") or []]
        except Exception:
            continue
        for need in needs:
            if need in own:
                continue
            owner = next((p.parent.parent.name for p in sorted(Path(work).glob(f"*/*/{need}.md"))), None)
            if owner and owner != initiative and owner not in found:
                found.append(owner)
    return found


def stack_base(ws: Path, initiative: str, epic: str, epic_branch: str | None, records: Sequence[Mapping[str, object]]) -> str | None:
    """Edge (reads). With `epic_stack` on, the branch `initiative` forks from: see `core.landing.stack_parent`."""
    from core.landing import stack_parent

    members = {
        str(r["initiative"]): {"branch": r.get("branch"), "landed": r.get("state") == "landed"}
        for r in records if r.get("epic") == epic
    }
    return stack_parent(needed_initiatives(Path(ws) / "work", initiative), members, epic_branch)


def send_back(ws: Path, conn: Connection, worker: str, mode: str) -> Callable[[str, str, str], str | None]:
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


def build(ws: Path, profile: Mapping[str, object], *, worker: str | None = None, log: Callable[[str], None] = print,
          conn: Connection | None = None, forge_module: ForgeModule | None = None) -> lander.Ports:
    """Edge. The ports for the workspace `ws` under routing profile `profile`."""
    from agent_tools import cli
    from core import workstore
    from core.cartridge import OVERLAY_ALLOWED_KEYS
    from core.landing import resolve_epic_stack
    from harness import store_queue, store_read

    ws = Path(ws)
    runs_dir, work = ws / "runs", ws / "work"
    home = str(Path.home())
    name = worker or worker_name()
    store = conn if conn is not None else connect(runs_dir)
    module = forge_module or forge.forge_for(forge.forge_name(dict(profile)))
    provider_profile = profile.get("provider_profile")
    provider: Mapping[str, object] = read_provider_profile(str(provider_profile)) if provider_profile else {}
    mode = work_state.work_state_mode(provider)

    def board(initiative: str) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        """(the initiative's own fields, its tasks' fields by id), from the store's rows. A workspace whose board is
        its files (no rows for the initiative) is read from the files instead."""
        rows = store_queue.read(store, initiative)
        head = next((r for r in rows if r.get("kind") == "initiative"), None)
        if head is None:
            return _frontmatter(work / initiative / "initiative.md"), files_of(initiative)
        extra = _extra(head.get("extra"))
        own: dict[str, object] = {**extra, "title": head.get("title") or extra.get("title") or initiative}
        return own, {
            str(r["task_id"]): {"title": r.get("title") or r["task_id"], "phase": r.get("phase"), "state": r.get("state"),
                                "needs": _ids(r.get("needs"))}
            for r in rows if r.get("kind") == "task"
        }

    def repo_of(record: LandingRecord) -> Path:
        named = board(lander.record_text(record, "initiative"))[0].get("repo")
        if isinstance(named, str) and named:
            return Path(repo_path.expand(named, home))
        return Path(str(record.get("repo") or "")).expanduser()  # `lander.ports_repo` refuses an empty or relative one

    def files_of(initiative: str) -> dict[str, dict[str, object]]:
        found: dict[str, dict[str, object]] = {}
        for path in sorted((work / initiative).glob("*/*.md")):
            fields: dict[str, object]
            try:
                fields = dict(workstore.read_item(path))
            except Exception:  # a task file that does not parse still names its task; the store row carries the rest
                fields = _frontmatter(path)
            found[str(fields.get("id") or path.stem)] = {**fields, "phase": path.parent.name}
        return found

    def tasks(initiative: str) -> list[dict[str, object]]:
        # The initiative's own row and its intake's row live in the same table; neither is a task to merge or land.
        other = {str(r["task_id"]) for r in store_queue.read(store, initiative) if r.get("kind") not in (None, "task")}
        stored = [r for r in store_read.work_items(store, initiative) if str(r["task_id"]) not in other]
        return task_rows(stored, board(initiative)[1])

    def meta(initiative: str) -> lander.LandMeta:
        own: dict[str, object] = {**board(initiative)[0], "id": initiative}
        repo = Path(repo_path.expand(str(own["repo"]), home)) if own.get("repo") else None
        cartridge: dict[str, object]
        error: str | None
        cartridge, error = cli._team_cartridge(dict(profile), repo, OVERLAY_ALLOWED_KEYS)
        epics: dict[str, dict[str, object]] = cli._read_epics(ws)[0]
        epic = epics.get(str(own.get("epic"))) if own.get("epic") else None
        stack = bool(epic) and resolve_epic_stack(cartridge, (epic or {}).get("epic_stack"))
        landing_areas = cartridge.get("landing_areas")
        return {"initiative": own, "cartridge": cartridge, "cartridge_error": error,
                "epic_issue": (epic or {}).get("issue"), "gate": cli._resolved_gate_level(runs_dir),
                "epic_stack": stack, "epic_branch": (epic or {}).get("target_branch"), "epic_title": (epic or {}).get("title"),
                # Where the epic branch itself lands: the cartridge's target branch, else the repo's default branch.
                "epic_parent": (landing_areas or {}).get("target_branch") if stack and is_mapping(landing_areas) else None}

    def checks(repo: Path, tree: Path, gate: str) -> tuple[bool, str]:
        landing: Mapping[str, object]
        error: str | None
        landing, error = cli._cartridge_landing(repo, dict(profile))
        return run_gate(repo, tree, gate, landing, error)

    def mark_done(initiative: str, task: str, pr: str) -> str | None:
        phase = str(next((t["phase"] for t in tasks(initiative) if t["id"] == task), ""))
        item = work / initiative / phase / f"{task}.md"
        with WORKSPACE_WRITES:
            return cli._close_approved_item(str(item), runs_dir=runs_dir, by=name, merged=True, mode=mode)

    def file_rectify(initiative: str, phase: str, title: str, body: str) -> str | None:
        with WORKSPACE_WRITES:
            return _file_rectify(initiative, phase, title, body)

    def _file_rectify(initiative: str, phase: str, title: str, body: str) -> str | None:
        """One ready task in `phase`, as a work file and a store row. The phase's branch is the tree it builds on."""
        from agent_tools import queue_export
        from harness import landing_ops

        ident, exists = landing_ops.rectify_task(phase, tasks(initiative))
        if exists:
            return ident
        path = work / initiative / phase / f"{ident}.md"
        try:
            workstore.write_item({"id": ident, "phase": phase, "state": "ready", "needs": [], "surfaces": [],
                                  "title": title, "body": body}, path)
            # The whole ticket goes to the store, text and all: the store is the board and the file is its copy.
            store_queue.upsert(store, {
                "kind": "task", "initiative": initiative, "task_id": ident, "phase": phase, "state": "ready",
                "needs": [], "surfaces": [], "title": title, "body": body, "extra": {"id": ident, "phase": phase},
                "updated_at": now_stamp(), "updated_by": name,
            })
        except Exception as exc:
            log(f"lander: rectify task for {initiative}/{phase} was not filed: {type(exc).__name__}: {exc}")
            return None
        queue_export.record_written_paths(ws, [path])
        return ident

    def split_blocked(initiative: str, phases: Sequence[str]) -> str | None:
        try:
            repo = lander.ports_repo(ports_ref[0], {"initiative": initiative})
            with WORKSPACE_WRITES:
                return split_initiative(ws, store, repo, initiative, list(phases), worker=name, push=getattr(module, "push", None))
        except Exception as exc:
            log(f"lander: {initiative} was not split: {type(exc).__name__}: {exc}")
            return None

    def notify(kind: str, fields: Mapping[str, object]) -> None:
        initiative = str(fields.get("initiative") or "")
        note = f"lander: {kind}: {initiative}" + (f" phase {fields['phase']}" if fields.get("phase") else "") + f": {fields.get('reason') or ''}"
        entry = courier.send(
            courier.Reference("initiative", initiative), name, "chair", note.strip(), uuid.uuid4().hex,
            event="needs_attention", fields={k: v if isinstance(v, str) else json.dumps(v) for k, v in fields.items()},
            topic=initiative or None,
        )
        courier.append_file(ws / "courier.jsonl", entry)

    ports_ref: list[lander.Ports] = []
    ports_ref.append(lander.Ports(
        conn=store, worker=name, now=now_stamp, tasks=tasks, meta=meta, forge=Forge(module), checks=checks,
        mark_merged=lambda initiative, task, tip: None,  # the landing record is the one place that says a phase merged
        mark_done=mark_done, file_rectify=file_rectify, split_blocked=split_blocked, notify=notify, repo=repo_of,
        fetch=fetch_branch, log=log,
    ))
    return ports_ref[0]
