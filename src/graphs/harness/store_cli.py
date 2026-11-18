"""python -m harness.store_cli: mark-landed, set-state, lease, host, pause, resume, record-action and regenerate-states commands against the run-record store."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, TextIO, cast

import yaml

from harness import store_api
from harness.cli import profile_storage_url, read_provider_profile
from harness.store_api import db_errors, open_errors
from harness.store_dialect import Connection, Json, JsonObject, insert_ignore, json_text
from harness.store_hosts import (
    HOST_STATES,
    host_beat,
    host_delete,
    host_list,
    host_set_state,
    host_upsert,
    update_host_login,
)
from harness.store_migrate import open_store
from harness.store_read import work_items
from harness.work_mirror import set_frontmatter_state

# Contract read by coxswain-tools (pause and resume among them). Exit 0 and exit 3 print
# exactly one JSON object on stdout. Exit 2 prints nothing on stdout. Help and every error go to stderr.
# regenerate-states is an exception: it prints one plain line per change and no JSON, so agreement prints nothing.
# record-action is the other: it prints `recorded chair action <kind> <target>` and no JSON.
# host login is the third: it prints nothing on success, and bad JSON or an unknown host is exit 2, not exit 3.
EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_PRECONDITION = 3
# The states a caller may move a work item to, and every state a compare-and-set may name: both are every work state,
# the API's one enum, so the parser cannot drift from what `agent_tools` sends in process (a test holds them equal).
WORK_STATES = store_api.WORK_STATES
EXPECT_STATES = store_api.WORK_STATES

# What a command prints as its one JSON object: a mapping, or (for `host list`) an array of them.
type Payload = Mapping[str, object] | Sequence[Mapping[str, object]]


def resolve_store_url(flag: str | None, profile: Mapping[str, object], runs_dir: Path | str) -> str:
    """The flag, else what `harness cli` resolves: the profile's storage_url, else cox.db in the runs dir."""
    return flag or profile_storage_url(profile, runs_dir)


