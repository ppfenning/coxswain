"""The `lands_waiting` reader: recent lands that left a PR open and unmerged, with checks not all complete.

Source: `land.jsonl`, one `land.land_log_row` per land, appended when a land ends. Its `ts` is the land's end
time, not a wait start. A land that exits with `pr` set and no `merge` step has left its PR waiting on checks
from then on, so `waiting_since` is the earliest `ts` of any row for that PR. A land still walking its steps
has no row yet and is not listed.

A failed check read is not a land with checks not started. `LandWait` has no field for it, so the row carries
`checks unreadable: <error text>` in `forge_status` and `checks_started` is False. Only a successful read
that lists no checks means "not started" with `forge_status` untouched.

`land.jsonl` only grows, so the edge reads rows newer than `RECENT`, at most `MAX_ROWS`, and starts no check
read after `budget_s`. `forge_status.StatusReader` has no fetch in the repository, so `status_call` defaults
to None; `status_text` adapts a `ForgeStatus` for a caller that has one.
"""

import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_tools import land
from agent_tools.chair_types import LandWait
from agent_tools.forge_status import ForgeStatus
from agent_tools.jsonl_log import parse_log

Bodies = tuple[dict[str, Any], dict[str, Any]]
CheckRead = Bodies | str  # a str is the failed read's error text
RECENT = timedelta(days=2)
MAX_ROWS = 10
CHECKS_TTL_S = 180.0
UNREADABLE = "checks unreadable"
_PR_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/\d+$")
_RUNNING = frozenset({"in_progress", "completed"})


def checks_started(check_runs: Mapping[str, Any], status: Mapping[str, Any]) -> bool | None:
    """None when `land.check_poll_result` reads green or failed; else whether any check has begun."""
    rc, output = land.check_poll_result(dict(check_runs), dict(status))
    if land.wait_decision(rc, output, 0.0, 1.0) != "retry":
        return None
    runs = [r.get("status") for r in check_runs.get("check_runs") or []]
    states = [s.get("state") for s in status.get("statuses") or []]
    return any(s in _RUNNING for s in runs) or any(s not in (None, "pending") for s in states)


def status_text(status: ForgeStatus) -> str | None:
    """The incident text when the forge is degraded, else None."""
    return (status.incident or "degraded") if status.degraded else None


def _wait(row: Mapping[str, Any], read: CheckRead, forge_status: str | None) -> LandWait | None:
    started: bool | None = False
    status = forge_status
    if isinstance(read, str):
        status = "; ".join(filter(None, [f"{UNREADABLE}: {read}", forge_status]))
    else:
        started = checks_started(*read)
        if started is None:
            return None
    return {
        "run": str(row.get("run") or ""),
        "pr": str(row["pr"]),
        "waiting_since": str(row.get("waiting_since") or row.get("ts") or ""),
        "checks_started": started,
        "forge_status": status,
    }


def land_waits(
    rows: Sequence[Mapping[str, Any]], reads: Mapping[str, CheckRead], forge_status: str | None
) -> list[LandWait]:
    """One `LandWait` per row with checks not all complete; a failed read keeps the row and says so."""
    waits = (_wait(r, reads.get(r["pr"], "no check read"), forge_status) for r in rows)
    return [w for w in waits if w is not None]


