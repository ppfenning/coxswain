"""The pure plan for a new team cartridge.

`init_plan` decides what files and links a new team directory needs; it never
touches a filesystem. The template it renders from is content the caller (the
CLI edge) has already read from `cartridges/example-team/` — this module
imports only the standard library and never opens a file itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Mapping

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_RESERVED = ("base", "local")
_KNOWN_EXTENDS = ("base", "local")


def _validate(team: str, extends: str) -> None:
    if not _SLUG.match(team):
        raise ValueError(f"'{team}' is not a valid team slug (expected [a-z0-9][a-z0-9-]*)")
    if team in _RESERVED:
        raise ValueError(f"'{team}' is reserved and cannot be a team name")
    if team == extends:
        raise ValueError(f"a team cannot extend itself ('{team}')")
    if extends not in _KNOWN_EXTENDS:
        raise ValueError(f"'extends' must be one of {_KNOWN_EXTENDS}, got '{extends}'")


def _minimal_cartridge_yaml(team: str, extends: str) -> str:
    """A cartridge that only extends inherits every binding the loader merges
    (core/cartridge.py's `_merge`) — so a freshly scaffolded team starts with
    no bindings of its own rather than inheriting a template's fictional
    ones."""
    return (
        f"# {team}'s cartridge. Everything the graphs need is inherited from `{extends}`;\n"
        "# add `cast:` bindings for this team's installed plugins, and `context/` files\n"
        "# the reviewers should hold work to, here.\n"
        f"team: {team}\n"
        f"extends: {extends}\n"
        f"description: {team}'s overlay on the {extends} cartridge\n"
        "version: 1\n"
    )


def _ancestor_dirs(relative_paths: Iterable[str]) -> list[str]:
    """Every directory a write step needs, ordered so a parent precedes its child."""
    prefixes = {
        "/".join(Path(relative_path).parts[: depth + 1])
        for relative_path in relative_paths
        for depth in range(len(Path(relative_path).parts) - 1)
    }
    return sorted(prefixes, key=lambda p: (p.count("/"), p))


def init_plan(
    team: str,
    cartridges_dir: str | Path,
    *,
    extends: str = "local",
    package_cartridges_dir: str | Path,
    template: Mapping[str, str],
) -> list[dict]:
    """Return the ordered steps that would build `<cartridges_dir>/<team>`."""
    root = Path(cartridges_dir)
    package_root = Path(package_cartridges_dir)
    _validate(team, extends)

    team_dir = root / team
    needed_links = [extends] if extends == "base" else [extends, "base"]
    context_template = {path: text for path, text in template.items() if path != "cartridge.yaml"}

    mkdir_steps = [{"op": "mkdir", "path": str(team_dir)}] + [
        {"op": "mkdir", "path": str(team_dir / rel)} for rel in _ancestor_dirs(context_template.keys())
    ]
    # `base` and `local` only need a symlink when they live somewhere other than
    # `cartridges_dir` itself. When the two roots coincide (every cartridge in
    # THIS repo's own `cartridges/` sits that way, `base` and `local` beside
    # `example-team` with no symlink at all) the names are already there.
    symlink_steps = (
        []
        if root == package_root
        else [
            {"op": "symlink", "path": str(root / name), "target": str(package_root / name)}
            for name in needed_links
        ]
    )
    # Breadcrumb: the team's own cartridge.yaml is generated fresh, never
    # copied from the template. The shipped cartridges/example-team/cartridge.yaml
    # binds every role to a fictional example-skills:* plugin; copying it
    # would hand a new team ten bindings that resolve to no skill body.
    # Everything else the template carries (context/* charters, etc.) is
    # still copied verbatim, if the caller's template includes any.
    write_steps = [
        {"op": "write", "path": str(team_dir / "cartridge.yaml"), "text": _minimal_cartridge_yaml(team, extends)}
    ] + [
        {"op": "write", "path": str(team_dir / relative_path), "text": text}
        for relative_path, text in context_template.items()
    ]
    print_step = [{"op": "print", "text": f"team: {team}\ncartridges_dir: {root}"}]

    return mkdir_steps + symlink_steps + write_steps + print_step


def _render_step(step: dict) -> str:
    op = step["op"]
    if op == "mkdir":
        return f"mkdir {step['path']}"
    if op == "symlink":
        return f"link {step['path']} -> {step['target']}"
    if op == "write":
        n_lines = step["text"].count("\n") + (0 if step["text"].endswith("\n") else 1)
        return f"write {step['path']} ({n_lines} lines)"
    if op == "print":
        # The print step's own text carries a real newline (the two profile
        # lines a caller will print verbatim); escape it here so this step
        # still renders as exactly one dry-run line, like every other step.
        escaped_text = step["text"].replace("\n", "\\n")
        return f"print {escaped_text}"
    raise ValueError(f"unknown step op: {op!r}")


def render_plan(steps: list[dict]) -> str:
    """One line per step, for `--dry-run`."""
    return "\n".join(_render_step(step) for step in steps)
