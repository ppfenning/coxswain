"""`cox chair` and `cox lander`: the run loop, the service units, decisions, attach and the watchdog."""

from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import datetime
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_tools.cli import _CHAIR_RUN_INTERVAL, build_parser
from agent_tools.cli_chair import _chair_once_exit, _chair_run_deps, _chair_unwired_sources
from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_host import _chair_heads, _chair_service_host_repos, _origin_url
from agent_tools.cli_lake import _lake_provider
from agent_tools.cli_land import _leader_pid_alive, _leader_read_or_refuse
from agent_tools.cli_lazy import (
    bounded_run,
    chair,
    chair_holder,
    chair_land_freeze,
    chair_report,
    chair_restart,
    chair_run,
    chair_service,
    chair_service_host,
    chair_watchdog,
    courier,
    decisions,
    install_root,
    notify_core,
    route,
    run_store,
    settings_model,
    unit_env,
    work_state,
)
from agent_tools.cli_leader import _is_chair_run_command, _pid_command
from agent_tools.cli_route import _courier_profile
from agent_tools.cli_runs import _leader_heartbeat_minutes, _leader_runs_dir_or_refuse
from agent_tools.cli_runs_fetch import _holder_label


def _write_private(path: Path, text: str) -> None:
    """Edge. A unit or agent file, readable by its owner only: created 0600 before any byte is written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def _chair_run(a: argparse.Namespace) -> int:
    """Refuses before any beat while a fact source or recorder is unwired, so a chair that cannot act never holds the lease."""
    as_holder = bool(getattr(a, "as_holder", False))
    if as_holder and not a.dry_run:
        print("chair run: refusing to start, no lease taken; --as-holder needs --dry-run")
        return 2
    profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    last: collections.deque[str] = collections.deque([""], maxlen=1)

    def echo(line: str) -> None:
        chair_report.echo_line(line)
        last.append(line)

    provider, problem = _lake_provider(a)
    if problem:  # an unreadable provider profile would read as mode "files" and relaunch landed work
        print(f"chair run: refusing to start, no lease taken; lake: {problem}")
        return 2
    deps = _chair_run_deps(
        runs_dir, profile, _holder_label(a), os.getpid(), socket.gethostname(), a.dry_run, echo,
        _profile_path(a), work_state.work_state_mode(provider),
    )
    deps = dataclasses.replace(deps, force_holder=as_holder)
    missing = _chair_unwired_sources(deps)
    if missing:
        print(f"chair run: refusing to start, no lease taken; not wired: {', '.join(missing)}")
        return 2
    chair_run.run(a.once, a.interval, a.dry_run, deps, a.max_overruns)
    if a.once and deps.holds():  # a one-shot process exits here, so its lease must not outlive it
        deps.release()
    if not a.once and deps.restart_state.moved is not None:
        return chair_restart.RESTART_EXIT_CODE
    if not a.once and chair_run.overrun_limit_hit(deps.overrun_state.consecutive, a.max_overruns):
        print(f"chair run: stopping after {deps.overrun_state.consecutive} facts-stage overruns in a row; exit 3 for the supervisor")
        return 3
    return _chair_once_exit(last[0]) if a.once else 0


def _chair_worker(a: argparse.Namespace) -> int:
    """`cox chair worker --kind K`: one kind of the chair's work in its own process (`chair_queue`). Refuses, holding no
    lease, while a fact source or recorder is unwired, as `chair run` does.

    Every lease call opens its own connection to the store (the beat thread and the fence never share one) and is
    bounded by `chair_queue.CALL_TIMEOUT_SECONDS`, so a store that hangs reads as a lost lease."""
    from agent_tools import chair_kinds, chair_queue, chair_read_queues
    from harness import store_chair_lease
    from harness.store_dialect import connect

    if a.kind not in chair_kinds.QUEUE_KINDS:
        print(f"chair worker: no queue worker for kind {a.kind!r}; one of: {', '.join(chair_kinds.QUEUE_KINDS)}")
        return 2
    profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    provider, problem = _lake_provider(a)
    if problem:
        print(f"chair worker: refusing to start, no lease taken; lake: {problem}")
        return 2
    held = chair_queue.Held()  # the kind lease's epoch, read by the recorder, the fence and the pass's beat thread
    pid, host = os.getpid(), socket.gethostname()
    session = _holder_label(a)
    url = run_store.store_url_for(runs_dir)
    name, holder = chair_kinds.lease_name(a.kind), f"{a.kind}-{chair.lease_holder(session, pid, host)}"
    ttl, limit = chair_queue.LEASE_TTL_SECONDS, chair_queue.CALL_TIMEOUT_SECONDS

    def on_store(call, otherwise):
        def opened():
            conn = connect(url)
            try:
                return call(conn)
            finally:
                conn.close()

        return chair_queue.bounded(opened, limit, otherwise)

    def record_store(argv: list[str]) -> tuple[int, str]:
        """An action's row, fenced on the kind lease. The effect has already run when its row is written, so a lease
        lost in between does not drop the row: it is written unfenced and marked `late` (`store_chair_lease.late`)."""
        epoch = held.epoch
        action = json.loads(argv[-1])
        kept = epoch is not None and on_store(
            lambda c: store_chair_lease.record_fenced(c, name, holder, epoch, argv[2], action), False
        )
        if kept:
            return 0, ""
        wrote = on_store(lambda c: store_chair_lease.record_late(c, argv[2], action) or True, False)
        if wrote:
            print(f"chair worker: {action.get('kind')} recorded late: {name} was lost after it ran", file=sys.stderr)
            return 0, ""
        return 1, f"the kind lease {name} was lost and the late row could not be written either"

    deps = _chair_run_deps(
        runs_dir, profile, session, pid, host, a.dry_run, chair_report.echo_line,
        _profile_path(a), work_state.work_state_mode(provider),
        worker_epoch=lambda: held.epoch if held.epoch is not None else -1, record_store=record_store,
    )
    missing = _chair_unwired_sources(deps)
    if missing:
        print(f"chair worker: refusing to start, no lease taken; not wired: {', '.join(missing)}")
        return 2

    def gather(now: datetime.datetime) -> object:
        deps.beat()  # a worker's beat only clears the read caches of the last pass
        return deps.gather(deps.facts_deps, now)

    def perform_one(action: dict, current: Callable[[], int]) -> list:
        return deps.perform([action], deps.exec_deps, current, a.dry_run)

    wait, close = chair_queue.store_waiter(url, time.sleep)
    ports = chair_queue.Ports(
        queued=chair_read_queues.queues_keeping_last_good(_profile_path(a)),
        acquire=lambda: on_store(lambda c: store_chair_lease.acquire(c, name, holder, ttl), None),
        renew=lambda epoch: on_store(lambda c: store_chair_lease.renew(c, name, holder, epoch, ttl), False),
        holds=lambda epoch: on_store(lambda c: store_chair_lease.holds(c, name, holder, epoch), False),
        release=lambda epoch: on_store(lambda c: store_chair_lease.release(c, name, holder, epoch), False),
        gather=gather, perform_one=perform_one,  # type: ignore[arg-type]
        wait=wait, pause=close, clock=time.monotonic, now=lambda: datetime.datetime.now(datetime.UTC),
        echo=chair_report.echo_line,
    )
    poll = a.poll if a.poll is not None else chair_kinds.POLL_SECONDS[a.kind]
    timing = chair_queue.Timing(poll=poll, min_gap=a.min_gap, settle=a.settle)
    restore = (lambda: None) if a.dry_run else chair_run.sigterm_as_interrupt()
    try:
        return chair_queue.run(a.kind, ports, timing, a.once, a.dry_run, held)
    finally:
        close()
        restore()


def _chair_default_environment_file() -> str | None:
    garage = Path(unit_env.env_file_path(str(Path.home())))
    return str(garage) if garage.is_file() else None


def _chair_service_environment_file(a: argparse.Namespace) -> str | None:
    if a.environment_file:
        return os.path.abspath(Path(a.environment_file).expanduser())
    return _chair_default_environment_file()


@dataclasses.dataclass(frozen=True)
class _ChairUnitRefusal:
    """Why no chair unit text could be built; `message` is printed as is."""

    message: str


@dataclasses.dataclass(frozen=True)
class _ChairUnitInputs:
    """The absolute, systemd-safe values a chair unit is built from."""

    cox: str
    workspace_dir: str
    profile: str
    path: str
    env_file: str | None


def _chair_unit_inputs(label: str | None, env_file: str | None, profile_path: Path) -> _ChairUnitInputs | _ChairUnitRefusal:
    """Edge-light. Resolves cox, the profile, PATH and the env file, or says why not; writes and prints nothing.
    A `label` of None means the unit has none, so there is no label to check."""
    cox = shutil.which("cox")
    if cox is None:
        return _ChairUnitRefusal("chair service: cox is not on PATH")
    text = _read_text_or_none(profile_path)
    if text is None:
        return _ChairUnitRefusal(f"routing: no profile at {profile_path}")
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return _ChairUnitRefusal(f"routing: profile unreadable: {exc}")
    if not profile.get("workspace_dir", ""):
        return _ChairUnitRefusal(f"routing: workspace_dir not set in profile {profile_path}")
    harness_dir = profile.get("harness_dir")
    # The tools venv: the coxswain checkout's, else (an unmigrated host) the tools checkout's own.
    tools_root = install_root.checkout_root(Path(__file__).parent) or Path(__file__).resolve().parents[1]
    unit_path_value = chair_service.path_value(
        os.path.abspath(cox),
        str(install_root.venv_python(tools_root).parent),
        str(install_root.harness_python(harness_dir).parent) if harness_dir else "",
        os.environ.get("PATH", ""),
    )
    # systemd does not expand `~/` in WorkingDirectory=, and the unit does not inherit this shell's
    # $AGENT_TOOLS_PROFILE, so every path is absolute and the profile is pinned in ExecStart.
    values = {
        "cox": os.path.abspath(cox),
        "workspace_dir": os.path.abspath(Path(profile["workspace_dir"]).expanduser()),
        **({"--label": label} if label is not None else {}),
        "--profile": os.path.abspath(profile_path),
        **({"--environment-file": env_file} if env_file is not None else {}),
        "PATH": unit_path_value,
    }
    unsafe = chair_service.unsafe_values(values)
    if unsafe:
        return _ChairUnitRefusal(
            f"chair service: systemd would split or expand {', '.join(unsafe)}; use only letters, digits and . _ @ + : , / = -"
        )
    exports = chair_service.export_lines(_read_text_or_none(Path(env_file)) or "") if env_file is not None else []
    if exports:
        numbers = ", ".join(str(n) for n in exports)
        return _ChairUnitRefusal(
            f"chair service: {env_file} has export lines ({numbers}) that EnvironmentFile= may skip; pass --environment-file with NAME=value lines"
        )
    return _ChairUnitInputs(values["cox"], values["workspace_dir"], values["--profile"], unit_path_value, env_file)


def _chair_unit_text(label: str, interval: int | str, env_file: str | None, profile_path: Path) -> str | _ChairUnitRefusal:
    """Edge-light. The unit text for these arguments, or the refusal; install and `setup migrate` both build it here."""
    resolved = _chair_unit_inputs(label, env_file, profile_path)
    if isinstance(resolved, _ChairUnitRefusal):
        return resolved
    return chair_service.unit_text(
        resolved.cox, resolved.workspace_dir, label, interval, env_file, resolved.profile, resolved.path,
    )


def _lander_unit_text(a: argparse.Namespace, ws: Path) -> str | None:
    """Edge-light. The lander's unit text, built from the same resolved cox, PATH and profile as the chair's; None after
    printing why when systemd would read one of them differently from this shell."""
    from agent_tools import lander_cli

    env_file = _chair_service_environment_file(a)
    resolved = _chair_unit_inputs("lander", env_file, _profile_path(a))
    if isinstance(resolved, _ChairUnitRefusal):
        print(resolved.message)
        return None
    return lander_cli.unit_text(resolved.cox, str(ws), int(a.interval), resolved.profile, resolved.path, env_file)


def _chair_service_install(a: argparse.Namespace, path: Path) -> int:
    """Refuses before any write when systemd would read a value differently from the shell that ran the install."""
    text = _chair_unit_text(_holder_label(a), a.interval, _chair_service_environment_file(a), _profile_path(a))
    if isinstance(text, _ChairUnitRefusal):
        print(text.message)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(path)
    print("\n".join(chair_service.systemctl_lines()))
    return 0


def _chair_service_host_steps(a: argparse.Namespace, profile: Mapping, runs_dir: Path) -> list | int:
    """The install steps for `a.host`, or an exit code after printing why there are none."""
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.host), None)
    if row is None:
        print(f"unknown host: {a.host}")
        return 2
    root = str(Path(profile["workspace_dir"]).expanduser().parent)
    workspace_remote = _origin_url(str(Path(profile["workspace_dir"]).expanduser())) or ""
    return chair_service_host.install_steps(
        str(row["ssh"]), root, _chair_service_host_repos(profile), workspace_remote, _holder_label(a), _chair_heads(profile),
    )


def _chair_service_host_print(steps: list) -> int:
    for argv, run in steps:
        line = shlex.join(argv)
        print(line if run else f"{line}  # run by hand")
    return 0


def _chair_service_host_apply(steps: list) -> int:
    """Runs the runnable steps in order; stops at the first non-zero exit and prints its output, then
    prints every step still left (runnable or not) as a line for a person to run by hand."""
    for i, (argv, run) in enumerate(steps):
        if not run:
            print(f"{shlex.join(argv)}  # run by hand")
            continue
        done = bounded_run.completed(argv, capture_output=True, text=True)
        if done.returncode != 0:
            print((done.stdout + done.stderr).strip())
            for left_argv, _left_run in steps[i + 1 :]:
                print(f"{shlex.join(left_argv)}  # run by hand")
            return done.returncode
    return 0


def _chair_service_host_install(a: argparse.Namespace, profile: Mapping, runs_dir: Path) -> int:
    steps = _chair_service_host_steps(a, profile, runs_dir)
    if isinstance(steps, int):
        return steps
    if not getattr(a, "apply", False):
        return _chair_service_host_print(steps)
    return _chair_service_host_apply(steps)


def _chair_service_host_ssh_line(ssh: str, cmd: str) -> str | None:
    """The stdout of `ssh ssh cmd`, or None on a non-zero exit or a 30s timeout. Never raises."""
    try:
        done = subprocess.run(["ssh", ssh, cmd], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    return done.stdout if done.returncode == 0 else None


def _chair_service_host_status(a: argparse.Namespace, runs_dir: Path) -> int:
    """Prints `chair_service_host.status_line` for `a.host`; an unknown name prints one line and exits 2."""
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.host), None)
    if row is None:
        print(f"unknown host: {a.host}")
        return 2
    ssh = str(row["ssh"])
    unit = _chair_service_host_ssh_line(ssh, "systemctl --user is-active coxswain-chair.service")
    unit_state = unit.strip() if unit is not None else None
    runs_top = _chair_service_host_ssh_line(ssh, "cox runs top --once")
    chair_line = runs_top.splitlines()[0] if runs_top else None
    tick = _chair_service_host_ssh_line(ssh, "tail -1 repos/workspace/runs/chair-loop.log")
    last_tick = tick.strip() if tick else None
    print(chair_service_host.status_line(a.host, unit_state, chair_line, last_tick))
    return 0


def _chair_service(a: argparse.Namespace) -> int:
    """Edge for the unit text in `chair_service`. Never runs systemctl; `--install` wins when both flags are given."""
    path = chair_service.unit_path(Path.home())
    host = getattr(a, "host", None)
    if a.install and host:
        profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
        if refuse_rc is not None:
            return refuse_rc
        return _chair_service_host_install(a, profile, runs_dir)
    if a.install:
        return _chair_service_install(a, path)
    if a.status and host:
        _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
        if refuse_rc is not None:
            return refuse_rc
        return _chair_service_host_status(a, runs_dir)
    if a.status:
        text = _read_text_or_none(path)
        if text is None:
            print(f"no unit at {path}")
        else:
            print(text, end="")
        return 0
    with contextlib.suppress(SystemExit):
        build_parser().parse_args(["chair", "service", "--help"])
    return 0


_DECISION_PREFIX = "coxswain://decision/"


def _decision_bus(a: argparse.Namespace) -> tuple[Path, str, list[dict], str] | None:
    """The courier path, its raw text, its latest-per-id entries and the profile's operator; None when no workspace resolves."""
    profile = _courier_profile(a)
    if profile is None: return None
    path = Path(profile["workspace_dir"]).expanduser() / "courier.jsonl"
    blob = courier.read_text(path)
    return path, blob, courier.entries(blob), settings_model.operator_name(profile)


