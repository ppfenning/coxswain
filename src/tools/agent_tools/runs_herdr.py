"""`cox runs herdr`: push bar's idle|running|attention class to a herdr pane.

The pure core maps a class to a herdr state, builds the label and decides when to send. The edge
shells out to the herdr binary named by HERDR_BIN_PATH and never lets a herdr failure stop the loop.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from agent_tools import runs_bar

__all__ = ["decide", "env_ok", "fleet_state", "herdr_state", "install_exit_handlers", "label", "run_loop"]

SOURCE = "coxswain"
AGENT = "coxswain"  # herdr docs: never reuse a built-in agent's name such as "claude"
_ENV_KEYS = ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_BIN_PATH")
_STATES = {"running": "working", "attention": "blocked", "idle": "idle"}

Runner = Callable[[Sequence[str]], int]


def herdr_state(cls: str) -> str:
    """Bar's class as herdr's state. An unknown class reads as idle."""
    return _STATES.get(cls, "idle")


def fleet_state(cls: str, stranded: int) -> str:
    """Approved work waiting to land is attention, which bar's class does not carry, so it forces blocked."""
    return "blocked" if stranded > 0 else herdr_state(cls)


def label(rows: Sequence[Mapping], stranded: int = 0) -> str:
    live = sum(1 for r in rows if r["alive"])
    held = sum(1 for r in rows if runs_bar.attention([r]))
    parts = [f"{live} in flight"]
    parts += [f"{stranded} to land"] if stranded else []
    parts += [f"{held} need attention"] if held else []
    return ", ".join(parts)


def decide(prev_state: str | None, state: str, last_sent_at: float | None, now: float, interval: float) -> bool:
    """Send on a state change, or as a keepalive once a full interval has passed since the last send."""
    if prev_state != state or last_sent_at is None:
        return True
    return now - last_sent_at >= interval


def env_ok(environ: Mapping[str, str]) -> bool:
    return all(environ.get(k) for k in _ENV_KEYS)


def install_exit_handlers(register: Callable[[int, Callable], object] = signal.signal) -> None:
    """Edge: turn SIGTERM and SIGHUP (a closing herdr pane) into SystemExit so run_loop's finally releases.
    SIGINT already raises KeyboardInterrupt."""

    def leave(_signum, _frame):
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        register(sig, leave)


def _subprocess_runner(argv: Sequence[str]) -> int:
    """Edge: run the herdr binary. A missing binary or a hang is a failure code, not an exception."""
    try:
        return subprocess.run(list(argv), capture_output=True, timeout=10, check=False).returncode
    except OSError, subprocess.SubprocessError:
        return 127


def _call(runner: Runner, argv: Sequence[str], err, quiet: bool = False) -> bool:
    code = runner(argv)
    if code != 0 and not quiet:
        print(f"runs herdr: {' '.join(argv[1:3])} failed ({code})", file=err)
    return code == 0


def run_loop(
    rows_fn: Callable[[], Sequence[Mapping]],
    stranded_fn: Callable[[], int],
    environ: Mapping[str, str],
    *,
    once: bool,
    interval: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    runner: Runner = _subprocess_runner,
    err=None,
    seq_start: int = 0,
) -> int:
    """`seq_start` seeds the counter so a restarted process never reuses a seq an earlier one sent."""
    err = sys.stderr if err is None else err
    if not env_ok(environ):
        print("runs herdr: HERDR_ENV, HERDR_PANE_ID and HERDR_BIN_PATH must all be set; nothing to report", file=err)
        return 0
    herdr, pane = environ["HERDR_BIN_PATH"], environ["HERDR_PANE_ID"]
    seq = seq_start
    prev_state: str | None = None
    prev_label: str | None = None
    last_sent_at: float | None = None
    label_ok = True
    try:
        while True:
            rows, stranded = rows_fn(), stranded_fn()
            state = fleet_state(runs_bar.bar(rows, runs_bar.attention(rows))["class"], stranded)
            t = now()
            sent_ok = True
            if decide(prev_state, state, last_sent_at, t, interval):
                seq += 1
                sent_ok = _call(
                    runner,
                    [
                        herdr,
                        "pane",
                        "report-agent",
                        pane,
                        "--source",
                        SOURCE,
                        "--agent",
                        AGENT,
                        "--state",
                        state,
                        "--seq",
                        str(seq),
                    ],
                    err,
                )
                if sent_ok:
                    prev_state, last_sent_at = state, t
            text = label(rows, stranded)
            # The herdr CLI may not accept a label. A refusal is skipped silently and not retried.
            if sent_ok and label_ok and text != prev_label:
                label_ok = _call(
                    runner,
                    [herdr, "pane", "report-metadata", pane, "--source", SOURCE, "--label", text],
                    err,
                    quiet=True,
                )
                prev_label = text if label_ok else prev_label
            if once:
                return 0
            sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        _call(runner, [herdr, "pane", "release-agent", pane, "--source", SOURCE], err)
