"""What a chair process stops of its own children on its way out.

The chair's systemd units run with `KillMode=process` (`chair_service.KILL_MODE_LINE`): a stop signals the main
process only, so the lands, lanes and launches the chair started detached outlive it, as they do under launchd. What
the chair ran for itself must not: a check, a `git` or `gh` call, a revert's test run. So on every way out (a stop's
SIGTERM, a return, a hung pass's exit 4) the chair calls `stop_children`. It signals each direct child it did not
mark `detached`, with that child's process group when the child leads one, and the rest of its own process group;
after `GRACE_SECONDS` it kills whatever is left of them.

It works from a keep list, not a stop list: a spawn that should outlive the chair says so (`detached`), and anything
else is stopped. A new detached spawn that forgets to say so is stopped on exit, as every child was before the setting:
the old behaviour, never a leak. A worker killed outright (SIGKILL, the OOM killer) runs nothing, and what it ran is
left to its own timeouts."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

GRACE_SECONDS = 5.0
PS_TIMEOUT_SECONDS = 10.0

# Edge state: the pids this process started to outlive it.
_detached: set[int] = set()
_lock = threading.Lock()


def detached(pid: int) -> int:
    """Edge. Mark `pid`, a child started in its own session, as one `stop_children` leaves running; returns it."""
    with _lock:
        _detached.add(pid)
    return pid


def kept() -> frozenset[int]:
    with _lock:
        return frozenset(_detached)


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int


@dataclass(frozen=True)
class Target:
    """A process to stop: `group` when it leads its own process group, which is signalled whole."""

    pid: int
    group: bool


def parse_ps(text: str) -> list[Proc]:
    """Pure. `ps -A -o pid= -o ppid= -o pgid=` as rows; a line that is not three integers is dropped."""
    procs = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and all(f.isdigit() for f in fields):
            procs.append(Proc(int(fields[0]), int(fields[1]), int(fields[2])))
    return procs


def targets(procs: Iterable[Proc], me: int, my_pgid: int, keep: frozenset[int]) -> list[Target]:
    """Pure. What `me` stops on exit: each direct child not in `keep`, as its group when it leads one, then each other
    member of `me`'s own group (a grandchild whose parent already exited) when `me` leads that group. A kept child's
    group is never touched, and neither is `me`, nor a group `me` does not lead (a shell's job, when run by hand)."""
    rows = list(procs)
    kept_groups = {p.pgid for p in rows if p.pid in keep and p.pgid == p.pid}
    chosen: dict[int, Target] = {}
    for p in rows:
        if p.ppid == me and p.pid != me and p.pid not in keep and p.pgid not in kept_groups:
            chosen[p.pid] = Target(p.pid, p.pgid == p.pid and p.pgid != my_pgid)
    if my_pgid == me:
        for p in rows:
            if p.pgid == me and p.pid != me and p.pid not in keep and p.pid not in chosen:
                chosen[p.pid] = Target(p.pid, False)
    return sorted(chosen.values(), key=lambda t: t.pid)


def _ps() -> str:
    try:
        done = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid="], capture_output=True, text=True,
                              timeout=PS_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout


def _signal(target: Target, sig: int) -> bool:
    """Edge. Signal the target; False once it (or its whole group) is gone."""
    try:
        (os.killpg if target.group else os.kill)(target.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _alive(target: Target) -> bool:
    """Edge. A direct child that exited is reaped here, so it reads as gone rather than as a zombie."""
    with contextlib.suppress(ChildProcessError, OSError):
        pid, _ = os.waitpid(target.pid, os.WNOHANG)
        if pid == target.pid:
            return target.group and _signal(target, 0)
    return _signal(target, 0)


def _session_leader() -> bool:
    return os.getsid(0) == os.getpid()


def stop_children(
    grace: float = GRACE_SECONDS, ps: Callable[[], str] = _ps, log: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    leader: Callable[[], bool] = _session_leader,
) -> Sequence[Target]:
    """Edge. SIGTERM each target, wait up to `grace` for them to go, then SIGKILL the rest. Never raises; returns the
    targets signalled. `log` gets one line naming them, when there are any.

    Only a session leader does it, which is how systemd and launchd start a unit's process. Run by hand from a shell,
    the chair shares its process group with its pipeline (`| tee`) and its children with whatever started it (a test
    run), and a stop there is the shell's to make: so it stops nothing."""
    me = os.getpid()
    try:
        if not leader():
            return ()
        found = targets(parse_ps(ps()), me, os.getpgrp(), kept())
    except Exception:
        return ()
    found = [t for t in found if _signal(t, 0)]  # `ps` itself, and anything else that exited since it ran
    if not found:
        return ()
    if log is not None:
        log(f"chair: stopping {len(found)} child process(es) of pid {me} on exit: "
            + ", ".join(f"{t.pid}{' (group)' if t.group else ''}" for t in found))
    live = [t for t in found if _signal(t, signal.SIGTERM)]
    deadline = clock() + grace
    while live and clock() < deadline:
        sleep(0.1)
        live = [t for t in live if _alive(t)]
    for t in live:
        _signal(t, signal.SIGKILL)
    deadline = clock() + 1.0  # reap what was killed, so no zombie of it outlives this call
    while live and clock() < deadline:
        live = [t for t in live if _alive(t)]
        if live:
            sleep(0.05)
    return found
