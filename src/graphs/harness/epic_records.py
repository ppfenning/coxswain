"""The epic driver's records: the store mirror of tasks and rows, pauses, the run's result and its exit."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

from core.jsontypes import is_list, is_mapping
from harness import work_mirror
from harness.courier_adapter import publish_run_exited
from harness.epic_context import Ctx, StoreRetry
from harness.epic_types import EpicItem, ItemRef, Quarantine, as_list, as_mapping, phase_record
from harness.resume import save_result, write_atomic
from harness.store_dialect import Connection, JsonObject
from harness.store_lease import assert_epoch, renew
from harness.store_pause import clear_paused, is_paused
from harness.store_write import Store, upsert_work_item
from harness.surface_heal import heal_bare_filenames, heal_new_markers, notes_out_of_surfaces
from harness.waves import widest_wave

if TYPE_CHECKING:  # annotation only: a module-level store_read import loads store_traces early (see `mirror_read`)
    from harness.store_read import WorkItemReadRow
    from harness.store_types import QueueRow

epic_log = logging.getLogger("harness.epic")  # the driver's logger keeps its name across the split (H9)


# The principal names the driver AND the graph whose work it records, so a
# ledger row from a swarm stays distinguishable from the same graph run alone.
PRINCIPAL = "epic-swarm(lifecycle-propose)"


# Distinct from a clean run's implicit 0, for a caller to act on the pause.
EXIT_PAUSED = 3

# A phase whose unlanded commits are all approved tasks with a land in flight: not a failure, not a pause.
EXIT_AWAITING_LAND = 4


# A mid-run mirror or a pause clear waits this long for a lost connection; the run's exit record gets the full five minutes.
END_WRITE_WINDOW_S = 60.0
_EXIT_WINDOW_S = 300.0


LEASE_NAME = "chair"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class StoreMissing(RuntimeError):
    """A store write or read on a run that opened no store: a wiring fault, named rather than an AttributeError."""


def ctx_store(ctx: Ctx) -> Store:
    """The run's open store. Every caller sits behind a check that the run has one, so None is a wiring fault."""
    if ctx.store is None:
        raise StoreMissing(f"run {ctx.run_id} of {ctx.initiative_id} reached a store call without an open store")
    return ctx.store


def mirror_by(ctx: Ctx) -> str:
    return f"epic-driver:{ctx.run_id}"


