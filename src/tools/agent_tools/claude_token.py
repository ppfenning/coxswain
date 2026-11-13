"""The stored long-lived Claude token: where it lives, how it parses, how it is reported.

The token is a secret and never appears in any output or message from this module.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_FILE_NAME = "claude-token"

_REASON_TAIL = 200


def token_path(user_home: str) -> str:
    return os.path.join(user_home, ".config", "agent-tools", TOKEN_FILE_NAME)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_token_text(text: str) -> str | None:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if len(lines) != 1:
        return None
    line = lines[0]
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    prefix = TOKEN_ENV + "="
    if line.startswith(prefix):
        line = line[len(prefix) :]
    value = _unquote(line.strip()).strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    return value


def claude_env(environ: Mapping[str, str], token: str | None) -> dict[str, str]:
    if token is None:
        return dict(environ)
    return {**environ, TOKEN_ENV: token}


def _reports_logged_in(auth_stdout: str) -> bool:
    try:
        data = json.loads(auth_stdout)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("loggedIn") is True


def status_report(
    path: str,
    present: bool,
    auth_stdout: str,
    probe_exit: int | None,
    probe_tail: str,
) -> dict:
    """probe_exit None means the probe was not run."""
    if not present:
        return {
            "loggedIn": False,
            "tokenFile": False,
            "reason": f"claude token: no token file at {path}",
        }
    if probe_exit is not None and probe_exit != 0:
        return {
            "loggedIn": False,
            "tokenFile": True,
            "reason": f"claude token: rejected: {probe_tail[:_REASON_TAIL]}",
        }
    if not _reports_logged_in(auth_stdout):
        return {
            "loggedIn": False,
            "tokenFile": True,
            "reason": "claude token: claude auth status does not report logged in",
        }
    return {"loggedIn": True, "tokenFile": True, "reason": None}


def read_token(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    return parse_token_text(text)


def write_token(path: str, token: str) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    # Wrong belief: "chmod after writing is enough". Between write and chmod the
    # token sits on disk with the default umask's wider mode, so open it at 600.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
