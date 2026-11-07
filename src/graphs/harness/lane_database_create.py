"""The edge that creates a lane's test database from the schema file on first use.

Every outcome is a record: a launch error or a timeout is `failed`, never a raise.
The function does not track whether the database exists; the configured command must tolerate an existing one.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping

CREATE_TIMEOUT_SECONDS = 60
DETAIL_TAIL_CHARS = 500


def _command(create_cmd: str, schema: str, name: str) -> str:
    """Substitute `{name}` and `{schema}` by replace, not format, so any other brace survives."""
    return create_cmd.replace("{name}", name).replace("{schema}", schema)


def _tail(text: str) -> str:
    return text.strip()[-DETAIL_TAIL_CHARS:]


def _record(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def create_lane_database(
    create_cmd: str | None,
    schema: str,
    name: str,
    env: Mapping[str, str],
    cwd: str,
    run: Callable = subprocess.run,
) -> dict:
    if not create_cmd:
        return _record(name, "skipped", "no create command was configured")
    try:
        done = run(
            _command(create_cmd, schema, name),
            shell=True,
            cwd=cwd,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=CREATE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _record(name, "failed", f"{type(exc).__name__}: {exc}")
    output = _tail(f"{done.stdout or ''}\n{done.stderr or ''}")
    if done.returncode == 0:
        return _record(name, "created", output)
    return _record(name, "failed", output or f"exit {done.returncode}")


def creation_failure(record: Mapping[str, str]) -> str:
    """One-line reason when the record is `failed`, else an empty string."""
    if record.get("status") != "failed":
        return ""
    lines = (record.get("detail") or "").strip().splitlines()
    reason = lines[0].strip() if lines else "no detail"
    return f"lane database {record.get('name', '')} not created: {reason}"
