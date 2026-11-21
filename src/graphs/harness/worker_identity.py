"""A worker's machine, so two machines can never share one worker name.

A worker's name defaults to the host name, and two machines left on a default host name would share one identity:
one `workers` row, one `claimed_by`, one `old_model_runs` host filter and L2's host-keyed smoke reuse. So the
`workers` row records the machine that wrote it (`machine_id`, 0023), and `store_phases.register` asks `takeover`
before it writes: a live row of the same name from another machine is refused, and the worker does not start. A row
whose machine stopped it, or whose beat has aged past `STALE_S`, is taken over, and the caller logs it. A running
worker beats its row every `BEAT_S` (`store_phases.beat_worker`), so a live one never looks stale. A row an older
build wrote (no machine recorded) has no such beat, so it is never taken over while it says `up`.

The machine is `/etc/machine-id` (or D-Bus's copy) on Linux and `IOPlatformUUID` from `ioreg` on macOS. Where
neither answers, a random id is kept in the worker's workspace (`FALLBACK_FILE`), stable for as long as that file is.
`takeover`, `parse_ioreg` and `pick` are pure; `machine_id` reads the machine.
"""

from __future__ import annotations

import re
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from harness import phase_pick

__all__ = ["BEAT_S", "STALE_S", "Verdict", "machine_id", "parse_ioreg", "pick", "takeover", "takeover_note"]

BEAT_S = 30  # how often a running worker beats its row
STALE_S = 900  # a row not beaten for this long (thirty beats) is long lapsed: its worker is gone
FALLBACK_FILE = "worker-machine-id"
LINUX_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")

# new: no row of the name. same: this machine's own row. stopped: its worker marked it stopping. stale: its beat is
# older than STALE_S. refused: another machine's live row (or one whose machine is not recorded, which may be).
Verdict = Literal["new", "same", "stopped", "stale", "refused"]
TAKES = frozenset({"new", "same", "stopped", "stale"})

_IOREG = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')


def takeover(found: Mapping[str, object] | None, machine: str, now: str, stale_s: int = STALE_S) -> Verdict:
    """Pure. Whether `machine` may write the row `found` (the `workers` row of its name, as stored) at the store's
    `now`. A row with no machine recorded (written before 0023, or by an older build) is never taken over while it
    says `up`, however old its beat: an older build writes `beat_at` only when it registers, so an old beat says
    nothing about whether that worker is still running. Only its own clean stop (`stopping`) frees it."""
    if found is None:
        return "new"
    prior = str(found.get("machine_id") or "")
    if machine and prior == machine:
        return "same"
    if found.get("state") != "up":
        return "stopped"
    if not prior:
        return "refused"
    beat = str(found.get("beat_at") or "")
    try:
        aged = phase_pick.seconds(beat, now) >= stale_s
    except ValueError:  # a row with no readable beat has nothing to say it is live
        aged = True
    return "stale" if aged else "refused"


def takeover_note(name: str, verdict: Verdict, found: Mapping[str, object] | None, machine: str) -> str:
    """Pure. What the worker says of its registration: why it was refused, or whose row it took over; "" when the
    row was new or its own."""
    prior = str((found or {}).get("machine_id") or "") or "an unrecorded machine"
    beat = str((found or {}).get("beat_at") or "never")
    if verdict == "refused" and not (found or {}).get("machine_id"):
        return (f"worker: {name} is registered as up by an older build that records no machine (beat {beat}), and "
                f"this machine ({machine}) cannot tell whether that worker still runs: stop it cleanly on its own "
                "machine (its row then says `stopping`), or give this worker a name of its own (`--name`)")
    if verdict == "refused":
        return (f"worker: {name} is registered and live from machine {prior} (beat {beat}), not this one ({machine}): "
                "give this worker a name of its own (`--name`), or stop the other one")
    if verdict in ("stopped", "stale"):
        how = "it was stopped" if verdict == "stopped" else f"its last beat was {beat}"
        return f"worker: {name} takes over the row machine {prior} left ({how})"
    return ""


def parse_ioreg(text: str) -> str:
    """Pure. The `IOPlatformUUID` in `ioreg -rd1 -c IOPlatformExpertDevice` output; "" when it has none."""
    found = _IOREG.search(text)
    return found.group(1).strip() if found else ""


def pick(candidates: Iterable[str]) -> str:
    """Pure. The first candidate id that says anything, stripped; "" when none does."""
    return next((c.strip() for c in candidates if c and c.strip()), "")


def _read(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _ioreg() -> str:
    try:
        done = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return parse_ioreg(done.stdout)


def _fallback(ws: Path) -> str:
    path = ws / FALLBACK_FILE
    if kept := _read(path).strip():
        return kept
    made = f"generated-{uuid.uuid4().hex}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(made + "\n", encoding="utf-8")
    return made


def machine_id(ws: Path, platform: str = sys.platform) -> str:
    """Edge. This machine's id: the Linux machine id, macOS's `IOPlatformUUID`, else the one kept in `ws`."""
    found = pick(_read(p) for p in LINUX_FILES) if platform.startswith("linux") else ""
    if not found and platform == "darwin":
        found = _ioreg()
    return found or _fallback(ws)