def _file_times(items: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Task id to the ISO mtime of its work file; an item with no readable file has no entry."""
    times: dict[str, str] = {}
    for item in items:
        with contextlib.suppress(OSError):
            if item.get("path"):
                times[str(item["id"])] = datetime.fromtimestamp(Path(str(item["path"])).stat().st_mtime, UTC).isoformat()
    return times


def _upsert_row(store: Store, row: Mapping[str, object]) -> None:
    upsert_work_item(
        store.conn, str(row["initiative"]), str(row["task_id"]), str(row["phase"]), str(row["state"]),
        [str(n) for n in as_list(row["needs"])], str(row["updated_at"]), str(row["updated_by"]),
    )  # fmt: skip


def repair_file_state(item: Mapping[str, object], new_state: str) -> bool:
    """Rewrite only the `state:` line of the item's work file, bytes otherwise kept. False when it could not."""
    if not item.get("path"):
        return False
    path = Path(str(item["path"]))
    try:
        text = path.read_bytes().decode("utf-8")
        repaired = work_mirror.set_frontmatter_state(text, new_state)
        if repaired is None:
            return False
        if repaired != text:
            path.write_bytes(repaired.encode("utf-8"))
    except (OSError, UnicodeError):
        return False
    return True


def _mirror_read_store(ctx: Ctx, items: Sequence[EpicItem], rows: Sequence[WorkItemReadRow]) -> None:
    """work_state: store. A row wins over its file: the file's state line is repaired and `items` follows the row.

    `items` is the driver's own copy, so the ready and needs checks that read it see the store's state. A task with
    no row is read from its file and upserted, as under files.
    """
    by_task = {row["task_id"]: row for row in rows}
    times = _file_times(items)
    for item in items:
        task, file_state = item["id"], item["state"]
        row = by_task.get(task)
        row_state = None if row is None else row["state"]
        decision = work_mirror.authority_decision("store", file_state, row_state)
        if decision == work_mirror.USE_STORE_REWRITE_FILE:
            stored = cast(str, row_state)
            if repair_file_state(item, stored):
                epic_log.warning("work file state rewritten from the store: task=%s old=%s new=%s", task, file_state, row_state)
            else:
                epic_log.warning("work file state not rewritable, store state used: task=%s file=%s", task, file_state)
            item["state"] = stored
        elif decision == work_mirror.USE_FILE_AND_UPSERT:
            new = work_mirror.item_row(ctx.initiative_id, item, times.get(str(task)) or utc_now(), mirror_by(ctx))
            _upsert_row(ctx_store(ctx), new)
        if row is not None:
            # A merge and its handoff live only in the row, so `ready_tasks` sees a merged parent in any run.
            item["merged"] = row["merged"]
            item["merged_commit"] = row["merged_commit"]
            item["handoff"] = row["handoff"]


def mirror_read(ctx: Ctx, items: Sequence[EpicItem]) -> None:
    """Mirror the files' state into work_items. Under work_state files the files stay authoritative: `items` is never touched.

    Under work_state store a stored row wins: see `_mirror_read_store`. A ctx with no work_state reads as files.
    A store error is logged and swallowed here, at the edge, so it can never fail the run.
    """
    if ctx.store is None:
        return
    try:
        # Not a module-level import: harness/__init__ imports this module, so a top-level store_read import
        # loads store_traces early and `python -m harness.store_traces` then warns a second line on stderr.
        from harness import store_read

        rows = store_read.work_items(ctx.store.conn, ctx.initiative_id)
        if getattr(ctx, "work_state", "files") == "store":
            _mirror_read_store(ctx, items, rows)
            return
        upserts, disagreements = work_mirror.plan_mirror(
            ctx.initiative_id, items, rows, _file_times(items), mirror_by(ctx), fallback_time=utc_now()
        )
        for row in upserts:
            _upsert_row(ctx.store, row)
        for d in disagreements:
            epic_log.warning(
                "work_items disagrees with the file: initiative=%s task=%s file_state=%s store_state=%s updated_by=%s",
                d["initiative"], d["task_id"], d["file_state"], d["store_state"], d["store_updated_by"],
            )  # fmt: skip
    except Exception as exc:
        epic_log.warning("work_items mirror (read) failed for %s: %s: %s", ctx.initiative_id, type(exc).__name__, exc)


def store_authoritative(ctx: Ctx) -> bool:
    return ctx.store is not None and getattr(ctx, "work_state", "files") == "store"


def read_store_rows(ctx: Ctx) -> list[WorkItemReadRow] | None:
    """The initiative's work_items rows as the store holds them now; None under files or when the read fails.

    The one store read of the done-wins check. A failure is logged and reads as no rows, as `mirror_read` does.
    """
    if not store_authoritative(ctx):
        return None
    try:
        from harness import store_read  # not module level, for the reason given in `mirror_read`

        return list(store_read.work_items(ctx_store(ctx).conn, ctx.initiative_id))
    except Exception as exc:
        epic_log.warning("work_items not re-read for %s: %s: %s", ctx.initiative_id, type(exc).__name__, exc)
        return None


def store_fresh(items: Sequence[EpicItem], rows: Sequence[WorkItemReadRow] | None) -> list[EpicItem]:
    """Copies of `items` where a task whose row says done takes the row's state, merged flag and merged commit."""
    done = {r["task_id"]: r for r in rows or () if r["state"] == "done"}
    return [
        {**item, "state": "done", "merged": done[k]["merged"], "merged_commit": done[k]["merged_commit"]}
        if (k := item["id"]) in done
        else item.copy()
        for item in items
    ]


def store_first(ctx: Ctx, item: ItemRef, state: str) -> str | None:
    """work_state: store. Move `item` to `state` in the store, compare-and-set on the state the mover read.

    None when the store accepted, so the driver writes the file line itself. Otherwise the refusal reason: nothing else runs.
    A mismatch adopts the store's state into `item`, as `_mirror_read_store` does. A store error refuses the move.
    """
    from harness import store_work_state  # not module level: see `mirror_read`

    task, expected = item.get("id"), str(item["state"])
    try:
        written = store_work_state.set_state(
            ctx_store(ctx).conn, ctx.initiative_id, str(task), state, mirror_by(ctx), item.get("phase"), utc_now(), expected=expected
        )
    except Exception as exc:
        reason = f"state move refused, store write failed: {type(exc).__name__}: {exc}"
        epic_log.warning("%s: task=%s", reason, task)
        return reason
    if not isinstance(written, store_work_state.Mismatch):
        return None
    _, why = work_mirror.check_expected_state(expected, written.current)
    reason = f"state move refused: {why}"
    epic_log.warning("%s: task=%s", reason, task)
    if written.current is not None:
        item["state"] = written.current
    return reason


def row_should_quarantine(work_state: str, current_state: str | None, *, approved_is_open: bool = False) -> bool:
    """True when a quarantine moves the store row: store work state, a row that exists, and not finished or already quarantined.

    `approved_is_open` is for a merge into the phase branch that failed: the row reads approved but its work never landed.
    """
    finished = ("done", "quarantined") if approved_is_open else ("approved", "done", "quarantined")
    return work_state == "store" and current_state is not None and current_state not in finished


def git_stdout(*args: str, timeout: int = 120, ok_codes: tuple[int, ...] = (0,)) -> str | None:
    """Edge. Stdout of one git command, byte for byte; None on an exit outside `ok_codes`, a timeout or undecodable output.

    `git_in` returns stderr in place of stdout when stderr is non-empty, so it cannot carry a NUL-separated listing.
    `git grep` exits 1 for no match, so its callers pass ok_codes=(0, 1).
    """
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        return None
    return proc.stdout if proc.returncode in ok_codes else None


def default_branch_paths(ctx: Ctx) -> frozenset[str] | None:
    """Edge. Repo-relative file paths on `ctx.default_ref`, the ref `resolve_default_ref` produced after its fetch.

    Cached on `ctx` by the commit the ref names, so a phase reuses the listing until the ref moves.
    None when the ref or its listing cannot be read; that is not cached.
    """
    repo = ("-C", str(ctx.repo))
    named = git_stdout(*repo, "rev-parse", "--verify", f"{ctx.default_ref}^{{commit}}")
    if named is None:
        return None
    sha = named.strip()
    with ctx.items_lock:
        if sha not in ctx.surface_paths:
            listing = git_stdout(*repo, "ls-tree", "-r", "-z", "--name-only", sha)
            if listing is None:
                return None
            ctx.surface_paths[sha] = frozenset(p for p in listing.split("\0") if p)
        return ctx.surface_paths[sha]


def default_branch_commit(ctx: Ctx) -> str:
    """Edge. The commit `ctx.default_ref` names; empty when it cannot be read."""
    named = git_stdout("-C", str(ctx.repo), "rev-parse", "--verify", f"{ctx.default_ref}^{{commit}}")
    return (named or "").strip()


def without_correction_lint(ticket: Mapping[str, object], original: Mapping[str, object]) -> dict[str, object]:
    """`ticket` without its `correction:` lint entries.

    A correction quotes the tree at one commit, so it goes stale once the code or the body changes. It is
    recomputed at each launch and handed to the builder, never stored.
    """
    kept = [n for n in as_list(ticket.get("lint")) if not str(n).startswith("correction:")]
    rest = {k: v for k, v in ticket.items() if k != "lint"}
    return {**rest, "lint": kept} if kept or "lint" in original else rest


def working_tree_paths(root: Path) -> frozenset[str]:
    """Edge. Repo-relative file paths in the working tree of `root`; the fallback when the branch listing is unreadable."""
    return frozenset(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts and p.is_file()
    )


def surface_refusal_reason(malformed: Sequence[str]) -> str:
    """The reason a task is refused for surfaces that are absolute, reach outside the repository, or are a glob."""
    return (
        f"malformed surfaces: {', '.join(malformed)}; a surface is a repository relative path "
        "with no glob and no .. segment."
    )


def frontmatter_key_lines(key: str, value: Sequence[str]) -> list[str]:
    return yaml.safe_dump({key: list(value)}, default_flow_style=None, allow_unicode=True).rstrip().split("\n")


def heal_surfaces(task: Mapping[str, object], paths: frozenset[str]) -> dict[str, object]:
    """Bare filenames resolved or left out with a note, notes moved to lint, then missing surfaces marked `(new)`.

    An unmarked bare name that no repository file shares is a new file at the repository root, so it is not
    handed to heal_bare_filenames, which would drop it; heal_new_markers marks it `(new)` as before.
    """
    names = {p.rsplit("/", 1)[-1] for p in paths}
    surfaces = [str(s) for s in as_list(task.get("surfaces"))]
    root_new = [
        s for s in surfaces
        if "/" not in s and not s.strip().endswith("(new)") and s.strip() not in names
        and s.strip() and not any(c.isspace() or c in "*?[]" for c in s.strip()) and s.strip() != ".."
    ]
    resolved = heal_bare_filenames({**task, "surfaces": [s for s in surfaces if s not in root_new]}, paths)
    kept = {**resolved, "surfaces": [*as_list(resolved.get("surfaces")), *root_new]}
    return heal_new_markers(notes_out_of_surfaces(kept), paths)


def _rewrite_frontmatter_surfaces(path: Path, surfaces: Sequence[str], notes: Sequence[str]) -> list[str]:
    """Replace the `surfaces` key and extend the `lint` key of the file's frontmatter; every other line is kept as it was.

    Returns the lint list now in the file, so a caller can give the store row the same notes.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if lines[0].strip() != "---" or end is None:
        raise ValueError(f"{path}: no frontmatter block")
    lint: list[str] = list(dict.fromkeys([*(str(n) for n in as_list(as_mapping(yaml.safe_load("\n".join(lines[1:end]))).get("lint"))), *notes]))
    front = lines[1:end]
    for key, value in (("surfaces", surfaces), ("lint", lint)):
        at = next((i for i, line in enumerate(front) if line.startswith(f"{key}:")), None)
        if at is None:
            front = [*front, *frontmatter_key_lines(key, value)]
            continue
        stop = next((i for i in range(at + 1, len(front)) if not front[i].startswith((" ", "-"))), len(front))
        front = [*front[:at], *frontmatter_key_lines(key, value), *front[stop:]]
    write_atomic(path, "\n".join(["---", *front, *lines[end:]]))
    return lint


def write_healed_surfaces(ctx: Ctx, task: str, surfaces: Sequence[str], notes: Sequence[str], path: str | None) -> None:
    """Persist a healed task's surfaces and new lint notes, so a relaunch reads them and does not heal again.

    A stale leader writes neither the file nor the row. Otherwise the file is rewritten first with the row's lint
    notes and the new ones added to its own, and the row gets that same list and the same surfaces, in either work
    state. So a note held only by the row, or only by the file, survives in both. A file that cannot be rewritten
    leaves the row as it was. Under work_state store a task with no file path is the one case where the row is
    written alone, since the row is the source of truth and there is no file to disagree with. Errors are logged,
    never raised.

    Functions checked for other writes of a task file's title, body or surfaces: `_rewrite_frontmatter_surfaces`
    (called only here), `repair_file_state` (the `state:` line only), and `apply_amend_directly`, whose only file
    write puts back the bytes read before a refused amend. Triage's own effects write a body (`_rewrite_file_body`, for
    an amendment the store took or a combined sibling) and `needs` (`_rewrite_file_needs`), never surfaces or a title.
    The fix-loop re-scope and correction rounds amend a ticket
    through the `work_state_arm` `ticket_amend` call and do not write the file themselves, so none needed routing.
    """
    try:
        stale = fence_reason(ctx)
    except Exception as exc:
        epic_log.warning("healed surfaces not written: task=%s: %s: %s", task, type(exc).__name__, exc)
        return
    if stale is not None:
        epic_log.warning("healed surfaces not written: task=%s: %s", task, stale)
        return
    from harness import store_queue  # not module level: see `mirror_read`

    row: QueueRow | None = None
    if ctx.store is not None:
        try:
            row = next((r for r in store_queue.read(ctx.store.conn, ctx.initiative_id) if r["task_id"] == task), None)
        except Exception as exc:
            epic_log.warning("healed surfaces: store row not read: task=%s: %s: %s", task, type(exc).__name__, exc)
        if row is None:
            epic_log.warning("healed surfaces not written to the store row: task=%s: no row", task)
    extra = dict(as_mapping(row["extra"] if row is not None else None))
    row_lint = [str(n) for n in as_list(extra.get("lint"))]
    lint = list(dict.fromkeys([*row_lint, *notes]))
    if path:
        try:
            lint = _rewrite_frontmatter_surfaces(Path(path), surfaces, lint)
        except Exception as exc:
            epic_log.warning("healed surfaces not written to the task file: task=%s: %s: %s", task, type(exc).__name__, exc)
            epic_log.warning("healed surfaces not written to the store row, to keep it in step with the task file: task=%s", task)
            return
    elif not store_authoritative(ctx):
        epic_log.warning("healed surfaces not written to the task file: task=%s: no path", task)
        epic_log.warning("healed surfaces not written to the store row, to keep it in step with the task file: task=%s", task)
        return
    if row is None:
        return
    try:
        store_queue.upsert(
            ctx_store(ctx).conn,
            {"initiative": ctx.initiative_id, "task_id": task, "surfaces": list(surfaces), "extra": {**extra, "lint": list(lint)}},
        )
    except Exception as exc:
        epic_log.warning("healed surfaces not written to the store row: task=%s: %s: %s", task, type(exc).__name__, exc)


def quarantine_row(ctx: Ctx, phase: str, task: str, *, approved_is_open: bool = False) -> None:
    """Under work_state store, move the task's row to quarantined, so the chair stops relaunching it. Errors are logged, never raised.

    The files work state writes nothing here, and neither does a stale leader. Same store write as the move to approved: `store_first`.
    """
    if not store_authoritative(ctx):
        return
    try:
        stale = fence_reason(ctx)  # callers in `run_phase_tasks` reach here long after the phase-start fence
        if stale is not None:
            epic_log.warning("quarantined state not written to the store row: task=%s: %s", task, stale)
            return
        from harness import store_read  # not module level: see `mirror_read`

        row = next((r for r in store_read.work_items(ctx_store(ctx).conn, ctx.initiative_id) if r["task_id"] == task), None)
        current = None if row is None else str(row["state"])
        if current is not None and row_should_quarantine(ctx.work_state, current, approved_is_open=approved_is_open):
            store_first(ctx, {"id": task, "phase": phase, "state": current}, "quarantined")
    except Exception as exc:
        epic_log.warning("quarantined state not written to the store row: task=%s: %s: %s", task, type(exc).__name__, exc)


def mirror_write(ctx: Ctx, item: Mapping[str, object] | None, state: str) -> None:
    """Upsert one task's work_items row after an arm wrote `state` to its file. Errors are logged, never raised.

    Under work_state store the row was already written by `store_first`, before the file, so nothing is upserted.
    """
    if ctx.store is None or item is None or store_authoritative(ctx):
        return
    try:
        _upsert_row(ctx.store, work_mirror.item_row(ctx.initiative_id, {**item, "state": state}, utc_now(), mirror_by(ctx)))
    except Exception as exc:
        epic_log.warning("work_items mirror (write) failed for %s: %s: %s", item.get("id"), type(exc).__name__, exc)


def fence_reason(ctx: Ctx) -> str | None:
    """None when a leader-only write may proceed, else the `stale epoch` reason. No epoch, no fence."""
    if ctx.epoch is None or ctx.store is None:
        return None
    conn, name, epoch = ctx.store.conn, ctx.lease_name or LEASE_NAME, ctx.epoch

    def check() -> str | None:
        if assert_epoch(conn, name, epoch, utc_now()):
            return None
        row = conn.query_one(f"SELECT epoch FROM leases WHERE name = {conn.dialect.placeholder}", (name,))
        held = "none" if row is None else row[0]
        return f"stale epoch: this driver holds epoch {epoch}, lease '{name}' is at epoch {held} or has expired"

    return ctx.retry.write(check, END_WRITE_WINDOW_S)


def is_stale_epoch(reason: str) -> bool:
    """True for the reason `fence_reason` returns when the driver's lease is lost."""
    return reason.startswith("stale epoch")


