"""`cox chair service --install --queues` and `--uninstall --queues`: the queue workers' units in for the tick's, and
back out (C6). The text and the plans are `chair_queue_units`'; this module writes the files and runs the steps.

Unlike `cox chair service --install` for the tick's unit, which only writes its file and prints the lines to run, this
one runs them: the cutover (§5.4 step 3) is one command, and the rollback (§5.5) is one too. `--dry-run` prints what it
would write and run and touches nothing.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agent_tools.cli_lazy import bounded_run, chair_queue_units, chair_service

if TYPE_CHECKING:
    from agent_tools.chair_queue_units import Step, UnitInputs

__all__ = [
    "chair_parity", "chair_service_queues", "chair_status", "launchd_loaded", "queue_unit_names", "run_steps", "store_identity",
]

_QU_STEP_SECONDS = 60.0  # a systemctl or launchctl call that has not answered by then has failed

Runner = Callable[[Sequence[str]], tuple[int, str]]


def _qu_run(argv: Sequence[str]) -> tuple[int, str]:
    """Edge. One systemctl or launchctl call, bounded; a hung one is killed and fails."""
    result = bounded_run.run(list(argv), _QU_STEP_SECONDS)
    if result.timed_out or result.returncode == bounded_run.UNRUNNABLE_RETURNCODE:
        return result.returncode, bounded_run.failure_line(list(argv), result, _QU_STEP_SECONDS)
    return result.returncode, result.output


def run_steps(steps: Sequence[Step], run: Runner = _qu_run, echo: Callable[[str], None] = print) -> int:
    """Edge. Each step in order, printed as it runs. A failed step stops the rest, unless it is marked `may_fail` (it
    undoes something that may not be there); the steps left are printed for a person to run. 0 when all went."""
    for i, step in enumerate(steps):
        echo(f"$ {shlex.join(step.argv)}")
        code, out = run(step.argv)
        if code == 0:
            continue
        detail = out.strip().splitlines()[0] if out.strip() else ""
        if step.may_fail:
            echo(f"  (exit {code}, nothing to undo: {detail})" if detail else f"  (exit {code}, nothing to undo)")
            continue
        echo(f"  exit {code}{': ' + detail if detail else ''}")
        for left in steps[i + 1:]:
            echo(f"{shlex.join(left.argv)}  # not run")
        return 1
    return 0


def launchd_loaded(labels: Sequence[str], uid: int, run: Runner = _qu_run) -> dict[str, bool]:
    """Edge. Which of `labels` launchd has loaded in the user's GUI domain: `launchctl print` exits 0 for a loaded one."""
    return {label: run(("launchctl", "print", f"gui/{uid}/{label}"))[0] == 0 for label in labels}


def queue_unit_names(home: Path) -> list[str]:
    """Edge. The queue workers' systemd units this machine has installed: the five instances while the template is
    there, else none. The edge upgrade restarts these with the tick's and the lander's, and its doctor's `units start`
    row judges them (`cli_install._edge_units`)."""
    return chair_queue_units.instance_names() if chair_queue_units.template_path(home).exists() else []


def _qu_inputs(a: argparse.Namespace) -> UnitInputs | str:
    """Edge. The values the units are built from, resolved as the tick's unit's are; else why they cannot be."""
    from agent_tools.cli_chair_service import (
        _chair_service_environment_file,
        _chair_unit_inputs,
        _ChairUnitRefusal,
    )
    from agent_tools.cli_common import _profile_path
    from agent_tools.cli_runs_fetch import _holder_label

    label = _holder_label(a)
    resolved = _chair_unit_inputs(label, _chair_service_environment_file(a), _profile_path(a))
    if isinstance(resolved, _ChairUnitRefusal):
        return resolved.message
    return chair_queue_units.UnitInputs(resolved.cox, resolved.workspace_dir, label, resolved.profile, resolved.path,
                                        resolved.env_file)


def _qu_queued(a: argparse.Namespace) -> frozenset[str] | str:
    from agent_tools import chair_read_queues, route
    from agent_tools.cli_common import _profile_path

    read = chair_read_queues.read_chair_queues(_profile_path(a))
    return f"routing: profile unreadable: {read}" if isinstance(read, route.ProfileError) else read


def _qu_labels() -> list[str]:
    return [chair_service.CHAIR_PLIST_LABEL, *(chair_queue_units.plist_label(k) for k in chair_queue_units.QUEUE_KINDS)]