def _new_decision_id(taken: set[str]) -> str | None:
    """Up to five draws; None when every one collides with a taken ref."""
    drawn = (f"d-{secrets.token_hex(4)}" for _ in range(5))
    return next((d for d in drawn if f"{_DECISION_PREFIX}{d}" not in taken), None)


def _decision_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _chair_ask(a: argparse.Namespace) -> int:
    bus = _decision_bus(a)
    if bus is None: return 2
    path, blob, entries, operator = bus
    problem = decisions.validate_options(a.option)
    if problem: print(f"chair ask: {problem}"); return 2
    decision_id = _new_decision_id({e["ref"] for e in entries})
    if decision_id is None: print("chair ask: could not draw an unused decision id"); return 2
    entry = decisions.make_ask_entry(
        a.question, a.option, a.context, _holder_label(a), decision_id, _decision_now(), operator=operator
    )
    courier.append_file(path, entry)
    print(decision_id)
    return 0


def _chair_answer(a: argparse.Namespace) -> int:
    bus = _decision_bus(a)
    if bus is None: return 2
    path, blob, entries, operator = bus
    asks, _ = decisions.inbox_split(entries, operator)
    ask_entry = next((e for e in asks if e["ref"] == f"{_DECISION_PREFIX}{a.id}"), None)
    problem = decisions.validate_answer(ask_entry, a.option)
    if problem: print(f"chair answer: {problem}"); return 2
    answer = decisions.make_answer_entry(
        ask_entry, a.option, answered_at=_decision_now(), message_id=uuid.uuid4().hex, operator=operator
    )
    courier.append_file(path, answer)
    courier.ack_file(path, ask_entry["id"])
    return 0


