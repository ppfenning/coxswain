"""Render terminal screenshots of `cox` commands to SVG for the docs.

The core (`shot_plan`) is pure: it names each command to capture and the
argv to run it with, with no I/O. The edge (`main`) runs a maintainer's own
machine, capturing real output and writing SVGs; it does nothing but say so
and exit 0 if `cox` isn't on PATH. Rich is imported inside the edge function
so importing this module never requires it to be installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SHOTS_DIR = Path("docs/assets/shots")


def shot_plan() -> list[tuple[str, list[str]]]:
    """Return the (name, argv) pairs to capture, one per screenshot."""
    return [
        ("versions", ["cox", "versions", "--manifest", "manifest.toml"]),
        ("doctor", ["cox", "setup", "doctor"]),
        ("route-status", ["cox", "route", "status"]),
        (
            "install-dry-run",
            ["cox", "install", "--dry-run", "--manifest", "manifest.toml", "--root", "/tmp/x"],
        ),
    ]


MAX_LINES = 14
MAX_COLS = 100


def clip(output: str, max_lines: int = MAX_LINES, max_cols: int = MAX_COLS) -> str:
    """Pure: `output` bounded to a screenshot's worth of terminal.

    A screenshot is an illustration, not a dump. Two things have to be bounded,
    not one: `cox route status` prints a line per run the workspace has ever
    recorded, and each of those lines carries a whole quarantine reason, so a
    line cap alone still wraps into hundreds of rendered rows. Lines past
    `max_lines` and columns past `max_cols` are cut, and each cut is marked so
    the reader knows the terminal said more than the picture shows.
    """
    lines = output.splitlines()
    kept = [line if len(line) <= max_cols else line[: max_cols - 1] + "\u2026" for line in lines[:max_lines]]
    if len(lines) > max_lines:
        kept.append(f"... {len(lines) - max_lines} more lines")
    return "\n".join(kept)


def redact(output: str, home: str) -> str:
    """Pure: the maintainer's home directory replaced by `~`.

    These SVGs ship on a public site. The commands print real profile and
    workspace paths, which carry a username; `~` is what a reader needs and
    all they should get. `home` is passed in rather than read, so the rule is
    testable and this function has no side door.
    """
    return output.replace(home.rstrip("/"), "~") if home else output


def _capture(argv: list[str], home: str) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return clip(redact(result.stdout + result.stderr, home))


def main() -> None:
    if shutil.which("cox") is None:
        print("cox is not on PATH; skipping screenshot capture")
        return

    from rich.color_triplet import ColorTriplet
    from rich.console import Console
    from rich.terminal_theme import TerminalTheme

    background = ColorTriplet(0x0B, 0x16, 0x22)
    foreground = ColorTriplet(0xE8, 0xEE, 0xF2)
    primary = ColorTriplet(0x2A, 0x9D, 0x8F)
    accent = ColorTriplet(0xE9, 0xC4, 0x6A)
    muted = ColorTriplet(0x9F, 0xB3, 0xC8)
    cox_dark_theme = TerminalTheme(
        background,
        foreground,
        [background, accent, primary, accent, primary, accent, muted, foreground],
    )

    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, argv in shot_plan():
        console = Console(record=True, width=100)
        console.print(_capture(argv, str(Path.home())))
        svg = console.export_svg(title=" ".join(argv), theme=cox_dark_theme)
        (SHOTS_DIR / f"{name}.svg").write_text(svg)


if __name__ == "__main__":
    sys.exit(main())
