"""Pool edge: real git repositories, a stub sync argv that counts its runs, a scripted clock."""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from harness import pool_edge
from harness.pool_edge import (
    INDEX_NAME,
    LOCK_NAME,
    Lease,
    PoolError,
    _update,
    acquire,
    next_slot,
    pick_reusable,
    release,
    trim,
)
from harness.warm_pool import PoolEntry, lock_digest

NAMES = ("uv.lock",)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "uv.lock").write_text("lock v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _counter_argv(counter: Path) -> list[str]:
    return [sys.executable, "-c", f"open({str(counter)!r}, 'a').write('sync\\n')"]


def _syncs(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def _clock(*readings: float) -> Callable[[], float]:
    queue = list(readings)
    return lambda: queue.pop(0)


def _ticker() -> Callable[[], float]:
    state = {"now": 0.0}

    def tick() -> float:
        state["now"] += 1.0
        return state["now"]

    return tick


def _entry(slot: str, last_used: float, leased: bool = False) -> PoolEntry:
    return PoolEntry(path=f"/pool/{slot}", slot=slot, synced_digest="d", last_used=last_used, leased=leased)


class _Host:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = _repo(tmp_path)
        self.pool = tmp_path / "pool"
        self.counter = tmp_path / "counter"
        self.clock = _ticker()

    def acquire(self, branch: str, **kw: object) -> Lease:
        args: dict[str, object] = {
            "branch": branch,
            "base": "main",
            "sync_argv": _counter_argv(self.counter),
            "lock_names": NAMES,
            "keep": 2,
            "clock": self.clock,
        }
        return acquire(self.repo, self.pool, **{**args, **kw})  # type: ignore[arg-type]

    def release(self, lease: Lease) -> None:
        release(lease, lock_digest((lease.path / "uv.lock").read_bytes()))

    def index(self) -> list[dict[str, object]]:
        return json.loads((self.pool / "repo" / INDEX_NAME).read_text())["entries"]


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def test_next_slot_takes_the_first_gap() -> None:
    assert next_slot([_entry("slot-1", 0), _entry("slot-3", 0)]) == "slot-2"
    assert next_slot([]) == "slot-1"


def test_pick_reusable_prefers_the_latest_idle_then_a_stale_lease() -> None:
    entries = [_entry("slot-1", 5), _entry("slot-2", 9), _entry("slot-3", 1, leased=True)]
    assert pick_reusable(entries, now=100, stale_age=10) == entries[1]
    assert pick_reusable(entries[2:], now=100, stale_age=10) == entries[2]
    assert pick_reusable(entries[2:], now=5, stale_age=10) is None


def test_first_acquire_is_cold_and_syncs_once(host: _Host) -> None:
    lease = host.acquire("lane/a")
    assert (lease.mode, lease.slot, lease.path) == ("cold", "slot-1", host.pool / "repo" / "slot-1")
    assert (lease.path / "uv.lock").read_text() == "lock v1\n"
    assert _syncs(host.counter) == 1
    assert [(e["slot"], e["leased"]) for e in host.index()] == [("slot-1", True)]


def test_release_then_acquire_is_warm_without_a_second_sync(host: _Host) -> None:
    first = host.acquire("lane/a")
    host.release(first)
    second = host.acquire("lane/b")
    assert (second.mode, second.slot, second.path) == ("warm", "slot-1", first.path)
    assert _syncs(host.counter) == 1
    assert (
        subprocess.run(
            ["git", "-C", str(second.path), "branch", "--show-current"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "lane/b"
    )


def test_changed_lockfile_forces_exactly_one_more_sync(host: _Host) -> None:
    host.release(host.acquire("lane/a"))
    host.release(host.acquire("lane/b"))
    assert _syncs(host.counter) == 1
    (host.repo / "uv.lock").write_text("lock v2\n")
    _git(host.repo, "commit", "-qam", "bump lock")
    third = host.acquire("lane/c")
    assert (third.mode, _syncs(host.counter)) == ("warm", 2)
    host.release(third)
    host.acquire("lane/d")
    assert _syncs(host.counter) == 2


def test_overlapping_acquires_get_distinct_slots(host: _Host) -> None:
    one, two = host.acquire("lane/a"), host.acquire("lane/b")
    assert {one.slot, two.slot} == {"slot-1", "slot-2"}
    assert (one.mode, two.mode) == ("cold", "cold")
    assert one.path != two.path


def test_a_full_pool_with_fresh_leases_refuses(host: _Host) -> None:
    host.acquire("lane/a", keep=1)
    with pytest.raises(PoolError, match="full"):
        host.acquire("lane/b", keep=1)


def test_setup_seconds_is_the_difference_of_the_two_clock_readings(host: _Host) -> None:
    lease = host.acquire("lane/a", clock=_clock(10.0, 12.5))
    assert lease.setup_seconds == 2.5


def test_release_records_the_digest_and_the_clock(host: _Host) -> None:
    lease = host.acquire("lane/a", clock=_clock(1.0, 2.0, 7.0))
    release(lease, "abc")
    (entry,) = host.index()
    assert (entry["leased"], entry["synced_digest"], entry["last_used"]) == (False, "abc", 7.0)


def test_a_vanished_directory_drops_its_entry_and_falls_through_to_cold(host: _Host) -> None:
    first = host.acquire("lane/a")
    host.release(first)
    shutil.rmtree(first.path)
    second = host.acquire("lane/b")
    assert (second.mode, second.slot) == ("cold", "slot-1")
    assert _syncs(host.counter) == 2
    assert [e["slot"] for e in host.index()] == ["slot-1"]


def test_a_leased_entry_is_reclaimed_only_past_the_stale_age(host: _Host) -> None:
    host.acquire("lane/a", keep=1, clock=_clock(100.0, 101.0))
    with pytest.raises(PoolError, match="full"):
        host.acquire("lane/b", keep=1, stale_age=50.0, clock=_clock(120.0))
    reclaimed = host.acquire("lane/c", keep=1, stale_age=50.0, clock=_clock(200.0, 201.0))
    assert (reclaimed.mode, reclaimed.slot) == ("warm", "slot-1")
    # A crashed lease has no trusted environment, so it resyncs.
    assert _syncs(host.counter) == 2


def test_a_failed_cold_setup_leaves_nothing_registered(host: _Host) -> None:
    with pytest.raises(PoolError, match="cold setup"):
        host.acquire("lane/a", sync_argv=[sys.executable, "-c", "raise SystemExit(3)"])
    assert host.index() == []
    assert not (host.pool / "repo" / "slot-1").exists()


def test_trim_leaves_exactly_keep_idle_directories(host: _Host) -> None:
    leases = [host.acquire(f"lane/{n}", keep=4) for n in range(4)]
    for lease in leases:
        host.release(lease)
    busy = host.acquire("lane/busy", keep=4)
    removed = trim(host.repo, host.pool, 2, host.clock)
    # The busy lease took the most recent idle entry, leaving three idle; the oldest goes.
    assert (busy.slot, removed) == ("slot-4", ["slot-1"])
    idle = [e for e in host.index() if not e["leased"]]
    assert len(idle) == 2
    on_disk = sorted(p.name for p in (host.pool / "repo").iterdir() if p.is_dir())
    assert on_disk == sorted(str(e["slot"]) for e in host.index())
    assert busy.path.is_dir()
    listing = subprocess.run(
        ["git", "-C", str(host.repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert listing.count("worktree ") == 1 + len(host.index())


def test_trim_with_more_releases_than_keep_removes_the_oldest(host: _Host) -> None:
    leases = [host.acquire(f"lane/{n}", keep=3) for n in range(3)]
    for lease in leases:
        host.release(lease)
    assert trim(host.repo, host.pool, 1, host.clock) == ["slot-1", "slot-2"]
    assert [e["slot"] for e in host.index()] == ["slot-3"]
    assert [p.name for p in (host.pool / "repo").iterdir() if p.is_dir()] == ["slot-3"]


def _branches(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"], capture_output=True, text=True, check=True
    ).stdout
    return sorted(out.split())


def test_a_failed_cold_setup_deletes_its_branch_so_the_retry_succeeds(host: _Host) -> None:
    with pytest.raises(PoolError, match="cold setup"):
        host.acquire("lane/a", sync_argv=[sys.executable, "-c", "raise SystemExit(3)"])
    assert _branches(host.repo) == ["main"]
    assert host.acquire("lane/a").mode == "cold"


def test_a_cold_create_that_fails_on_an_existing_branch_keeps_that_branch(host: _Host) -> None:
    _git(host.repo, "branch", "lane/a")
    with pytest.raises(PoolError, match="already exists"):
        host.acquire("lane/a")
    assert _branches(host.repo) == ["lane/a", "main"]


def test_pick_reusable_prefers_the_slot_holding_the_branch() -> None:
    entries = [_entry("slot-1", 5), _entry("slot-2", 9)]
    assert pick_reusable(entries, now=100, stale_age=10, holder="slot-1") == entries[0]
    assert pick_reusable(entries, now=100, stale_age=10, holder=None) == entries[1]


def test_reacquiring_a_branch_reuses_the_slot_that_holds_it(host: _Host) -> None:
    a, b = host.acquire("lane/a"), host.acquire("lane/b")
    host.release(a)
    host.release(b)
    again = host.acquire("lane/a")
    assert (again.slot, again.mode) == (a.slot, "warm")


def test_a_reclaimed_lease_cannot_release_the_new_holder(host: _Host) -> None:
    old = host.acquire("lane/a", keep=1, clock=_clock(100.0, 101.0, 300.0))
    host.acquire("lane/b", keep=1, stale_age=50.0, clock=_clock(200.0, 201.0))
    assert release(old, "x") is False
    assert [(e["leased"], e["last_used"]) for e in host.index()] == [(True, 200.0)]


def test_update_serialises_concurrent_read_modify_write(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"

    def add(n: int) -> None:
        # The sleep sits between the read and the write, so only the lock keeps the updates apart.
        _update(pool_dir, lambda entries: ([*entries, _entry(f"slot-{n}", n)], time.sleep(0.02)))

    threads = [threading.Thread(target=add, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(e.slot for e in pool_edge._read_index(pool_dir)) == [f"slot-{n}" for n in range(6)]


def test_concurrent_acquires_get_distinct_slots(host: _Host) -> None:
    slots: list[str] = []
    threads = [
        threading.Thread(target=lambda n=n: slots.append(host.acquire(f"lane/{n}", keep=3).slot)) for n in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(slots) == ["slot-1", "slot-2", "slot-3"]


def test_trim_holds_no_lock_while_removing_and_retries_a_failed_removal(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    for lease in [host.acquire(f"lane/{n}", keep=2) for n in range(2)]:
        host.release(lease)
    real = pool_edge.remove_worktree

    def flaky(repo: Path, path: Path) -> tuple[bool, str]:
        with open(host.pool / "repo" / LOCK_NAME, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if trim still holds it
        return (False, "busy") if path.name == "slot-1" else real(repo, path)

    monkeypatch.setattr(pool_edge, "remove_worktree", flaky)
    assert trim(host.repo, host.pool, 0, host.clock) == ["slot-2"]
    assert [(e["slot"], e["leased"]) for e in host.index()] == [("slot-1", False)]
    monkeypatch.setattr(pool_edge, "remove_worktree", real)
    assert trim(host.repo, host.pool, 0, host.clock) == ["slot-1"]
    assert host.index() == []


def _two_released(host: _Host) -> tuple[Lease, Lease]:
    a, b = host.acquire("lane/a"), host.acquire("lane/b")
    host.release(a)
    host.release(b)
    return a, b


def test_a_slot_that_is_no_longer_a_worktree_is_discarded_and_the_next_candidate_serves(host: _Host) -> None:
    a, b = _two_released(host)
    shutil.rmtree(b.path)
    b.path.mkdir()  # the newest slot is now a plain directory, as a crash between mkdir and worktree add leaves
    lease = host.acquire("lane/c")
    assert (lease.slot, lease.mode) == (a.slot, "warm")
    assert [e["slot"] for e in host.index()] == [a.slot]
    assert not b.path.exists()


def test_a_broken_slot_with_no_other_candidate_falls_through_to_cold(host: _Host) -> None:
    a = host.acquire("lane/a", keep=1)
    host.release(a)
    shutil.rmtree(a.path)
    a.path.mkdir()
    lease = host.acquire("lane/b", keep=1)
    assert (lease.slot, lease.mode) == ("slot-1", "cold")
    assert (lease.path / "uv.lock").exists()


def test_a_failed_warm_sync_returns_the_slot_idle_with_its_old_last_used(host: _Host) -> None:
    _two_released(host)
    before = host.index()
    (host.repo / "uv.lock").write_text("lock v2\n")
    _git(host.repo, "commit", "-qam", "bump lock")
    with pytest.raises(PoolError, match="warm setup"):
        host.acquire("lane/c", sync_argv=[sys.executable, "-c", "raise SystemExit(3)"])
    after = host.index()
    newest = max(before, key=lambda e: e["last_used"])["slot"]
    changed = next(e for e in after if e["slot"] == newest)
    assert (changed["leased"], changed["synced_digest"]) == (False, None)
    assert [e["last_used"] for e in after] == [e["last_used"] for e in before]


def test_a_failed_warm_reset_on_a_healthy_worktree_keeps_the_slot(host: _Host) -> None:
    a, _ = _two_released(host)
    with pytest.raises(PoolError, match="warm setup"):
        host.acquire("lane/c", base="no-such-ref")
    assert sorted(e["slot"] for e in host.index()) == ["slot-1", "slot-2"]
    assert a.path.is_dir()


def test_a_lease_stamp_is_unique_per_slot_under_a_frozen_clock(host: _Host) -> None:
    first = host.acquire("lane/a", clock=lambda: 5.0)
    assert release(first, "x") is True
    second = host.acquire("lane/b", clock=lambda: 5.0)
    assert second.slot == first.slot
    assert release(first, "y") is False
    assert [e["leased"] for e in host.index()] == [True]


def test_trim_stamps_claimed_entries_so_they_cannot_be_reclaimed_as_stale(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_released(host)
    seen: list[list[dict[str, object]]] = []
    real = pool_edge.remove_worktree

    def spy(repo: Path, path: Path) -> tuple[bool, str]:
        seen.append(host.index())
        return real(repo, path)

    monkeypatch.setattr(pool_edge, "remove_worktree", spy)
    assert trim(host.repo, host.pool, 1, _clock(500.0)) == ["slot-1"]
    (claimed,) = [e for e in seen[0] if e["slot"] == "slot-1"]
    assert (claimed["leased"], claimed["last_used"]) == (True, 500.0)
    assert pick_reusable([PoolEntry(**{**claimed, "path": "/x"})], now=500.0, stale_age=10.0) is None  # type: ignore[arg-type]


def test_trim_deletes_a_merged_branch_and_keeps_one_with_unmerged_work(host: _Host) -> None:
    a, b = _two_released(host)
    _git(a.path, "commit", "--allow-empty", "-qm", "unmerged work")
    trim(host.repo, host.pool, 0, host.clock)
    assert _branches(host.repo) == ["lane/a", "main"]


_ACQUIRE_SCRIPT = """
import sys, time
from pathlib import Path
from harness.pool_edge import acquire
lease = acquire(Path(sys.argv[1]), Path(sys.argv[2]), branch=sys.argv[3], base="main",
                sync_argv=[sys.executable, "-c", "import time; time.sleep(0.3)"],
                lock_names=("uv.lock",), keep=4, clock=time.time)
print(lease.slot)
"""


def test_separate_processes_get_distinct_slots(host: _Host) -> None:
    graphs = Path(__file__).resolve().parents[1]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _ACQUIRE_SCRIPT, str(host.repo), str(host.pool), f"lane/p{n}"],
            cwd=graphs,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(3)
    ]
    outs = [p.communicate() for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    assert sorted(out.strip() for out, _ in outs) == ["slot-1", "slot-2", "slot-3"]


_UPDATE_SCRIPT = """
import sys, time
from pathlib import Path
from harness.pool_edge import _update
from harness.warm_pool import PoolEntry
n = sys.argv[2]
# The sleep sits between the read and the write, so only the lock keeps the processes apart.
_update(Path(sys.argv[1]), lambda es: ([*es, PoolEntry(f"/p/{n}", f"slot-{n}", None, 0.0, False)], time.sleep(0.2)))
"""


def test_separate_processes_serialise_their_index_updates(tmp_path: Path) -> None:
    graphs = Path(__file__).resolve().parents[1]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _UPDATE_SCRIPT, str(tmp_path / "pool"), str(n)],
            cwd=graphs,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(4)
    ]
    outs = [p.communicate() for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0], outs
    assert sorted(e.slot for e in pool_edge._read_index(tmp_path / "pool")) == [f"slot-{n}" for n in range(4)]
