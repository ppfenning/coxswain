import os
import re
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_tools import epic, run_store


@dataclass(frozen=True)
class Lease:
    holder: str
    expires_at: str  # YYYY-MM-DDTHH:MM:SSZ UTC, compared as a string like `epic.run_live`


def live_initiatives(leases: Mapping[str, Lease | None], pid_live: Mapping[str, bool], now: str) -> list[str]:
    """Pure. Live: the lease is held and unexpired at `now`, else the pid check says alive. Sorted."""
    by_lease = {name for name, lease in leases.items() if lease is not None and lease.holder and lease.expires_at > now}
    by_pid = {name for name, alive in pid_live.items() if alive}
    return sorted(by_lease | by_pid)


_RUN_NUMBER = re.compile(r"-\d+")
_PHASE = re.compile(r"([^.]+?)(?:-\d+)?")


def pidfile_index(names: Collection[str]) -> dict[str, list[str]]:
    """Pure. Initiative to its `<initiative>.pid` and `<initiative>-<N>.pid` entries among a runs directory's `names`,
    each sorted. Built once a read: matching every initiative against every entry cost seconds on a long history.
    A stem ending in `-<N>` is filed under itself too, since an initiative id may end in digits."""
    index: dict[str, set[str]] = {}
    for name in names:
        if not name.endswith(".pid"):
            continue
        stem = name.removesuffix(".pid")
        index.setdefault(stem, set()).add(name)
        base, dash, number = stem.rpartition("-")
        if dash and base and _RUN_NUMBER.fullmatch(f"-{number}"):
            index.setdefault(base, set()).add(name)
    return {initiative: sorted(found) for initiative, found in index.items()}


def phase_pidfile_index(names: Collection[str]) -> dict[str, list[tuple[str, str]]]:
    """Pure. Initiative to `(phase, name)` for its `<initiative>.<phase>.pid` and `<initiative>.<phase>-<N>.pid` entries
    among `names`, sorted; the phase has no dot."""
    index: dict[str, list[tuple[str, str]]] = {}
    for name in names:
        if not name.endswith(".pid"):
            continue
        initiative, dot, tail = name.removesuffix(".pid").rpartition(".")
        if dot and initiative and (m := _PHASE.fullmatch(tail)):
            index.setdefault(initiative, []).append((m.group(1), name))
    return {initiative: sorted(found) for initiative, found in index.items()}


def _pid_names(runs_dir: Path) -> list[str]:
    """Edge. The `.pid` entries of `runs_dir`, listed once: a glob per initiative re-read the whole directory each time,
    thousands of entries per initiative on a chair with a long run history."""
    try:
        return [name for name in os.listdir(runs_dir) if name.endswith(".pid")]
    except OSError:
        return []


def _pid_of(pidfile: Path) -> int | None:
    try:
        text = pidfile.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _run_live(pidfile: Path, now: str) -> bool:
    pid = _pid_of(pidfile)
    return pid is not None and epic.run_live(pid, pidfile, now=now)


def read_live_initiatives(runs_dir: Path, initiatives: Sequence[str], now: str) -> list[str]:
    """Edge. Reads each `runs:<initiative>` lease row and runs `epic.run_live`, the pid check `route status` uses, on its pidfiles. `now` is passed in."""
    rows = {name: run_store.lease(runs_dir, name) for name in initiatives}
    leases = {name: None if row is None else Lease(row[0], row[1]) for name, row in rows.items()}
    index = pidfile_index(_pid_names(runs_dir))
    pid_live = {name: any(_run_live(runs_dir / f, now) for f in index.get(name, [])) for name in initiatives}
    return live_initiatives(leases, pid_live, now)


def live_phases(leases: Mapping[tuple[str, str], Lease | None], pid_live: Mapping[tuple[str, str], bool], now: str) -> list[tuple[str, str]]:
    """Pure. Keys are `(initiative, phase)`. Live by the `live_initiatives` rule: a held, unexpired lease, else a live pid. Sorted."""
    by_lease = {key for key, lease in leases.items() if lease is not None and lease.holder and lease.expires_at > now}
    by_pid = {key for key, alive in pid_live.items() if alive}
    return sorted(by_lease | by_pid)


def _phase_pidfiles(runs_dir: Path, initiative: str) -> list[tuple[str, Path]]:
    """Edge. `(phase, path)` for `<initiative>.<phase>.pid` and `<initiative>.<phase>-<N>.pid`; the phase has no dot."""
    return [(phase, runs_dir / name) for phase, name in phase_pidfile_index(_pid_names(runs_dir)).get(initiative, [])]


def _phase_leases(runs_dir: Path, initiative: str) -> dict[str, Lease]:
    """Edge. The `runs:<initiative>.<phase>` lease rows by phase; the whole-initiative `runs:<initiative>` row never matches."""
    own = re.compile("runs:" + re.escape(initiative) + r"\.([^.]+)")
    table = run_store.lease_table_for(str(runs_dir), int(time.monotonic() // run_store.LEASE_SNAPSHOT_S))  # the cache window is the only clock read
    matches = ((own.fullmatch(name), row) for name, row in table.items())
    return {m.group(1): Lease(row[0], row[1]) for m, row in matches if m}


def read_live_phases(runs_dir: Path, initiatives: Sequence[str], now: str) -> list[tuple[str, str]]:
    """Edge. The live `(initiative, phase)` pairs of each initiative's phase leases and phase pidfiles. `now` is passed in."""
    leases = {(i, phase): lease for i in initiatives for phase, lease in _phase_leases(runs_dir, i).items()}
    index = phase_pidfile_index(_pid_names(runs_dir))
    pidfiles = [(i, phase, runs_dir / f) for i in initiatives for phase, f in index.get(i, [])]
    pid_live = dict.fromkeys({(i, phase) for i, phase, p in pidfiles if _run_live(p, now)}, True)
    return live_phases(leases, pid_live, now)
