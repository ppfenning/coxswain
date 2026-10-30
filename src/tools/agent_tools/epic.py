"""Watch a detached harness run without spending a turn on it. Blocks in Python, returns facts."""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["alive", "launched_epoch", "live_runs", "log_ended", "proc_start_epoch", "process_alive", "ps_lstart_epoch", "reused", "run_alive", "run_live", "run_state", "summarize_log", "wait_lanes", "watch"]

_LINE = re.compile(r"^\s*(quarantined task|quarantined phase|approved but not landed|reused|epic |  usage)", re.M)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_start_epoch(stat_text: str, boot_epoch: float, clk_tck: int) -> float | None:
    """Pure: start time, epoch seconds, from a `/proc/<pid>/stat` line; comm may hold parens, so field 22 is counted after the last `)`."""
    tail = stat_text.rpartition(")")[2].split()
    try:
        return boot_epoch + int(tail[19]) / clk_tck
    except (IndexError, ValueError):
        return None


def ps_lstart_epoch(text: str) -> float | None:
    """Pure: epoch seconds from `ps -o lstart=` output, which is local time; None when empty or unparseable."""
    try:
        return time.mktime(time.strptime(text.strip(), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def reused(started_at: float, launched_at: float, slack: float = 2.0) -> bool:
    """Pure: a process that started after the run was launched (plus `slack` seconds for btime rounding) is a reused pid, not the run."""
    return started_at > launched_at + slack


def launched_epoch(text: str | None) -> float | None:
    """Pure: the `at` of a `<run>.launched.json` as epoch seconds, None when absent or unreadable."""
    try:
        return datetime.datetime.fromisoformat(json.loads(text or "")["at"]).timestamp()
    except (ValueError, KeyError, TypeError):
        return None


def log_ended(text: str) -> bool:
    """Pure: the log already carries the epic summary line."""
    return summarize_log(text)["summary"] is not None


def _ps_lstart(pid: int) -> str | None:
    """Edge. `ps -o lstart=` for the pid, C locale so day and month names stay English; None on any failure."""
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _start_epoch(pid: int, proc_root: str = "/proc", ps: Callable[[int], str | None] = _ps_lstart) -> float | None:
    """Edge. /proc first, then `ps` where /proc is absent (macOS). None when neither can say, so the caller falls back to the bare pid probe."""
    try:
        boot = next(float(l.split()[1]) for l in Path(f"{proc_root}/stat").read_text().splitlines() if l.startswith("btime "))
        return proc_start_epoch(Path(f"{proc_root}/{pid}/stat").read_text(), boot, os.sysconf("SC_CLK_TCK"))
    except (OSError, StopIteration, ValueError):
        text = ps(pid)
        return None if text is None else ps_lstart_epoch(text)


def run_alive(pid: int, launched_at: float | None, log_text: str | None) -> bool:
    """Edge. Alive: no summary line in the log, the pid answers, and the process did not start after the launch."""
    if log_text is not None and log_ended(log_text):
        return False
    if not alive(pid):
        return False
    started = _start_epoch(pid)
    # no launch time or no /proc: the pid probe above is all the evidence there is
    return started is None or launched_at is None or not reused(started, launched_at)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def run_live(pid: int | None, pidfile: Path | str, log: Path | str | None = None, now: str | None = None) -> bool:
    """Edge. The store lease for the run whose pidfile is `<run>.pid` when one exists: live only while this run holds it unexpired. With no lease row, `run_alive` on the pid, reading its `.launched.json` and `.log` beside it; pid <= 0 is never signalled and a pid too large for pid_t reads as dead. `now` is `YYYY-MM-DDTHH:MM:SSZ` UTC."""
    from agent_tools import run_store  # run_store imports epic at module level

    pidfile = Path(pidfile)
    row = run_store.lease(pidfile.parent, pidfile.stem)
    if row is not None:
        holder, expires_at, _heartbeat_at = row
        return holder == pidfile.stem and expires_at > (now or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return process_alive(pid, pidfile, log)


def process_alive(pid: int | None, pidfile: Path | str, log: Path | str | None = None) -> bool:
    """Edge. The process half of `run_live`, ignoring any lease: `run_alive` on the pid with its `.launched.json` and `.log` beside the pidfile. pid <= 0 is never signalled and a pid too large for pid_t reads as dead."""
    pidfile = Path(pidfile)
    if pid is None or pid <= 0:
        return False
    try:
        if not alive(pid):
            return False  # a dead pid needs no file reads; most pidfiles in a runs dir are long dead
    except OverflowError:
        return False
    logged_at = launched_epoch(_read(pidfile.with_suffix(".launched.json")))
    # the pidfile is written right after the spawn, so its mtime bounds the launch when no launched.json says
    launched_at = logged_at if logged_at is not None else _mtime(pidfile)
    log_text = _read(Path(log) if log else pidfile.with_suffix(".log"))
    try:
        return run_alive(pid, launched_at, log_text)
    except OverflowError:
        return False


def run_state(pid: int | None, pidfile: Path | str, log: Path | str | None = None, now: str | None = None) -> str:
    """Edge. `running`, `fenced` or `exited`: the process verdict of `process_alive` joined with the run's lease standing. A live process whose lease is lapsed or held by another run is fenced. `now` is `YYYY-MM-DDTHH:MM:SSZ` UTC."""
    from agent_tools import lease_standing, run_store  # run_store imports epic at module level

    pidfile = Path(pidfile)
    stamp = now or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    held = lease_standing.standing(run_store.lease(pidfile.parent, pidfile.stem), pidfile.stem, stamp)
    return lease_standing.status(process_alive(pid, pidfile, log), held)


def live_runs(runs_dir: Path | str, prefix: str) -> list[dict[str, Any]]:
    """Edge. One `{run, pid, state}` per `<prefix>-<digits>.pid` in `runs_dir` whose state is running or fenced, sorted by run."""
    pattern = re.compile(rf"^{re.escape(prefix)}-\d+$")
    out: list[dict[str, Any]] = []
    for pidfile in sorted(Path(runs_dir).glob(f"{prefix}-*.pid")):
        text = _read(pidfile)
        if not pattern.match(pidfile.stem) or text is None or not text.strip().isdigit():
            continue
        pid = int(text.strip())
        state = run_state(pid, pidfile)
        if state != "exited":
            out.append({"run": pidfile.stem, "pid": pid, "state": state})
    return out


def summarize_log(text: str) -> dict[str, Any]:
    """Pure: the lines of a run log that state an outcome."""
    lines = [l.strip() for l in text.splitlines() if _LINE.match(l)]
    return {
        "approved": [l for l in lines if l.startswith("approved but not landed")],
        "quarantined": [l for l in lines if l.startswith("quarantined")],
        "reused": [l for l in lines if l.startswith("reused")],
        "summary": next((l for l in lines if l.startswith("epic ")), None),
        "usage": next((l for l in lines if l.startswith("usage")), None),
    }


def wait_lanes(busy: list[str], is_live: Callable[[str], bool], now: Callable[[], float], sleep: Callable[[float], None],
               max_seconds: float, interval: float) -> tuple[list[str], list[str]]:
    """Poll `busy` until at least one run is not live or the cap passes. Returns (exited, still_busy); exited is empty on a timeout."""
    deadline = now() + max_seconds
    while True:
        exited = [r for r in busy if not is_live(r)]
        if exited or now() >= deadline:
            return exited, [r for r in busy if r not in exited]
        sleep(interval)


def watch(pidfile: Path | str, *, log: Path | str | None = None, max_seconds: float = 570, interval: float = 20) -> dict[str, Any]:
    """Wait for the pid in `pidfile` to exit, up to the cap. Returns its `run_state` (a fenced run is not finished), whether it finished, and the log's outcome lines."""
    pid = int(Path(pidfile).read_text().strip())
    deadline = time.monotonic() + max_seconds
    while run_state(pid, pidfile, log) != "exited" and time.monotonic() < deadline:
        time.sleep(interval)
    state = run_state(pid, pidfile, log)
    out: dict[str, Any] = {"pid": pid, "state": state, "finished": state == "exited"}
    if log and Path(log).exists():
        out.update(summarize_log(Path(log).read_text(encoding="utf-8", errors="replace")))
    return out