def _decision_line(d: dict) -> str:
    answer = f" -> {d['answer']}" if d["answer"] is not None else ""
    return f"{d['id']}  {d['asked_at']}  {d['question']}  [{' | '.join(d['options'])}]{answer}"


def _chair_decisions(a: argparse.Namespace) -> int:
    bus = _decision_bus(a)
    if bus is None: return 2
    _, _, entries, operator = bus
    asks, answers = decisions.inbox_split(entries, operator)
    listed = decisions.filter_decisions(decisions.merge_decisions(asks, answers), open_only=a.open, answered_only=a.answered)
    if a.json: print(json.dumps(listed))
    else:
        for d in listed: print(_decision_line(d))
    return 0


def _chair_lease_row(runs_dir: Path) -> dict | None:
    """Edge, read only (`run_store.open_store` is `mode=ro`). The whole chair lease row, `status` included; None with no store or no row.

    A store error is raised, not read as no lease: `read_lease` and `read_chair_rows` fold it into None, and a false "no lease"
    sends the operator to take a held lease. `_chair_attach` turns the raise into its one-line reason."""
    opened = run_store.open_store(runs_dir)
    if opened is None:
        return None
    conn, mark = opened
    try:
        row = conn.execute(f"SELECT * FROM leases WHERE name = {mark}", (chair.LEASE_NAME,)).fetchone()
    finally:
        conn.close()
    return None if row is None else dict(row)


