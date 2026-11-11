from __future__ import annotations

import json

PREFIX = "land failed:"

# The first line of a land's output is often not the error: `forge: github` and git's push progress come first.
_NOISE_PREFIXES = (
    "forge:",
    "==>",
    "pushing",
    "enumerating objects",
    "counting objects",
    "compressing objects",
    "writing objects",
    "delta compression",
    "total ",
)


def _is_noise(line: str) -> bool:
    return line.lower().startswith(_NOISE_PREFIXES) or line.endswith(("...", "…"))


def _text(label: str, value: str) -> list[str]:
    return [f"{label}={json.dumps(value.strip())}"] if value.strip() else []


def first_error_line(text: str) -> str:
    """First non-blank line that is not a banner or progress line, stripped; empty when none."""
    lines = (line.strip() for line in text.splitlines())
    return next((line for line in lines if line and not _is_noise(line)), "")


def failure_line(step: str, forge: str, command: str, code: int | None, detail: str) -> str:
    """One line: the prefix, then `label=value` fields. Text values are JSON strings, so any character is safe."""
    fields = [
        *_text("step", step),
        *_text("forge", forge),
        *_text("command", command),
        *([] if code is None else [f"code={code}"]),
        *_text("error", first_error_line(detail)),
    ]
    return " ".join([PREFIX, *fields])


def failure_of(output: str) -> str | None:
    """First line of a land's output that starts with the failure prefix once stripped, returned stripped, or None."""
    lines = (line.strip() for line in output.splitlines())
    return next((line for line in lines if line.startswith(PREFIX)), None)