# Only a store error is retried in the pause wait; a bug there still raises with its traceback.
def _store_errors() -> tuple[type[Exception], ...]:
    try:  # the postgres driver is an optional extra
        import psycopg
    except ImportError:  # pragma: no cover - depends on what is installed
        return (sqlite3.Error,)
    return (sqlite3.Error, psycopg.Error)


_STORE_ERRORS = _store_errors()

_PAUSE_LEASE_TTL = 120  # seconds; matches the CLI's own `_LEASE_TTL` (harness/cli_launch.py) heartbeat renew.


def _wait_while_paused(
    conn: Connection,
    run_id: str,
    lease_name: str,
    epoch: int,
    now: Callable[[], str],
    ttl: int,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 10,
    attempts: int = 3,
    retry_wait: float = 2.0,
) -> None:
    """Block while paused; a lost lease or a store down for `attempts` tries ends the run as SIGTERM does (143)."""

    def retried[T](call: Callable[[], T]) -> T:
        error: Exception | None = None
        for attempt in range(max(1, attempts)):
            if attempt:
                sleep(retry_wait)
            try:
                return call()
            except _STORE_ERRORS as exc:
                error = exc
        epic_log.warning("pause wait: store error after %d attempts: %s: %s", attempts, type(error).__name__, error)
        raise SystemExit(143) from error

    while retried(lambda: is_paused(conn, run_id)):
        # heartbeat says why: paused, not stuck
        if not retried(lambda: renew(conn, lease_name, run_id, epoch, now(), ttl, status="paused")):
            epic_log.warning("pause wait: lease '%s' epoch %s lost; ending the run", lease_name, epoch)
            raise SystemExit(143)
        sleep(poll_seconds)
    # always: clears a prior "paused" on resume too. A False return is left to the caller's fence re-check.
    retried(lambda: renew(conn, lease_name, run_id, epoch, now(), ttl, status=None))