def _qu_install(a: argparse.Namespace, platform: str, home: Path, uid: int, run: Runner) -> int:
    queued = _qu_queued(a)
    if isinstance(queued, str):
        print(queued)
        return 2
    refusal = chair_queue_units.install_refusal(platform, queued)
    if refusal:
        print(f"chair service: refusing, nothing written: {refusal}")
        return 2
    installed, active = _qu_watchdog(platform, home, uid, run)
    refusal = chair_queue_units.watchdog_refusal(platform, installed, active)
    if refusal:
        print(f"chair service: refusing, nothing written: {refusal}")
        return 2
    inputs = _qu_inputs(a)
    if isinstance(inputs, str):
        print(inputs)
        return 2
    files = chair_queue_units.queue_files(platform, home, inputs)
    changed = [path for path, text in files if not path.is_file() or path.read_text(encoding="utf-8") != text]
    tick_unit = chair_service.service_file(platform, home)
    loaded = launchd_loaded(_qu_labels(), uid, run) if platform == "darwin" else {}
    steps = chair_queue_units.install_steps(
        platform, home, changed, tick_unit is not None and tick_unit.exists(), loaded, uid,
    )
    if a.dry_run:
        for path, _text in files:
            print(f"would write {path}" if path in changed else f"unchanged {path}")
        print("\n".join(f"would run: {shlex.join(step.argv)}" for step in steps))
        return 0
    from agent_tools.cli_chair_service import _write_private

    for path, text in files:
        if path in changed:
            _write_private(path, text)
            print(f"wrote {path}")
        else:
            print(f"unchanged {path}")
    code = run_steps(steps, run)
    if code == 0:
        print("chair service: the five queue workers are installed and started, and the tick's unit is disabled; "
              "check with `cox chair status`")
        return code
    restore = chair_queue_units.restore_steps(platform, home, tick_unit is not None and tick_unit.exists(), uid)
    if not restore:
        print("chair service: the install failed partway and there is no tick unit to put back; "
              "check with `cox chair status`")
        return code
    print("chair service: the install failed partway; putting the tick's unit back, so something runs")
    back = run_steps(restore, run)
    print("chair service: the tick's unit is back. The workers that started are still running; fix the failure and run "
          "--install --queues again, or roll back (chair.queues: [], then --uninstall --queues)"
          if back == 0 else "chair service: the tick's unit could not be put back either: start it by hand (above)")
    return code


def _qu_watchdog(platform: str, home: Path, uid: int, run: Runner) -> tuple[bool, bool]:
    """Edge, read-only. Whether the chair watchdog timer's files are installed, and whether the service manager runs
    it: `systemctl --user is-active` on its timer, or `launchctl print` on its agent."""
    from agent_tools.cli_chair_service import _watchdog_paths

    installed = all(path.exists() for path in _watchdog_paths(platform, home))
    if platform == "darwin":
        return installed, launchd_loaded([chair_service.WATCHDOG_PLIST_LABEL], uid, run)[chair_service.WATCHDOG_PLIST_LABEL]
    return installed, run(("systemctl", "--user", "is-active", chair_service.WATCHDOG_TIMER_NAME))[0] == 0


def _qu_uninstall(a: argparse.Namespace, platform: str, home: Path, uid: int, run: Runner) -> int:
    queued = _qu_queued(a)
    if isinstance(queued, str):
        print(queued)
        return 2
    refusal = chair_queue_units.uninstall_refusal(platform, queued)
    if refusal:
        print(f"chair service: refusing, nothing changed: {refusal}")
        return 2
    # Every platform's queue files, so a file left by an install on the other path is never kept by mistake.
    present = [p for p in (chair_queue_units.template_path(home),
                           *(chair_queue_units.plist_path(home, k) for k in chair_queue_units.QUEUE_KINDS)) if p.exists()]
    tick_unit = chair_service.service_file(platform, home)
    loaded = launchd_loaded(_qu_labels(), uid, run) if platform == "darwin" else {}
    stop, start = chair_queue_units.uninstall_steps(
        platform, home, bool(present), tick_unit is not None and tick_unit.exists(), loaded, uid,
    )
    if a.dry_run:
        print("\n".join(f"would run: {shlex.join(step.argv)}" for step in stop))
        print("\n".join(f"would remove {path}" for path in present))
        print("\n".join(f"would run: {shlex.join(step.argv)}" for step in start))
        return 0
    if run_steps(stop, run) != 0:
        return 1
    for path in present:
        path.unlink()
        print(f"removed {path}")
    code = run_steps(start, run)
    if code == 0:
        started = "started again" if tick_unit is not None and tick_unit.exists() else "not installed, so not started"
        print(f"chair service: the queue workers are stopped and removed; the tick's unit is {started}")
    return code


def chair_service_queues(a: argparse.Namespace, run: Runner = _qu_run) -> int:
    """Edge. `--install --queues` or `--uninstall --queues` (`chair_queue_units`), on this machine's platform."""
    platform, home, uid = sys.platform, Path.home(), os.getuid()
    if a.install:
        return _qu_install(a, platform, home, uid, run)
    return _qu_uninstall(a, platform, home, uid, run)


