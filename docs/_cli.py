"""Generate docs/reference/cli/ from `cox --help` at build time.

The core (`parse_subcommands`) is pure: given a block of argparse `--help`
text, it decides which names are subcommands, with no I/O. The edge (`main`)
runs `cox --help` and one `--help` per group and subcommand it discovers, and
writes nothing at all if `cox` isn't on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REFERENCE_DIR = Path("docs/reference/cli")


def parse_subcommands(help_text: str) -> list[str]:
    """Return the subcommand names listed under "positional arguments:".

    Argparse indents the `{a,b,c}` placeholder line at one depth and each
    named subcommand one depth deeper; only the deeper indent is a name.
    """
    lines = help_text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().startswith("positional arguments")]
    if not starts:
        return []
    names = []
    for line in lines[starts[0] + 1 :]:
        if line and not line[:1].isspace():
            break
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 4 and stripped and not stripped.startswith("{"):
            names.append(stripped.split()[0])
    return names


def _help(*args: str) -> str:
    return subprocess.run(["cox", *args, "--help"], capture_output=True, text=True, check=True).stdout


def _group_page(name: str, group_help: str, subcommands: list[tuple[str, str]]) -> str:
    lines = [f"# {name}", "", "```", group_help.rstrip(), "```", ""]
    for sub_name, sub_help in subcommands:
        lines += [f"## {sub_name}", "", "```", sub_help.rstrip(), "```", ""]
    return "\n".join(lines)


def _index_page(names: list[str]) -> str:
    lines = ["# CLI reference", "", "Generated from `cox --help` at build time.", ""]
    lines += [f"- [{name}]({name}.md)" for name in names]
    return "\n".join(lines) + "\n"


def main() -> None:
    if shutil.which("cox") is None:
        print("cox is not on PATH; skipping CLI reference generation")
        return
    names = parse_subcommands(_help())
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        group_help = _help(name)
        subcommands = [(sub, _help(name, sub)) for sub in parse_subcommands(group_help)]
        (REFERENCE_DIR / f"{name}.md").write_text(_group_page(name, group_help, subcommands))
    (REFERENCE_DIR / "index.md").write_text(_index_page(names))


if __name__ == "__main__":
    sys.exit(main())