def clear_pause_if_held(
    store: Store, run_id: str, lease_name: str, epoch: int | None, retry: StoreRetry | None = None
) -> None:
    """Clear the run's pause flag so a rerun does not stop at once; skipped when another holder owns the run."""

    def clear() -> None:
        if epoch is None or assert_epoch(store.conn, lease_name, epoch, utc_now()):
            clear_paused(store.conn, run_id)
        else:
            epic_log.warning("pause flag kept for %s: lease '%s' epoch %s is no longer held", run_id, lease_name, epoch)

    try:
        (retry or StoreRetry()).write(clear, END_WRITE_WINDOW_S)
    except _STORE_ERRORS as exc:  # a store failure must not mask the run's real exit
        epic_log.warning("pause flag clear failed for %s: %s: %s", run_id, type(exc).__name__, exc)


def _awaits_land(phase: Mapping[str, object]) -> bool:
    """A phase waiting on a land."""
    return phase.get("status") == "awaiting_land"


def build_outcome(result: Mapping[str, object]) -> dict[str, object]:
    """The run's outcome from its epic result: totals, phase-grain quarantines, phases waiting on a parent.

    A waiting phase is a blocked record with `parents` entries: a parent did not complete, so it built nothing.
    """
    quarantined = [as_mapping(q) for q in as_list(result.get("quarantined"))]
    phases = [as_mapping(p) for p in as_list(result.get("phases"))]
    blocked = [{"phase": q["phase"], "reason": q["reason"]} for q in quarantined if q.get("grain") == "phase"]
    blocked_ids = {b["phase"] for b in blocked}
    waiting = [
        p["phase"]
        for p in phases
        if p.get("status") == "blocked" and len(as_list(p.get("parents"))) >= 1 and p["phase"] not in blocked_ids
    ]
    awaiting_land = any(_awaits_land(p) for p in phases)
    interrupted = [{"phase": p["phase"], "reason": p.get("reason")} for p in phases if p.get("status") == "interrupted"]
    return {
        "totals": dict(as_mapping(result.get("totals"))),
        "blocked": blocked,
        "waiting": waiting,
        **({"interrupted": interrupted} if interrupted else {}),
        **({"blocked_kind": "awaiting_land"} if awaiting_land else {}),
    }