_QU_LEASES_SQL = "SELECT name, holder, epoch, heartbeat_at, expires_at FROM leases WHERE name = {p} OR name LIKE {p}"


def _qu_lease_rows(runs_dir: Path) -> tuple[list, str] | str:
    """Edge, read-only. The chair lease and the kinds' worker leases, with the store's clock; else why not."""
    from agent_tools import chair_read_queues, run_store
    from agent_tools.chair import LEASE_NAME
    from agent_tools.chair_kinds import LEASE_PREFIX
    from agent_tools.chair_status import LeaseRow

    try:
        opened = run_store.open_store(runs_dir)
        if opened is None:
            return f"no store under {runs_dir}"
        conn, token = opened
        try:
            raw = conn.execute(run_store.sql_for(_QU_LEASES_SQL, token), (LEASE_NAME, f"{LEASE_PREFIX}%")).fetchall()
            now = conn.execute(chair_read_queues.NOW_SQL[token]).fetchone()["now"]
        finally:
            conn.close()
    except Exception as exc:  # the status is a read; any failure is its answer
        return f"cannot read the store: {type(exc).__name__}: {exc}"
    rows = [LeaseRow(str(r["name"]), str(r["holder"] or ""), int(r["epoch"] or 0), str(r["heartbeat_at"] or ""),
                     str(r["expires_at"] or "")) for r in raw]
    return rows, chair_read_queues.store_stamp(now)


def _qu_unit_states(platform: str, home: Path, run: Runner) -> dict[str, str] | None:
    """Edge. What the service manager says of the tick's unit and the queue workers' units that are installed: the
    systemd state, or on macOS whether the agent is loaded. None on a platform with neither."""
    if platform == "linux":
        names = [n for n in (chair_service.UNIT_NAME,) if chair_service.unit_path(home).exists()] + queue_unit_names(home)
        if not names:
            return {}
        _code, out = run(("systemctl", "--user", "is-active", *names))
        states = [line.strip() for line in out.splitlines() if line.strip()]
        return dict(zip(names, [*states, *[""] * len(names)], strict=False))
    if platform == "darwin":
        uid = os.getuid()
        labels = [chair_service.CHAIR_PLIST_LABEL] if chair_service.chair_plist_path(home).exists() else []
        labels += [chair_queue_units.plist_label(k) for k in chair_queue_units.QUEUE_KINDS
                   if chair_queue_units.plist_path(home, k).exists()]
        return {label: ("loaded" if up else "not loaded") for label, up in launchd_loaded(labels, uid, run).items()}
    return None


def chair_status(a: argparse.Namespace, run: Runner = _qu_run) -> int:
    """Edge. `cox chair status` (`chair_status`): exit 0 when every kind `chair.queues` lists has a live worker, 1 when
    one does not, 2 when the store or the profile cannot be read."""
    from datetime import UTC, datetime

    from agent_tools import chair_status as status
    from agent_tools.chair import LEASE_NAME
    from agent_tools.chair_kinds import LEASE_PREFIX
    from agent_tools.cli_runs import _leader_runs_dir_or_refuse

    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None or runs_dir is None:
        return refuse_rc if refuse_rc is not None else 2
    queued = _qu_queued(a)
    if isinstance(queued, str):
        print(queued)
        return 2
    read = _qu_lease_rows(runs_dir)
    if isinstance(read, str):
        print(f"chair status: {read}")
        return 2
    rows, stamp = read
    now = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    chair_row = next((r for r in rows if r.name == LEASE_NAME), None)
    kind_rows = [r for r in rows if r.name != LEASE_NAME]
    print("\n".join(status.status_lines(queued, chair_row, kind_rows, now, _qu_unit_states(sys.platform, Path.home(), run))))
    by_kind = {r.name.removeprefix(LEASE_PREFIX): status.kind_standing(r, now) for r in kind_rows}
    return 0 if status.all_live(queued, by_kind) else 1


def _chair_status(a: argparse.Namespace) -> int:
    return chair_status(a)


CoxRunner = Callable[[Sequence[str], dict[str, str], float], tuple[int, str]]


