"""`cox inbox [--json]`, `cox inbox accept <id>` and `cox inbox deny <id>`; lists drafts, approvals, PRs, needs-chair rows and refused lands."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from agent_tools import route
from agent_tools.inbox import (
    Ambiguous,
    Found,
    InboxItem,
    Verb,
    find_item,
    render_json,
    render_text,
    run_verb,
    sort_oldest_first,
)
from agent_tools.inbox_approvals import approvals_to_items, load_approval_records
from agent_tools.inbox_drafts import drafts_to_items, load_draft_records
from agent_tools.inbox_needs_chair import load_needs_chair_records, needs_chair_to_items
from agent_tools.inbox_prs import load_pr_records, pr_errors, prs_to_items
from agent_tools.inbox_refused import load_refused_rows, refused_to_items
from core import bounded_proc

Records = Callable[[], Sequence[Mapping]]

USAGE = "usage: cox inbox [--json] | cox inbox accept <id> | cox inbox deny <id>"


@dataclass(frozen=True)
class Listing:
    as_json: bool


@dataclass(frozen=True)
class Act:
    verb: Verb
    query: str


def parse_argv(argv: Sequence[str]) -> Listing | Act | None:
    """None is a usage error."""
    match list(argv):
        case [] | ["--json"]:
            return Listing(bool(argv))
        case ["accept", query]:
            return Act("accept", query)
        case ["deny", query]:
            return Act("deny", query)
        case _:
            return None


def gather(
    drafts: Sequence[Mapping],
    approvals: Sequence[Mapping],
    prs: Sequence[Mapping],
    needs_chair: Sequence[Mapping],
    refused: Sequence[Mapping],
) -> tuple[InboxItem, ...]:
    """Oldest first; drafts, approvals and PRs keep one item per accept command, so a draft beats its approval.

    Needs-chair and refused items skip that dedup: needs-chair rows naming no task share one placeholder accept argv.
    """
    candidates = drafts_to_items(drafts) + approvals_to_items(approvals) + prs_to_items(prs)
    firsts = {i.accept_cmd: i for i in reversed(candidates)}
    kept = tuple(firsts.values()) + needs_chair_to_items(needs_chair) + refused_to_items(refused)
    return sort_oldest_first(kept)


def run_inbox(
    argv: Sequence[str],
    *,
    load_drafts: Records,
    load_approvals: Records,
    load_prs: Records,
    load_needs_chair: Records,
    load_refused: Records,
    run: Callable[[Sequence[str]], int],
    tz: tzinfo,
) -> int:
    """Exit 2 on bad arguments before any loader runs; exit 1 when a PR could not be read or an id does not resolve."""
    request = parse_argv(argv)
    if request is None:
        print(USAGE, file=sys.stderr)
        return 2
    pr_records = load_prs()
    errors = pr_errors(pr_records)
    for line in errors:
        print(f"cox inbox: could not read PR {line}", file=sys.stderr)
    items = gather(load_drafts(), load_approvals(), pr_records, load_needs_chair(), load_refused())
    if isinstance(request, Listing):
        print(json.dumps(render_json(items), indent=2) if request.as_json else render_text(items, tz))
        return 1 if errors else 0
    match find_item(items, request.query):
        case Found(item):
            return run_verb(item, request.verb, run)
        case Ambiguous(_, ids):
            print(f"cox inbox: id {request.query!r} is ambiguous: {', '.join(ids)}", file=sys.stderr)
            return 1
        case _:
            hint = "; some PRs could not be read, see above" if errors else ""
            print(f"cox inbox: no inbox item with id {request.query!r}{hint}", file=sys.stderr)
            return 1


def argv_of(a: argparse.Namespace) -> list[str]:
    """The `run_inbox` argv for a namespace parsed by `cli.INBOX_GROUP`."""
    return [*([a.action] if a.action else []), *([a.id] if a.id else []), *(["--json"] if a.json else [])]


class LocalTime(tzinfo):
    """Edge. The system timezone resolved per instant, so items on either side of a DST change get their own offset."""

    def fromutc(self, dt: datetime) -> datetime:
        return dt + timedelta(seconds=time.localtime(dt.replace(tzinfo=UTC).timestamp()).tm_gmtoff)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        stamp = time.time() if dt is None else time.mktime(dt.replace(tzinfo=None).timetuple())
        return timedelta(seconds=time.localtime(stamp).tm_gmtoff)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return time.strftime("%Z")


def subprocess_runner(argv: Sequence[str]) -> int:
    """Edge. Run with the terminal attached, under `bounded_proc`'s bound for the program (`gh` 120 s, else the step
    bound), which is 124 when it fires; a missing program is 127 and a child killed by signal N is 128+N."""
    try:
        code = bounded_proc.completed(list(argv)).returncode
    except OSError as exc:
        print(f"cox inbox: cannot run {argv[0]}: {exc}", file=sys.stderr)
        return 127
    return 128 - code if code < 0 else code


def _workspace() -> Path | None:
    """Edge. `workspace_dir` of the profile, read as inbox_approvals reads it; None with no profile."""
    profile = Path(os.environ.get("AGENT_TOOLS_PROFILE") or "~/.config/agent-tools/profile.yaml").expanduser()
    try:
        workspace = route.parse_profile(profile.read_text(encoding="utf-8")).get("workspace_dir", "")
    except (OSError, UnicodeDecodeError, route.ProfileError):
        return None
    return Path(workspace).expanduser() if workspace else None


def main(argv: Sequence[str]) -> int:
    """Edge. Bind the real loaders, the clock, the local timezone and a subprocess runner."""
    workspace = _workspace()
    now = datetime.now(UTC)
    return run_inbox(
        argv,
        load_drafts=lambda: load_draft_records(workspace / "work", now.isoformat()) if workspace else [],
        load_approvals=load_approval_records,
        load_prs=lambda: load_pr_records(workspace / "runs") if workspace else [],
        load_needs_chair=lambda: load_needs_chair_records(workspace / "runs") if workspace else [],
        load_refused=lambda: load_refused_rows(workspace / "runs") if workspace else [],
        run=subprocess_runner,
        tz=LocalTime(),
    )


def handler(a: argparse.Namespace) -> int:
    return main(argv_of(a))