def _attach_time(value: object) -> datetime.datetime | None:
    """Pure. An aware datetime from a datetime or ISO text, naive read as UTC; None when absent or unparseable."""
    try:
        at = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=datetime.UTC)


def _attach_inputs(row: Mapping) -> tuple[object, datetime.datetime | None] | None:
    """Pure. `(HolderLease, last_tick)` from a lease row; None when the holder is not `label@host:pid` or the expiry does not parse.

    Only the chair loop writes a `tick_at` into the `status` cell (`chair_tick_status`), and a Claude session holder is
    `label@host:pid` too, so a tick marks a loop. A row with no `status` column cannot say, so it counts as a loop and
    the pid and tick checks decide; a store that old must not answer "not a loop" for a live one."""
    label, _, rest = str(row.get("holder") or "").partition("@")
    host, _, pid = rest.rpartition(":")
    expires_at = _attach_time(row.get("expires_at"))
    if not (label and host and pid.isdigit() and expires_at):
        return None
    doc = run_store.json_cell(row.get("status")) if row.get("status") else None
    last_tick = _attach_time(doc.get("tick_at")) if isinstance(doc, dict) else None
    return chair_holder.HolderLease(label, int(pid), host, expires_at, "status" not in row or last_tick is not None), last_tick


def _chair_attach_lend(a: argparse.Namespace, on: bool, clock: Callable[[], datetime.datetime] | None = None) -> int:
    """Edge. Lend or recall the routine ticks on chair.json; only the live holder may, anyone else is refused with exit 1."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    host = socket.gethostname()
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return 1
        now = (clock or (lambda: datetime.datetime.now(datetime.UTC)))()
        state = chair.liveness(record, _leader_pid_alive(record), now, host, _leader_heartbeat_minutes())
        if record is None or state != "live" or not chair.is_caller_holder(record, os.environ, host):
            print(f"chair: not lent: this session is not the live holder (lease {state})")
            return 1
        chair.write(runs_dir, chair.lend_routine(record, on))
    print("chair: routine ticks lent" if on else "chair: routine ticks recalled")
    return 0


def _chair_attach(
    a: argparse.Namespace,
    read_row: Callable[[Path], dict | None] | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    clock: Callable[[], datetime.datetime] | None = None,
    pid_command: Callable[[int], str] | None = None,
) -> int:
    """The explicit holder check. Reads the lease, this host, the clock and the pid; `chair_holder_verdict` decides.

    Exit 0 and the holder line when a loop is live; exit 1 and the reason when none is. It writes only under
    `--lend-routine` and `--recall-routine`, through `_chair_attach_lend`."""
    lend, recall = getattr(a, "lend_routine", False), getattr(a, "recall_routine", False)
    if lend or recall:
        return _chair_attach_lend(a, bool(lend), clock)
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    try:
        row = (read_row or _chair_lease_row)(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError, OSError, ValueError) as exc:  # an unreachable Postgres, psycopg absent, a bad store
        print(f"no live chair loop: store unreadable ({type(exc).__name__}: {' '.join(str(exc).split())})")
        return 1
    held = None if row is None or not row.get("holder") else _attach_inputs(row)
    if held is None:
        print("no live chair loop: " + ("no lease" if row is None or not row.get("holder") else "holder unreadable"))
        return 1
    lease, last_tick = held
    this_host = socket.gethostname()
    alive = (pid_alive or chair.pid_alive)(lease.pid) if lease.host == this_host else None
    if not lease.is_loop and alive and _is_chair_run_command((pid_command or _pid_command)(lease.pid)):
        lease = dataclasses.replace(lease, is_loop=True)  # a loop whose tick never landed: the process says what it is, not the label
    verdict = chair_holder.chair_holder_verdict(
        lease, this_host, (clock or (lambda: datetime.datetime.now(datetime.UTC)))(), last_tick, alive, 3 * _CHAIR_RUN_INTERVAL
    )
    print(chair_holder.format_holder_verdict(verdict))
    return 1 if verdict.kind == "none" else 0


def _watchdog_paths(platform: str, home: Path) -> list[Path]:
    """The files a watchdog timer lives in on this platform."""
    if platform == "darwin":
        return [home / "Library" / "LaunchAgents" / chair_service.WATCHDOG_PLIST_NAME]
    unit_dir = chair_service.unit_path(home).parent
    return [unit_dir / chair_service.WATCHDOG_SERVICE_NAME, unit_dir / chair_service.WATCHDOG_TIMER_NAME]


def _watchdog_install_plan(platform: str, home: Path, resolved: _ChairUnitInputs, minutes: int) -> list[tuple[Path, str]]:
    """Each file to write, in `_watchdog_paths` order, with its text."""
    if platform == "darwin":
        env_file = resolved.env_file or unit_env.env_file_path(str(home))
        texts = [chair_service.watchdog_plist_text(resolved.cox, resolved.profile, resolved.path, minutes, env_file)]
    else:
        texts = [
            chair_service.watchdog_service_text(resolved.cox, resolved.profile, resolved.path, resolved.env_file),
            chair_service.watchdog_timer_text(minutes),
        ]
    return list(zip(_watchdog_paths(platform, home), texts, strict=True))


def _watchdog_activation_lines(platform: str, plist: Path) -> list[str]:
    """Text for the user to run; nothing here is executed."""
    if platform == "darwin":
        return [f"launchctl bootstrap gui/$(id -u) {plist}"]
    return ["systemctl --user daemon-reload", f"systemctl --user enable --now {chair_service.WATCHDOG_TIMER_NAME}"]


def _chair_watchdog_install(a: argparse.Namespace) -> int:
    """Edge. Writes the watchdog timer files and prints the lines that activate them; runs no launchd or systemd tool."""
    if a.interval_minutes < 1:
        print("refusing: --interval-minutes must be at least 1")
        return 2
    resolved = _chair_unit_inputs(None, _chair_default_environment_file(), _profile_path(a))
    if isinstance(resolved, _ChairUnitRefusal):
        print(resolved.message)
        return 2
    plan = _watchdog_install_plan(sys.platform, Path.home(), resolved, a.interval_minutes)
    for path, text in plan:
        _write_private(path, text)
        print(path)
    print("\n".join(_watchdog_activation_lines(sys.platform, plan[0][0])))
    return 0


def _chair_watchdog_uninstall() -> int:
    """Edge. Removes the watchdog timer files that exist; runs no launchd or systemd tool."""
    # The platform's own paths, so uninstall undoes exactly what install wrote on this machine.
    present = [p for p in _watchdog_paths(sys.platform, Path.home()) if p.exists()]
    for path in present:
        path.unlink()
        print(f"removed {path}")
    if not present:
        print("no watchdog timer installed")
    return 0


def _chair_watchdog(a: argparse.Namespace) -> int:
    """Edge. One check of the chair lease and status rows; the notify state file is the one `chair run` uses."""
    if a.install:
        return _chair_watchdog_install(a)
    if a.uninstall:
        return _chair_watchdog_uninstall()
    profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    return chair_watchdog.run_watchdog(
        runs_dir, notify_config=notify_core.config_from_profile(profile), state_path=runs_dir.parent / "notify-sent.json",
        idle_minutes=a.idle_minutes, stale_minutes=a.stale_minutes, dry_run=a.dry_run,
    )


def _chair_freeze_lands(a: argparse.Namespace) -> int:
    """Edge. Prints the entry's text; exit 1 when that text is the usage line."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    text = chair_land_freeze.run_freeze(runs_dir, datetime.datetime.now(datetime.UTC), a.minutes, a.reason, a.off)
    print(text)
    return 1 if text.endswith(chair_land_freeze.USAGE) else 0


def _lander_once(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli.cmd_once(a)


def _lander_run(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli.cmd_run(a)


def _lander_status(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli.cmd_status(a)


def _lander_adopt(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli.cmd_adopt(a)


def _lander_backlog(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli.cmd_backlog(a)


def _lander_service(a: argparse.Namespace) -> int:
    from agent_tools import lander_cli
    return lander_cli.cmd_service(a)


def _worker_run(a: argparse.Namespace) -> int:
    from agent_tools import worker_cli
    return worker_cli.cmd_run(a)


def _worker_once(a: argparse.Namespace) -> int:
    from agent_tools import worker_cli
    return worker_cli.cmd_once(a)


def _worker_status(a: argparse.Namespace) -> int:
    from agent_tools import worker_cli
    return worker_cli.cmd_status(a)
