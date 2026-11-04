"""The CLI-surface release check: pure over facts the edge below gathers."""

from __future__ import annotations

import argparse
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devtools.release_check import Drift

_DOC_COMMAND = re.compile(r"`(cox(?: (?!-)[\w-]+)+)[^`]*`")


# build_parser registers `dev` by hand, so COMMAND_TABLE never lists it; `release` is hand-built too but SUPPRESS-help, so it stays out.
HAND_BUILT: tuple[str, ...] = ("cox dev",)


def _is_suppressed(item: Any) -> bool:
    return argparse.SUPPRESS in (getattr(item, "help", None), getattr(item, "summary", None))


def _leaves(rows: Sequence[Any]) -> list[tuple[Any, tuple[str, ...]]]:
    """Each visible invocable row with its name path; a row with subcommands is replaced by them."""
    return [
        (leaf, path)
        for r in rows
        if not _is_suppressed(r)
        for leaf, path in ([(s, (r.name, s.name)) for s in getattr(r, "subcommands", ())] or [(r, (r.name,))])
        if not _is_suppressed(leaf)
    ]


def table_commands(table: Sequence[tuple[Any, Sequence[Any]]], program: str = "cox") -> set[str]:
    """Full command strings of a (Group, rows) table; a group with no rows is one leaf."""
    return {
        " ".join([program, group.name, *path])
        for group, rows in table
        if not _is_suppressed(group)
        for _, path in (_leaves(rows) if rows else [(group, ())])
    }


def table_suppressed(table: Sequence[tuple[Any, Sequence[Any]]], program: str = "cox") -> set[str]:
    """Top-level groups whose own help is SUPPRESS, as `{program} {group}`."""
    return {f"{program} {group.name}" for group, _ in table if _is_suppressed(group)}


def commands_in_doc(text: str) -> set[str]:
    return {m.group(1) for m in _DOC_COMMAND.finditer(text)}


def _group(command: str) -> str:
    return command.split()[1]


def _namespaces(cli_commands: set[str]) -> set[str]:
    """A prefix shorter than a full command names a namespace, never a drift on its own."""
    return {" ".join(tokens[:i]) for cmd in cli_commands for tokens in (cmd.split(),) for i in range(2, len(tokens))}


def _doc_target(doc_commands: Mapping[str, set[str]], command: str) -> str:
    group = _group(command)
    return next((path for path in doc_commands if Path(path).stem == group),
                f"docs/reference/cli/{group}.md")


def check_cli_surface(facts: Mapping) -> list[Drift]:
    """Compares the command table's commands against the pages built from that
    same table, never against docs/reference/cli/*.md in the tree
    (only index.md is committed there; the per-group pages are build output).
    A `generator_error` fact short-circuits to one Drift, not one per symbol."""
    from devtools.release_check import Drift

    generator_error = facts.get("generator_error")
    if generator_error:
        return [Drift("cli_surface", "docs/_cli.py", None, "cox --help", None, generator_error)]

    cli_commands: set[str] = facts.get("cli_commands", set())
    doc_commands: Mapping[str, set[str]] = facts.get("doc_commands", {})
    readme_commands: Mapping[str, set[str]] = facts.get("readme_commands", {})
    namespaces = _namespaces(cli_commands)
    suppressed = {_group(cmd) for cmd in facts.get("suppressed", set())}
    documented = {cmd for cmds in doc_commands.values() for cmd in cmds}
    missing_docs = [
        Drift("cli_surface", "cox --help", None, _doc_target(doc_commands, cmd), None,
              f"add {cmd} to {_doc_target(doc_commands, cmd)}")
        for cmd in sorted(cli_commands - documented)
        if _group(cmd) not in suppressed
    ]
    missing_readme = [
        Drift("cli_surface", "cox --help", None, f"{_group(cmd)}/README.md", None,
              f"add {cmd} to {_group(cmd)}/README.md")
        for cmd in sorted(cli_commands)
        if _group(cmd) in readme_commands and cmd not in readme_commands[_group(cmd)]
        and _group(cmd) not in suppressed
    ]
    stray_docs = [
        Drift("cli_surface", path, None, "cox --help", None, f"remove {cmd} from {path}")
        for path, cmds in sorted(doc_commands.items())
        for cmd in sorted(cmds)
        if cmd not in cli_commands and cmd not in namespaces and _group(cmd) not in suppressed
    ]
    stray_readmes = [
        Drift("cli_surface", f"{name}/README.md", None, "cox --help", None,
              f"remove {cmd} from {name}/README.md")
        for name, cmds in sorted(readme_commands.items())
        for cmd in sorted(cmds)
        if cmd not in cli_commands and cmd not in namespaces and _group(cmd) not in suppressed
    ]
    return missing_docs + missing_readme + stray_docs + stray_readmes


def _table_doc_commands(commands: set[str]) -> dict[str, set[str]]:
    """One page per command group, written to a scratch directory and read back
    with `commands_in_doc`, the same as a real docs build's output. Pages keep
    the canonical docs/reference/cli/<group>.md label so a remediation message
    still points somewhere that exists once the scratch directory is gone."""
    groups: dict[str, set[str]] = {}
    for command in commands:
        groups.setdefault(_group(command), set()).add(command)
    pages: dict[str, set[str]] = {}
    with tempfile.TemporaryDirectory() as out_dir:
        for group, group_commands in groups.items():
            page = Path(out_dir) / f"{group}.md"
            page.write_text("".join(f"- `{command}`\n" for command in sorted(group_commands)))
            pages[f"docs/reference/cli/{group}.md"] = commands_in_doc(page.read_text())
    return pages


def gather_cli_facts(root: str) -> dict:
    """The commands come from `agent_tools.cli.COMMAND_TABLE`, imported at call time. `root` is the
    coxswain checkout, where a top-level folder's README named after a command group must list that
    group's commands."""
    readme_paths = sorted(Path(root).glob("*/README.md"))
    readme_commands = {p.parent.name: commands_in_doc(p.read_text()) for p in readme_paths}
    try:
        from agent_tools.cli import COMMAND_TABLE

        commands = table_commands(COMMAND_TABLE) | set(HAND_BUILT)
        suppressed = table_suppressed(COMMAND_TABLE)
        doc_commands = _table_doc_commands(commands)
    except Exception as exc:
        return {
            "generator_error": f"agent_tools.cli COMMAND_TABLE unavailable: {exc}",
            "readme_commands": readme_commands,
        }
    return {
        "cli_commands": commands,
        "doc_commands": doc_commands,
        "readme_commands": readme_commands,
        "suppressed": suppressed,
    }