def _positive_seconds(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {value}")
    return value


def _iso_time(text: str) -> str:
    """The text unchanged, once it parses as ISO 8601."""
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {text!r}") from None
    return text


def _non_empty(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return text


def _versions(text: str) -> JsonObject:
    try:
        value: Json = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not JSON: {text!r}") from None
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return value


def add_common_args(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """Leaf parsers default to SUPPRESS so a flag given before the subcommand survives."""

    def default(value: str | None) -> str | None:
        return value if top else argparse.SUPPRESS

    parser.add_argument("--store-url", default=default(None), help="database URL; required unless --runs-dir is given")
    parser.add_argument("--runs-dir", default=default(None), help="directory holding cox.db; required when --store-url is not given")
    parser.add_argument(
        "--provider-profile",
        default=default(None),
        help="optional; its storage_url is used when --store-url is not given, else cox.db in --runs-dir",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m harness.store_cli", description=cast(str, __doc__).splitlines()[0])
    add_common_args(ap, top=True)
    commands = ap.add_subparsers(dest="command", required=True)

    landed = commands.add_parser("mark-landed", help="stamp the landed fields on a task record")
    add_common_args(landed, top=False)
    for name in ("run_id", "phase", "task"):
        landed.add_argument(name)
    landed.add_argument("--pr", required=True, help="pull request URL")
    landed.add_argument("--at", required=True, type=_iso_time, help="landed time, ISO 8601")

    state = commands.add_parser("set-state", help="upsert one work item's state")
    add_common_args(state, top=False)
    state.add_argument("initiative")
    state.add_argument("task")
    state.add_argument("state", choices=WORK_STATES)
    state.add_argument("--by", required=True, type=_non_empty, help="who made the change")
    state.add_argument("--phase", help="phase for a new row; ignored when the row exists")
    state.add_argument("--expect", choices=EXPECT_STATES, help="apply only if the row's current state equals this; exit 3 otherwise")

    lease = commands.add_parser("lease", help="acquire, renew or release a named lease")
    actions = lease.add_subparsers(dest="action", required=True)

    acquire = actions.add_parser("acquire")
    add_common_args(acquire, top=False)
    acquire.add_argument("name")
    acquire.add_argument("holder")
    acquire.add_argument("--ttl", type=_positive_seconds, required=True, help="seconds")
    acquire.add_argument("--steal", action="store_true", help="take a lease another holder still holds, at epoch + 1")

    renew = actions.add_parser("renew")
    add_common_args(renew, top=False)
    renew.add_argument("name")
    renew.add_argument("holder")
    renew.add_argument("epoch", type=int)
    renew.add_argument("--ttl", type=_positive_seconds, required=True, help="seconds")

    release = actions.add_parser("release")
    add_common_args(release, top=False)
    release.add_argument("name")
    release.add_argument("holder")
    release.add_argument("epoch", type=int)

    host = commands.add_parser("host", help="upsert, beat, set-state, login, delete or list the hosts table")
    host_actions = host.add_subparsers(dest="action", required=True)

    upsert_host = host_actions.add_parser("upsert", help="insert or overwrite a host, keeping its last beat")
    add_common_args(upsert_host, top=False)
    upsert_host.add_argument("name", type=_non_empty)
    upsert_host.add_argument("--ssh", required=True, type=_non_empty)
    upsert_host.add_argument("--capacity", required=True, type=int)
    upsert_host.add_argument("--state", choices=HOST_STATES, default="active")
    upsert_host.add_argument("--by", required=True, type=_non_empty, help="who made the change")

    beat = host_actions.add_parser("beat", help="stamp a host's beat time and what it reports")
    add_common_args(beat, top=False)
    beat.add_argument("name")
    beat.add_argument("--versions", required=True, type=_versions, help="a JSON object: what the host reports")

    host_state = host_actions.add_parser("set-state", help="change a host's state")
    add_common_args(host_state, top=False)
    host_state.add_argument("name")
    host_state.add_argument("state", choices=HOST_STATES)
    host_state.add_argument("--by", required=True, type=_non_empty, help="who made the change")

    host_login = host_actions.add_parser("login", help="merge login fields into a host's versions, leaving its beat")
    add_common_args(host_login, top=False)
    host_login.add_argument("name", type=_non_empty)
    host_login.add_argument("--login-json", required=True, help="a JSON object: the login fields to merge")

    delete = host_actions.add_parser("delete", help="delete a draining host's row and print it as it was")
    add_common_args(delete, top=False)
    delete.add_argument("name")
    delete.add_argument("--by", required=True, type=_non_empty, help="who made the change")

    listing = host_actions.add_parser("list", help="print every host, by name")
    add_common_args(listing, top=False)

    pause = commands.add_parser("pause", help="set a run's paused_at to now")
    add_common_args(pause, top=False)
    pause.add_argument("run_id")
    pause.add_argument("--reason", help="optional free-text reason, echoed back in the JSON")

    resume = commands.add_parser("resume", help="clear a run's paused_at")
    add_common_args(resume, top=False)
    resume.add_argument("run_id")

    record = commands.add_parser("record-action", help="record one chair action line as a chair_actions row")
    add_common_args(record, top=False)
    record.add_argument("--holder", required=True, type=_non_empty, help="the lease holder that took the action")
    record.add_argument("action_json", metavar="json", help="the action line: an object with ts, epoch, kind and status")

    regen = commands.add_parser(
        "regenerate-states", help="report, or with --apply fix, ticket files whose state: differs from the store"
    )
    add_common_args(regen, top=False)
    regen.add_argument("work_dir", type=Path, help="the work root holding <initiative>/<phase>/<task>.md")
    regen.add_argument("--initiative", required=True, type=_non_empty)
    regen.add_argument(
        "--apply", action="store_true", help="rewrite each differing state: line; without it nothing is written"
    )
    return ap


class Change(NamedTuple):
    name: str  # the key of the file text in the mapping given to plan_regenerate
    task_id: str
    file_state: str
    store_state: str | None  # None: the store has no row for the task

    @property
    def skipped(self) -> bool:
        return self.store_state is None


def ticket_facts(name: str, text: str) -> tuple[str, str] | None:
    """(task id, file state) from the frontmatter, the id falling back to the file stem; None if there is no state."""
    lines = text.split("\n")
    close = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), None)
    if lines[0].rstrip("\r") != "---" or close is None:
        return None
    try:
        loaded: object = yaml.safe_load("\n".join(lines[1:close]))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    data = cast("dict[str, object]", loaded)
    if not isinstance(data.get("state"), str):
        return None
    return (str(data.get("id") or Path(name).stem), cast(str, data["state"]))


def plan_regenerate(texts: Mapping[str, str], rows: Mapping[str, Mapping[str, object]]) -> list[Change]:
    """One Change per file whose state differs from its row, and one per file with no row. rows is keyed by task id."""
    facts = [(name, ticket_facts(name, text)) for name, text in sorted(texts.items())]
    return [
        Change(name, task_id, state, None if row is None else cast(str, row["state"]))
        for name, found in facts
        if found is not None
        for task_id, state in [found]
        for row in [rows.get(task_id)]
        if row is None or row["state"] != state
    ]


def format_change(change: Change) -> str:
    if change.skipped:
        return f"{change.task_id}: no row, skipped"
    return f"{change.task_id}: file state {change.file_state}, store state {change.store_state}"


def regenerate_states(conn: Connection, work_dir: Path, initiative: str, apply: bool) -> list[str]:
    """The report lines. Edge: reads ticket files and the store's rows; only with apply does it write ticket files."""
    paths = sorted(p for p in (work_dir / initiative).glob("*/*.md") if p.name != "initiative.md")
    texts = {str(p): p.read_bytes().decode("utf-8") for p in paths}
    rows = {row["task_id"]: row for row in work_items(conn, initiative)}
    changes = plan_regenerate(texts, rows)
    if apply:
        for change in changes:
            if change.store_state is not None:
                new = set_frontmatter_state(texts[change.name], change.store_state)
                if new is not None:
                    Path(change.name).write_bytes(new.encode("utf-8"))
    return [format_change(change) for change in changes]


def _regenerate(conn: Connection, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        lines = regenerate_states(conn, args.work_dir, args.initiative, args.apply)
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(f"cannot read or write the ticket files: {exc}", err)
    for line in lines:
        print(line, file=out)
    return EXIT_OK


CHAIR_ACTION_COLUMNS = ("ts", "epoch", "holder", "kind", "target", "status", "reason", "action_json")
CHAIR_ACTION_KEY = ("ts", "holder", "kind", "target")
_ACTION_REQUIRED = ("ts", "epoch", "kind", "status")


def parse_action(text: str) -> JsonObject | str:
    """The action object, or a one-line reason it is refused."""
    try:
        action: Json = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"the action is not JSON: {exc}"
    if not isinstance(action, dict):
        return "the action must be a JSON object"
    missing = [k for k in _ACTION_REQUIRED if k not in action]
    return f"the action lacks {', '.join(missing)}" if missing else action


def parse_login(text: str) -> JsonObject | str:
    """The login object, or a one-line reason it is refused."""
    try:
        login: Json = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"the login is not JSON: {exc}"
    return login if isinstance(login, dict) else "the login must be a JSON object"


def action_target(action: Mapping[str, object]) -> str:
    """task_id, else initiative, else the first intake id, else the empty string."""
    intake = action.get("intake_ids")
    first = cast("list[object]", intake)[0] if isinstance(intake, list) and intake else ""
    return next((str(v) for v in (action.get("task_id"), action.get("initiative"), first) if v), "")


def action_row(holder: str, action: Mapping[str, object]) -> dict[str, object]:
    return {
        "ts": action["ts"],
        "epoch": action["epoch"],
        "holder": holder,
        "kind": action["kind"],
        "target": action_target(action),
        "status": action["status"],
        "reason": str(action.get("reason", "")),
        "action_json": json_text(action),
    }


def _record_action(conn: Connection, holder: str, action: Mapping[str, object], out: TextIO) -> int:
    """Edge: insert the row, skipping one whose key is already there, and print the one line."""
    row = action_row(holder, action)
    sql = insert_ignore(conn.dialect, "chair_actions", CHAIR_ACTION_COLUMNS, CHAIR_ACTION_KEY)
    conn.execute(sql, list(row.values()))
    print(f"recorded chair action {row['kind']} {row['target']}", file=out)
    return EXIT_OK


def _host_login(conn: Connection, name: str, login: JsonObject, err: TextIO) -> int:
    """Edge: write through update_host_login only; silent on success, one stderr line on an unknown host."""
    return EXIT_OK if update_host_login(conn, name, login) else _fail(f"no host {name}", err)


def _host(conn: Connection, args: argparse.Namespace, now: str) -> tuple[Payload, int]:
    """An unknown host is the empty object with exit 3; list is the array of rows."""
    if args.action == "list":
        return host_list(conn), EXIT_OK
    if args.action == "upsert":
        return host_upsert(conn, args.name, args.ssh, args.capacity, args.state, args.by, now), EXIT_OK
    if args.action == "beat":
        row = host_beat(conn, args.name, args.versions, now)
    elif args.action == "delete":
        row = host_delete(conn, args.name)
    else:
        row = host_set_state(conn, args.name, args.state, args.by, now)
    return ({}, EXIT_PRECONDITION) if row is None else (row, EXIT_OK)


def lease_payload(
    action: str, result: store_api.LeaseGranted | store_api.LeaseRefused | store_api.LeaseReleased
) -> JsonObject:
    """Pure: the `{ok, epoch, holder}` object a lease command prints. Only an acquire names the epoch it granted; a
    renew keeps the caller's epoch and a release ends it, so both print epoch and holder null."""
    if isinstance(result, store_api.LeaseRefused):
        return {"ok": False, "epoch": result.epoch, "holder": result.holder}
    if isinstance(result, store_api.LeaseGranted) and action == "acquire":
        return {"ok": True, "epoch": result.epoch, "holder": result.holder}
    return {"ok": True, "epoch": None, "holder": None}


def _lease(conn: Connection, args: argparse.Namespace, now: str) -> tuple[Payload, int]:
    if args.action == "acquire":
        result = store_api.lease_acquire_on(conn, args.name, args.holder, now, args.ttl, steal=args.steal)
    elif args.action == "renew":
        result = store_api.lease_renew_on(conn, args.name, args.holder, args.epoch, now, args.ttl)
    else:
        result = store_api.lease_release_on(conn, args.name, args.holder, args.epoch)
    refused = isinstance(result, store_api.LeaseRefused)
    return lease_payload(args.action, result), EXIT_PRECONDITION if refused else EXIT_OK


def _set_state(conn: Connection, args: argparse.Namespace, now: str) -> tuple[Payload, int]:
    """A refused compare-and-set prints the state it found; a missing row with no --phase prints the empty object."""
    result = store_api.set_state_on(
        conn, args.initiative, args.task, args.state, args.by, now, phase=args.phase, expected=args.expect
    )
    if isinstance(result, store_api.StateSet):
        return dict(result.record), EXIT_OK
    return ({"actual": result.current, "expected": args.expect} if args.expect is not None else {}), EXIT_PRECONDITION


def dispatch(conn: Connection, args: argparse.Namespace, now: str) -> tuple[Payload, int]:
    """Every row command goes through `store_api`; a missing task record is the empty object with exit 3."""
    if args.command == "mark-landed":
        landed = store_api.mark_landed_on(conn, args.run_id, args.phase, args.task, args.pr, args.at)
        return ({}, EXIT_PRECONDITION) if isinstance(landed, store_api.NotInStore) else (landed.record, EXIT_OK)
    if args.command == "set-state":
        return _set_state(conn, args, now)
    if args.command == "host":
        return _host(conn, args, now)
    if args.command == "pause":
        if isinstance(store_api.pause_on(conn, args.run_id, now, args.reason), store_api.PauseRefused):
            return {"ok": False, "run": args.run_id, "paused": None, "reason": None}, EXIT_PRECONDITION
        return {"ok": True, "run": args.run_id, "paused": now, "reason": args.reason}, EXIT_OK
    if args.command == "resume":
        if isinstance(store_api.resume_on(conn, args.run_id), store_api.ResumeRefused):
            return {"ok": False, "run": args.run_id, "paused": None, "reason": None}, EXIT_PRECONDITION
        return {"ok": True, "run": args.run_id, "paused": None}, EXIT_OK
    return _lease(conn, args, now)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refusal(args: argparse.Namespace, payload: Payload) -> str:
    if args.command == "host" and args.action == "delete":
        return f"no draining host {args.name}"
    if args.command == "host":
        return f"no host {args.name}"
    if args.command == "mark-landed":
        return f"no task record for run {args.run_id} phase {args.phase} task {args.task}"
    if args.command == "set-state" and "actual" in payload:
        found = cast("Mapping[str, str | None]", payload)
        return store_api.refusal_detail(args.task, found["actual"], cast(str, found["expected"]))
    if args.command == "set-state":
        return store_api.no_row_detail(args.initiative, args.task)
    if args.command in ("pause", "resume"):
        return f"no run {args.run_id}"
    return f"lease {args.name} refused for holder {args.holder}"


def _fail(message: str, err: TextIO) -> int:
    print(f"error: {message}", file=err)
    return EXIT_BAD_INPUT


def main(argv: Sequence[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`out` and `err` default to the process's own streams; an in-process caller (`agent_tools.store_cli.runner`)
    passes buffers, so the same command runs without a child interpreter."""
    out = cast(TextIO, out or sys.stdout)
    err = cast(TextIO, err or sys.stderr)
    try:
        with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_BAD_INPUT
    if not args.store_url and args.runs_dir is None:
        return _fail("--store-url or --runs-dir is required", err)
    if args.command == "regenerate-states" and not (args.work_dir / args.initiative).is_dir():
        return _fail(f"no directory {args.work_dir / args.initiative}", err)
    action = parse_action(args.action_json) if args.command == "record-action" else None
    if isinstance(action, str):
        return _fail(action, err)
    login = parse_login(args.login_json) if args.command == "host" and args.action == "login" else None
    if isinstance(login, str):
        return _fail(login, err)
    now = _now()
    profile: Mapping[str, object] = {} if args.provider_profile is None else read_provider_profile(args.provider_profile)
    url = resolve_store_url(args.store_url, profile, args.runs_dir)
    try:
        conn = open_store(url, now)
    except open_errors() as exc:
        return _fail(f"cannot open the store: {exc}", err)
    try:
        if args.command == "regenerate-states":
            return _regenerate(conn, args, out, err)
        if action is not None:
            return _record_action(conn, args.holder, action, out)
        if login is not None:
            return _host_login(conn, args.name, login, err)
        payload, code = dispatch(conn, args, now)
    except db_errors() as exc:
        return _fail(f"cannot read the store: {exc}", err)
    finally:
        conn.close()
    if code == EXIT_PRECONDITION:
        print(f"error: {_refusal(args, payload)}", file=err)
    print(json.dumps(payload, sort_keys=True), file=out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