def _qu_run_cox(argv: Sequence[str], env: dict[str, str], timeout: float) -> tuple[int, str]:
    """Edge. One dry run of `cox`, bounded, with its stdout and stderr together."""
    import subprocess

    try:
        done = subprocess.run(list(argv), env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return bounded_run.TIMEOUT_RETURNCODE, f"{shlex.join(argv)} timed out after {timeout:.0f}s"
    except OSError as exc:
        return bounded_run.UNRUNNABLE_RETURNCODE, str(exc)
    return done.returncode, done.stdout + done.stderr


_IDENTITY_SQL = "SELECT current_database() AS db, (SELECT system_identifier FROM pg_control_system()) AS sysid"


def store_identity(url: str) -> tuple[str, ...] | str:
    """Edge, read-only. What store `url` is (`chair_parity.StoreIdentity`): on Postgres the server's system identifier
    and the database, read over a read-only connection; on sqlite the resolved file path. Else why it could not say."""
    from agent_tools import store_dialect

    if not store_dialect.is_postgres(url):
        return ("sqlite", str(Path(url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url).expanduser().resolve()))
    try:
        conn = store_dialect.connect_readonly_url(url)
        try:
            row = cast("Mapping[str, object] | None", conn.execute(_IDENTITY_SQL).fetchone())  # a dict_row connection
        finally:
            conn.close()
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"
    if row is None:
        return "the store returned no identity row"
    return ("postgres", str(row["sysid"]), str(row["db"]))


def chair_parity(a: argparse.Namespace, run_cox: CoxRunner = _qu_run_cox) -> int:
    """Edge. `cox chair parity --store-url URL` (`chair_parity`): every dry run against `URL` through a copy of the
    routing profile and its provider profile in a scratch directory, with a scratch workspace unless `--workspace` names
    one (so no read cache of the throwaway store lands in the live workspace's runs directory)."""
    import shutil
    import tempfile

    import yaml

    from agent_tools import chair_parity as parity
    from agent_tools import run_store
    from agent_tools.chair_kinds import QUEUE_KINDS
    from agent_tools.cli_common import _profile_path
    from agent_tools.cli_runs import _leader_runs_dir_or_refuse
    from agent_tools.store_url import read_provider_profile

    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None or runs_dir is None:
        return refuse_rc if refuse_rc is not None else 2
    refusal = parity.store_refusal(store_identity(run_store.store_url_for(runs_dir)), store_identity(a.store_url))
    given = Path(a.workspace).expanduser().resolve() if getattr(a, "workspace", None) else None
    refusal = refusal or parity.workspace_refusal(runs_dir.parent.resolve(), given)
    if refusal:
        print(f"chair parity: refusing: {refusal}")
        return 2
    cox = shutil.which("cox")
    if cox is None:
        print("chair parity: cox is not on PATH")
        return 2
    routing_text = _profile_path(a).read_text(encoding="utf-8")
    routing = yaml.safe_load(routing_text) or {}
    named = routing.get("provider_profile")
    provider = dict(read_provider_profile(named)) if isinstance(named, str) and named else {}
    with tempfile.TemporaryDirectory(prefix="cox-parity-") as scratch:
        root = Path(scratch)
        workspace = Path(a.workspace).expanduser() if getattr(a, "workspace", None) else root / "workspace"
        (workspace / "runs").mkdir(parents=True, exist_ok=True)
        (root / "provider.yaml").write_text(yaml.safe_dump({**provider, "storage_url": a.store_url}), encoding="utf-8")
        profile = root / "profile.yaml"
        env = {**os.environ, "AGENT_TOOLS_PROFILE": str(profile)}

        def listed(kinds: Sequence[str]) -> None:
            text = parity.scratch_profile(routing_text, str(root / "provider.yaml"), str(workspace), kinds)
            profile.write_text(text, encoding="utf-8")

        def dry(argv: list[str]) -> tuple[int, str]:
            print(f"$ {shlex.join(argv)}")
            return run_cox([cox, *argv, "--profile", str(profile)], env, a.timeout)

        listed([])
        tick = ["chair", "run", "--once", "--dry-run", "--as-holder"]
        alone_out = dry(tick)[1]
        listed(QUEUE_KINDS)
        listed_out = dry(tick)[1]
        worker_out = {kind: dry(["chair", "worker", "--kind", kind, "--once", "--dry-run"])[1] for kind in QUEUE_KINDS}
    alone, listed_reading = parity.tick_reading(alone_out), parity.tick_reading(listed_out)
    workers = {kind: parity.worker_reading(kind, out) for kind, out in worker_out.items()}
    unread = [name for name, r in (("tick alone", alone), ("tick listed", listed_reading), *workers.items()) if r is None]
    if alone is None or listed_reading is None or unread:
        print(f"chair parity: no plan could be read from {', '.join(unread)}; their output:")
        for name, out in (("tick alone", alone_out), ("tick listed", listed_out), *worker_out.items()):
            if name in unread:
                print(f"--- {name}\n{out.strip()}")
        return 2
    read = {kind: r for kind, r in workers.items() if r is not None}
    verdict = parity.judge(alone, listed_reading, read)
    print("\n".join(parity.verdict_lines(alone, listed_reading, read, verdict)))
    return 0 if verdict.ok else 1


def _chair_parity(a: argparse.Namespace) -> int:
    return chair_parity(a)
