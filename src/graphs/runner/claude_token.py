"""Read the stored Claude Code token from the host's own config."""

from __future__ import annotations

import os

# tools/agent_tools/claude_token.py holds the same rules; the copies must agree and both packages' tests pin the same literals.

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


def token_path(user_home: str) -> str:
    return os.path.join(user_home, ".config", "agent-tools", "claude-token")


def parse_token_text(text: str) -> str | None:
    """A bare token, or one `NAME=value` / `export NAME=value` line with NAME == TOKEN_ENV; else None."""
    lines = [s for s in (raw.strip() for raw in text.splitlines()) if s and not s.startswith("#")]
    if len(lines) != 1:
        return None
    line = lines[0]
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
        if "=" not in line:
            return None
    if "=" in line:
        name, _, line = line.partition("=")
        if name.strip() != TOKEN_ENV:
            return None
    value = line.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if not value or any(ch.isspace() for ch in value):
        return None
    return value


def read_token(path: str) -> str | None:
    """The token in the file at `path`, or None when it is missing, unreadable or malformed."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError, UnicodeDecodeError:
        return None
    return parse_token_text(text)
