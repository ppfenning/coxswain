"""Sole contact point with the courier inbox: subprocess only, never the `cox` package."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

RUN_EXITED_EVENT = "run_exited"
CHAIR_COURIER_ENV = "COX_CHAIR_COURIER"
_REMOTE_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")


def send(ref: str, to: str, note: str) -> bool:
    """Best-effort `cox courier send`; absent `cox` or any failure returns False, never raises."""
    if shutil.which("cox") is None:
        return False
    try:
        result = subprocess.run(
            ["cox", "courier", "send", ref, "--to", to, "--note", note],
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


def run_exited_argv(run_id: str, initiative: str, outcome: str) -> list[str]:
    """The `cox courier send` argv for a finished run: its own `run_exited` event, three fields, addressed to chair."""
    return [
        "cox", "courier", "send", f"coxswain://run/{run_id}", "--to", "chair",
        "--note", f"run {run_id} exited {outcome}",
        "--event", RUN_EXITED_EVENT,
        "--field", f"run_id={run_id}",
        "--field", f"initiative={initiative}",
        "--field", f"outcome={outcome}",
    ]


def remote_argv(destination: str, argv: list[str]) -> list[str]:
    """`argv` run on `destination` through a bounded, non-interactive remote shell call."""
    return ["ssh", *_REMOTE_OPTIONS, destination, shlex.join(argv)]


def publish_run_exited(run_id: str, initiative: str, outcome: str) -> bool:
    """Best-effort `run_exited` entry on the chair's bus: remote when COX_CHAIR_COURIER is set, else local. Never raises."""
    destination = os.environ.get(CHAIR_COURIER_ENV, "").strip()
    argv = run_exited_argv(run_id, initiative, outcome)
    if destination:
        if shutil.which("ssh") is None:
            return False
        argv, timeout = remote_argv(destination, argv), 15
    else:
        if shutil.which("cox") is None:
            return False
        timeout = 10
    try:
        result = subprocess.run(argv, timeout=timeout, stdin=subprocess.DEVNULL)
    except Exception:
        return False
    return result.returncode == 0
