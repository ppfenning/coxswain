"""Install proof checker: pure output checks and previous-tag selection.

Stdlib only, because the proof workflow runs this file with whatever python3 the
installed formula puts on PATH. The core takes text and returns a failure reason
or None; `main` is the thin edge that reads stdin and sets the exit code."""

from __future__ import annotations

import argparse
import json
import re
import sys

_DOTTED = re.compile(r"\d+(?:\.\d+)*")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _parse(tag: str) -> tuple[int, ...] | None:
    """Dotted integers after one optional leading `v`, trailing zeros dropped so 0.27 equals 0.27.0."""
    bare = tag.removeprefix("v")
    if _DOTTED.fullmatch(bare) is None:
        return None
    parts = tuple(int(part) for part in bare.split("."))
    last_nonzero = max((i for i, n in enumerate(parts) if n), default=-1)
    return parts[: last_nonzero + 1]


def previous_tag(tags: list[str], version: str) -> str | None:
    """The highest tag strictly below `version` by numeric dotted order, spelled as in `tags`."""
    target = _parse(version)
    if target is None:
        return None
    below = [(key, tag) for tag in tags if (key := _parse(tag)) is not None and key < target]
    return max(below, key=lambda pair: pair[0])[1] if below else None


def check_version(output: str, expected: str) -> str | None:
    """None when any line holds `expected` as a whole dotted token, else a one-line reason."""
    token = re.compile(rf"(?<![\d.]){re.escape(expected)}(?![\d.])")
    if any(token.search(line) for line in output.splitlines()):
        return None
    first = _one_line(output.splitlines()[0]) if output.strip() else ""
    return f"version: expected {expected!r}, first output line was {first!r}"


def _awaits_setup(row: dict) -> bool:
    """A fresh install has no profile until `cox setup install` runs: the profile row reports it missing and every
    row that needs a profile reports itself skipped. Those are the expected state of a clean machine."""
    detail = str(row.get("detail", ""))
    return (row.get("check") == "profile" and detail.startswith("missing:")) or detail == "skipped: no profile"


def _row_reason(row: object) -> str | None:
    if not isinstance(row, dict):
        return _one_line(f"doctor: row {row!r} failed: not an object")
    if row.get("ok") is True or _awaits_setup(row):
        return None
    return _one_line(f"doctor: {row.get('check', '?')} failed: {row.get('detail', '')}")


def _doctor_reason(data: object) -> str | None:
    if not isinstance(data, dict):
        return "doctor: output is not a JSON object"
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return "doctor: no rows"
    first_failure = next((reason for row in rows if (reason := _row_reason(row)) is not None), None)
    if first_failure is not None:
        return first_failure
    if not any(isinstance(r, dict) and r.get("ok") is True for r in rows):
        return "doctor: no row passed"
    return None


def check_doctor(text: str) -> str | None:
    """None for a JSON object with non-empty rows where every row passes or only awaits setup (a fresh machine has no
    profile yet), and at least one row passes. The top-level ok is not required: it is false until setup runs."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        return _one_line(f"doctor: invalid JSON: {exc}")
    return _doctor_reason(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install_proof")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("previous-tag").add_argument("version")
    sub.add_parser("check-version").add_argument("expected")
    sub.add_parser("check-doctor")
    args = parser.parse_args(argv)
    text = sys.stdin.read()
    if args.command == "previous-tag":
        tag = previous_tag(text.split(), args.version)
        if tag is None:
            return 1
        print(tag)
        return 0
    reason = check_version(text, args.expected) if args.command == "check-version" else check_doctor(text)
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
