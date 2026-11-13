import fcntl
import os
from pathlib import Path
from typing import IO

Stamp = tuple[int, int]
STALE_INTERVALS = 3
_LOCK = "feed.lock"
_SNAPSHOT = "feed.snapshot"


def try_own(dir: Path) -> IO[str] | None:
    """Take the exclusive advisory lock; the returned handle is ownership, closing it gives it up."""
    dir.mkdir(parents=True, exist_ok=True)
    handle = open(dir / _LOCK, "a")  # noqa: SIM115 - the open file is the lock and outlives this call
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def release(handle: IO[str]) -> None:
    handle.close()


def publish(dir: Path, line: str) -> None:
    """Replace the snapshot whole, so a reader sees the old line or the new one, never part of one."""
    tmp = dir / f"{_SNAPSHOT}.{os.getpid()}.tmp"
    tmp.write_text(line)
    os.replace(tmp, dir / _SNAPSHOT)


def read_if_changed(dir: Path, last_stamp: Stamp | None) -> tuple[str | None, Stamp | None]:
    """One stat when nothing changed; the stamp is (mtime_ns, size) of the file that was read."""
    path = dir / _SNAPSHOT
    try:
        seen = path.stat()
        if (seen.st_mtime_ns, seen.st_size) == last_stamp:
            return None, last_stamp
        with open(path) as f:
            opened = os.fstat(f.fileno())
            text = f.read()
    except FileNotFoundError:
        return None, last_stamp
    return text, (opened.st_mtime_ns, opened.st_size)


def is_stale(mtime_ns: int, now_ns: int, interval_s: float) -> bool:
    """True once the snapshot is older than STALE_INTERVALS intervals; exactly that old is not stale."""
    return now_ns - mtime_ns > STALE_INTERVALS * interval_s * 1_000_000_000


def follow_step(
    dir: Path, last_stamp: Stamp | None, handle: IO[str] | None, now_ns: int, interval_s: float
) -> tuple[str | None, Stamp | None, IO[str] | None]:
    """One pass: an owner keeps its handle and prints nothing, a follower tries the lock then reads."""
    if handle is not None:
        return None, last_stamp, handle
    won = try_own(dir)
    if won is not None:
        return None, last_stamp, won
    line, stamp = read_if_changed(dir, last_stamp)
    if stamp is None or is_stale(stamp[0], now_ns, interval_s):
        return line, stamp, try_own(dir)
    return line, stamp, None