def unlanded_tasks(
    tasks: Sequence[Mapping[str, object]], run_id: str, repo: Path | str
) -> list[tuple[Mapping[str, object], str]]:
    """Tasks approved but not landed, each with the `cox runs land` command a human runs for it.

    `outcome == "landed"` only ever meant "merged into the phase stack, not quarantined", never a claim that
    `cox runs land` ran, so such a task whose item still reads `approved` is unlanded too. `land` subsumes
    `recover` and never needs a phase branch resolved, so it is the command named here.
    """
    approved = [t for t in tasks if t.get("outcome") == "approved_not_landed"]
    merged = [t for t in tasks if t.get("outcome") == "landed" and t.get("state") == "approved"]
    return [(t, f"cox runs land {run_id} --repo {repo} --task {t['id']} --apply") for t in approved + merged]


def _integration_rows(record: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    """The check rows under `key`, shaped like `checks` rows; none when the key is absent, empty or not a list."""
    rows = record.get(key)
    return [r for r in rows if is_mapping(r)] if is_list(rows) else []


def _count(row: Mapping[str, object], key: str) -> int:
    """`row`'s `counts` entry under `key` as a number; 0 when it is absent or not one."""
    n = as_mapping(row.get("counts")).get(key, 0)
    return n if isinstance(n, int) else 0


def _integration_counts(tasks: Sequence[Mapping[str, object]], phases: Sequence[Mapping[str, object]]) -> dict[str, int]:
    """Integration totals kept apart from the unit `tests_run`; a task or level with no integration rows counts in none."""
    task_rows = [_integration_rows(t, "integration") for t in tasks]
    level_rows = [_integration_rows(p, "level_integration") for p in phases]
    return {
        "integration_tasks_passed": sum(1 for rows in task_rows if rows and all(r.get("passed") for r in rows)),
        "integration_tasks_failed": sum(1 for rows in task_rows if rows and not all(r.get("passed") for r in rows)),
        "integration_levels_passed": sum(1 for rows in level_rows if rows and all(r.get("passed") for r in rows)),
        "integration_levels_failed": sum(1 for rows in level_rows if rows and not all(r.get("passed") for r in rows)),
        **{
            f"integration_rows_{k}": sum(_count(r, k) for rows in task_rows for r in rows)
            for k in ("passed", "failed", "error")
        },
    }


def build_result(
    *,
    run_id: str,
    date: str,
    initiative_id: str,
    phases: Sequence[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
    quarantined: Sequence[Mapping[str, object]],
    proposals: Sequence[Mapping[str, object]],
    stacks_rebased: int,
    paused_until: str | None,
    repo: Path | str,
    land_at: str | None = None,
    waves: Sequence[Sequence[str]] = (),
    lane: Mapping[str, object] | None = None,
    lane_databases: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """The epic result from the lists as they stand: whole at a normal end, partial when a phase raised."""
    unlanded = unlanded_tasks(tasks, run_id, repo)
    return {
        "run_id": run_id,
        "date": date,
        "initiative": initiative_id,
        **({"land_at": land_at} if land_at else {}),
        "phases": phases,
        "waves": [list(w) for w in waves],
        "widest_wave": widest_wave(waves),
        "lane_bound": dict(lane or {}),
        **({"lane_databases": lane_databases} if lane_databases is not None else {}),
        "tasks": tasks,
        "quarantined": quarantined,
        "proposals": proposals,
        **({"paused_until": paused_until, "exit_code": EXIT_PAUSED} if paused_until else {}),
        **(
            {"exit_code": EXIT_AWAITING_LAND}
            if not paused_until and any(_awaits_land(p) for p in phases)
            else {}
        ),
        "exit_summary": (
            [f"paused: account session limit, resets {paused_until}"] if paused_until else []
        ) + [
            f"approved but not landed: {t['id']} — {command}" for t, command in unlanded
        ],
        "totals": {
            "phases_complete": sum(1 for p in phases if p["status"] == "complete"),
            "phases_partial": sum(1 for p in phases if p["status"] == "partial"),
            "phases_awaiting_land": sum(1 for p in phases if _awaits_land(p)),
            "phases_blocked": sum(1 for p in phases if p["status"] == "blocked"),
            "tasks_quarantined": sum(1 for q in quarantined if q.get("grain") == "task"),
            **_integration_counts(tasks, phases),
            "approved_not_landed": len(unlanded),
            "stacks_rebased": stacks_rebased,
        },
    }


def record_run_exit(
    store: Store, run_id: str, status: str, retry: StoreRetry, result: Mapping[str, object] | None = None
) -> None:
    """Stamp the run's end, then store its outcome, each retried for the full exit window. A failure is logged and never changes the run's exit."""
    ended_at = utc_now()
    try:
        retry.write(lambda: store.finish_run(run_id, ended_at, status), _EXIT_WINDOW_S)
    except Exception as exc:
        epic_log.warning("run exit for %s not recorded in the store: %s: %s", run_id, type(exc).__name__, exc)
    if result is None:
        return
    try:
        retry.write(lambda: store.record_outcome(run_id, build_outcome(result)), _EXIT_WINDOW_S)
    except Exception as exc:
        epic_log.warning("run outcome for %s not recorded in the store: %s: %s", run_id, type(exc).__name__, exc)


def publish_run_exit(run_id: str, initiative_id: str, status: str) -> None:
    """Publish `run_exited` once, after the exit is recorded. A failure is logged and never changes the run's exit."""
    try:
        published = publish_run_exited(run_id, initiative_id, status)
    except Exception as exc:
        epic_log.warning("run_exited for %s not published: %s: %s", run_id, type(exc).__name__, exc)
        return
    if not published:
        epic_log.warning("run_exited for %s not published: the courier was unreachable", run_id)


def wait_if_paused_here(ctx: Ctx) -> None:
    """`_wait_while_paused` bound to this run's own lease; a no-op without a fenced store."""
    if ctx.epoch is None or ctx.store is None:
        return
    _wait_while_paused(
        ctx.store.conn, ctx.run_id, ctx.lease_name or LEASE_NAME, ctx.epoch, utc_now, _PAUSE_LEASE_TTL, sleep=time.sleep,
    )


def _task_record_mirror(
    store: Store | None, result: Mapping[str, object], run_id: str, phase: str, task: str, ts: str
) -> tuple[str, str, str, dict[str, object], str] | None:
    """Arguments for `Store.record_task_record`, or None with no store to mirror into.

    The record is the JSON round trip the file gets, so the row equals the saved file.
    """
    if store is None:
        return None
    return (run_id, phase, task, json.loads(json.dumps(dict(result), default=str)), ts)


def save_task_result(ctx: Ctx, result: Mapping[str, object], *, phase: str, task: str) -> None:
    """Save the result file, then mirror it into the store. The file stays authoritative.

    A mirror failure is a warning, never a failed run. The file write is outside the try.
    """
    save_result(result, runs_dir=ctx.runs_dir, run_id=ctx.run_id, phase=phase, task=task)
    try:
        args = _task_record_mirror(ctx.store, result, ctx.run_id, phase, task, utc_now())
        store = ctx.store

        def mirror() -> None:
            if fence_reason(ctx) is None:
                cast(Store, store).record_task_record(*cast(tuple[str, str, str, dict[str, object], str], args))

        if args is not None and store is not None:
            ctx.retry.write(mirror, END_WRITE_WINDOW_S)
    except Exception as exc:
        epic_log.warning("task record for %s/%s/%s not mirrored into the store: %r", ctx.run_id, phase, task, exc)


def record_phase(ctx: Ctx, record: Mapping[str, object]) -> None:
    """Store the phase's record, checked as the store will hold it: a record that is not JSON is refused here."""
    if ctx.store is not None and fence_reason(ctx) is None:
        checked = phase_record(record)
        row = {**checked, "run_id": f"{ctx.run_id}:{record['phase']}", "ts": utc_now(), "principal": PRINCIPAL}
        store = ctx.store
        ctx.retry.write(lambda: store.record_phase(row, epoch=ctx.epoch), END_WRITE_WINDOW_S)


def record_tasks(
    ctx: Ctx, phase: str, task_records: Sequence[Mapping[str, object]], quarantined: Sequence[Mapping[str, object]]
) -> None:
    """One row per task at its final state: the store keeps the first row for a (run, task)."""
    if ctx.store is None or fence_reason(ctx) is not None:
        return
    states = {str(q["id"]): "quarantined" for q in quarantined if q.get("grain") == "task"}
    states.update({str(t["id"]): str(t.get("state") or t["status"]) for t in task_records})
    ts = utc_now()
    store = ctx.store
    for task, state in sorted(states.items()):
        # One retry per row, so a lost connection never replays rows that already landed.
        ctx.retry.write(
            lambda task=task, state=state: store.record_task(ctx.run_id, phase, task, state, ts, epoch=ctx.epoch),
            END_WRITE_WINDOW_S,
        )


def refuse_stale(state: Execution, *, phase: str, subject: str, slot: str, reason: str) -> None:
    """Quarantine what a stale leader tried to write. Not an attempt, so never through `quarantine_task`."""
    task_grain = bool(subject) and slot != "rebase"
    entry: Quarantine = {
        "id": subject if task_grain else phase,
        "phase": phase,
        "grain": "task" if task_grain else "phase",
        "reason": reason,
        "kind": "no_work",
    }
    if entry not in state.quarantined:
        state.quarantined.append(entry)


@dataclass
class Execution:
    """What actually happened, per task, as the batch executes."""

    landed: dict[str, bool]
    merged: dict[str, bool]
    moved: dict[str, bool]
    quarantined: list[Quarantine]
    review_pr: dict[str, str] = field(default_factory=dict[str, str])
    escalated: frozenset[str] = frozenset()
    notes: dict[str, str] = field(default_factory=dict[str, str])
    draft_detail: dict[str, str] = field(default_factory=dict[str, str])
    push_warnings: list[str] = field(default_factory=list[str])  # origin pushes that failed; the branch stays local
    commits: dict[str, str] = field(default_factory=dict[str, str])  # task -> the commit its merge made on the phase branch
    builds: dict[str, Mapping[str, object]] = field(default_factory=dict[str, Mapping[str, object]])  # task -> its build result, for the handoff
    handoffs: dict[str, JsonObject] = field(default_factory=dict[str, JsonObject])  # task -> the handoff stored with its merge