def _when(row: Mapping[str, Any]) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(row.get("ts") or ""))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def unmerged_lands(
    rows: Sequence[Mapping[str, Any]], now: datetime, recent: timedelta = RECENT, cap: int = MAX_ROWS
) -> list[dict]:
    """Newest-first, at most `cap`: per run and task, the newest row when it has a `pr`, no `merge`, and is recent."""
    parsed = [(w, r) for r in rows if (w := _when(r)) is not None]
    newest = {(r.get("run"), r.get("task")): (w, r) for w, r in parsed}
    open_rows = sorted(
        (
            (w, r)
            for w, r in newest.values()
            if r.get("pr") and "merge" not in (r.get("steps_reached") or []) and now - w <= recent
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )[:cap]
    return [
        {**r, "waiting_since": min((p for p in parsed if p[1].get("pr") == r["pr"]), key=lambda p: p[0])[1]["ts"]}
        for _, r in open_rows
    ]


def _failure(what: str, reply: Any) -> str:
    detail = (reply.stderr or "").strip() if isinstance(getattr(reply, "stderr", None), str) else ""
    return f"{what} failed: {detail or f'exit {reply.returncode}'}"


def gh_checks(pr: str, timeout_s: float, run: Callable[..., Any] = subprocess.run) -> CheckRead:
    """The PR head's bodies from `land.rest_checks_argvs`, or the error text; `timeout_s` bounds the three `gh` calls."""
    match = _PR_URL.match(pr)
    if match is None:
        return f"not a github pull request url: {pr}"
    each = timeout_s / 3
    opts: dict[str, Any] = {"capture_output": True, "text": True, "timeout": each, "check": False}
    repo = f"{match[1]}/{match[2]}"

    def api(argv: list[str], key: str) -> dict[str, Any] | str:
        reply = run([a.replace("{owner}/{repo}", repo) for a in argv], **opts)
        body = land.merge_pages(reply.stdout or "", key) if reply.returncode == 0 else None
        if body is None:
            return _failure(f"gh api {key}", reply) if reply.returncode != 0 else f"gh api {key} gave no body"
        return body

    try:
        view = run(["gh", "pr", "view", pr, "--json", "headRefOid", "--jq", ".headRefOid"], **opts)
        if view.returncode != 0 or not view.stdout.strip():
            return _failure("gh pr view", view)
        argvs = land.rest_checks_argvs(view.stdout.strip())
        # The two api calls need only the head sha, so they run together; the first failure in key order is reported.
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(api, argvs, ("check_runs", "statuses")))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    failed = next((r for r in replies if isinstance(r, str)), None)
    return failed if failed is not None else (replies[0], replies[1])  # type: ignore[return-value]


def cached_checks(
    read_checks: Callable[[str, float], CheckRead],
    store: dict[str, tuple[float, Bodies]],
    clock: Callable[[], float] = time.monotonic,
    ttl_s: float = CHECKS_TTL_S,
) -> Callable[[str, float], CheckRead]:
    """`read_checks` that remembers each PR's successful bodies for `ttl_s`. Errors are never kept.

    `store` is the caller's edge state, so the cache outlives one round; a pure read never owns it."""

    def read(pr: str, timeout_s: float) -> CheckRead:
        held = store.get(pr)
        if held is not None and clock() - held[0] < ttl_s:
            return held[1]
        got = read_checks(pr, timeout_s)
        if not isinstance(got, str):
            store[pr] = (clock(), got)
        return got

    return read


def _safely(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:  # edge: a failed forge call never stops the tick
        return exc


def read_lands_waiting(
    runs_dir: Path,
    now: datetime,
    read_checks: Callable[[str, float], CheckRead] = gh_checks,
    status_call: Callable[[float], str | None] | None = None,
    timeout_s: float = 5.0,
    budget_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
) -> list[LandWait]:
    """Edge. `[]` on an unreadable log; a raised or malformed check read is `checks unreadable`, a bad status is None."""
    try:
        text = (Path(runs_dir) / "land.jsonl").read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows = unmerged_lands(parse_log(text).rows, now)
    deadline = clock() + budget_s
    # Every read is submitted before any is awaited, so the PRs are read together; the budget gates each start.
    with ThreadPoolExecutor(max_workers=max(1, len(rows))) as pool:
        started: dict[str, Future | str] = {
            r["pr"]: pool.submit(_safely, lambda pr=r["pr"]: read_checks(pr, timeout_s)) if clock() < deadline
            else "budget exhausted"
            for r in rows
        }
        results = {pr: got.result() if isinstance(got, Future) else got for pr, got in started.items()}
    reads: dict[str, CheckRead] = {
        pr: f"{type(got).__name__}: {got}" if isinstance(got, Exception) else got for pr, got in results.items()
    }
    status = _safely(lambda: status_call(timeout_s)) if status_call is not None and rows else None
    return land_waits(rows, reads, status if isinstance(status, str) else None)
