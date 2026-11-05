"""The edge that drops a lane's test database when its worktree is cleaned.

Cleanup must not kill the run, so every outcome is a record: a launch error or a timeout is `failed`, never a raise.
With no drop command nothing runs and the record is `pending`: the name is listed for the operator to clean up.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping

DROP_TIMEOUT_SECONDS = 60
DETAIL_TAIL_CHARS = 500


def _command(drop_cmd: str, name: str) -> str:
    """Substitute `{name}` by replace, not format, so any other brace in the command survives."""
    return drop_cmd.replace("{name}", name)


def _tail(text: str) -> str:
    return text.strip()[-DETAIL_TAIL_CHARS:]


def _record(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def drop_lane_database(
    drop_cmd: str | None,
    name: str,
    env: Mapping[str, str],
    run: Callable = subprocess.run,
) -> dict:
    if not drop_cmd:
        return _record(name, "pending", "no drop command; listed for cleanup by the operator")
    try:
        done = run(
            _command(drop_cmd, name),
            shell=True,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=DROP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _record(name, "failed", f"{type(exc).__name__}: {exc}")
    output = _tail(f"{done.stdout or ''}\n{done.stderr or ''}")
    if done.returncode == 0:
        return _record(name, "dropped", output)
    return _record(name, "failed", output or f"exit {done.returncode}")
