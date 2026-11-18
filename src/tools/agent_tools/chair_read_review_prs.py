"""The `review_prs` source of a chair tick: approved tasks whose record names a review PR.

`awaiting_reviews` is pure. `read_review_prs` is the edge: it reads the task records under the runs directory and
asks the injected `pr_state` for each awaiting url. It never calls the forge or gh itself.
"""
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NotRequired

from agent_tools import run_store
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_types import ReviewPr
from core.jsontypes import is_dict

Record = Mapping[str, object]
PrState = Mapping[str, object]
_STATES: dict[str, Literal["open", "merged", "closed", "unknown"]] = {
    "open": "open", "merged": "merged", "closed": "closed", "unknown": "unknown",
}


class _OwnedReviewPr(ReviewPr):
    """A `ReviewPr` that also names the run that owns the review url, and that run's host when the store has a row for it."""

    run: NotRequired[str]
    host: NotRequired[str]


_NO_HOSTS: Mapping[str, str] = MappingProxyType({})


def _url(record: Record) -> str:
    url = record.get("review_pr")
    return url if isinstance(url, str) else ""


def _awaiting(record: Record) -> bool:
    return record.get("status") == "approved" and bool(_url(record))


def _review_pr(record: Record, pr: PrState, hosts: Mapping[str, str]) -> ReviewPr:
    """The row for one awaiting record, `pr` being what the forge said of its url."""
    raw_state, merged_at = pr.get("state", "unknown"), pr.get("merged_at")
    row: _OwnedReviewPr = {
        "initiative": str(record.get("initiative") or run_initiative(str(record.get("run", "")))),
        "phase": str(record.get("phase", "")),
        "task_id": str(record.get("task_id") or record.get("task", "")),
        "repo": str(record.get("repo") or ""),
        "url": _url(record),
        "state": _STATES[raw_state] if isinstance(raw_state, str) and raw_state in _STATES else "unknown",
        "merged_at": merged_at if isinstance(merged_at, str) else None,
    }
    run = str(record.get("run") or "")
    if run:
        row["run"] = run
    if run in hosts:
        row["host"] = hosts[run]
    return row


def awaiting_reviews(records: Sequence[Record], states: Mapping[str, PrState], hosts: Mapping[str, str] = _NO_HOSTS) -> list[ReviewPr]:
    """One row per approved record with a non-empty `review_pr`. A url missing from `states` has state "unknown".

    A row names the owning `run`, and its `host` only when `hosts` has the run; a run with no row has no `host` key.
    """
    return [_review_pr(record, states.get(_url(record), {}), hosts) for record in records if _awaiting(record)]


def store_run_hosts(runs_dir: Path, runs: Sequence[str]) -> dict[str, str]:
    """Edge. Each run's `runs.host` ("" when blank or the column is absent); a run with no row is omitted.

    Empty with no store or an unreadable one, so every row then lacks a host and the executor sends it to the chair.
    """
    opened = run_store.open_store(runs_dir)
    if opened is None:
        return {}
    conn, token = opened
    try:
        host_expr = "host" if "host" in run_store.runs_columns(conn, token) else "NULL AS host"
        sql = run_store.sql_for(f"SELECT {host_expr} FROM runs WHERE run_id = {{p}}", token)
        rows = {run: conn.execute(sql, (run,)).fetchone() for run in runs}
    except run_store._DB_ERRORS:
        return {}
    finally:
        conn.close()
    return {run: (row["host"] or "") for run, row in rows.items() if row is not None}


def _load_records(runs_dir: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(runs_dir.glob("*/tasks/*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if is_dict(record):
            records.append({"run": path.parent.parent.parent.name, "task": path.stem, "phase": path.parent.name, **record})
    return records


def read_review_prs(
    runs_dir: Path,
    pr_state: Callable[[str], PrState],
    run_hosts: Callable[[Path, Sequence[str]], Mapping[str, str]] = store_run_hosts,
) -> list[ReviewPr]:
    """Edge. Calls `pr_state(url)` once per awaiting record and `run_hosts` once, and hands both to `awaiting_reviews`."""
    records = _load_records(Path(runs_dir))
    awaiting = [r for r in records if _awaiting(r)]
    hosts: Mapping[str, str] = run_hosts(Path(runs_dir), sorted({str(r.get("run", "")) for r in awaiting})) if awaiting else {}
    return awaiting_reviews(records, {_url(r): pr_state(_url(r)) for r in awaiting}, hosts)
