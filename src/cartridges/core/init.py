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
_REQUIRED_YAML_KEYS = ("team:", "extends:", "description:")


def _validate(team: str, extends: str, template: Mapping[str, str]) -> None:
    if not _SLUG.match(team):
        raise ValueError(f"'{team}' is not a valid team slug (expected [a-z0-9][a-z0-9-]*)")
    if team in _RESERVED:
        raise ValueError(f"'{team}' is reserved and cannot be a team name")
    if team == extends:
        raise ValueError(f"a team cannot extend itself ('{team}')")
    if extends not in _KNOWN_EXTENDS:
        raise ValueError(f"'extends' must be one of {_KNOWN_EXTENDS}, got '{extends}'")
    if "cartridge.yaml" not in template:
        raise ValueError("template has no 'cartridge.yaml'")
    yaml_lines = template["cartridge.yaml"].splitlines()
    missing = [key for key in _REQUIRED_YAML_KEYS if not any(line.startswith(key) for line in yaml_lines)]
    if missing:
        raise ValueError(f"template's cartridge.yaml is missing line(s): {', '.join(missing)}")


def _rewrite_line(line: str, team: str, extends: str) -> str:
    """Rewrite an exact top-level key; an indented line merely containing the
    same word (e.g. a nested `description:` under some other key) is untouched."""
    ending = line[len(line.rstrip("\n")):]
    if line.startswith("team:"):
        return f"team: {team}{ending}"
    if line.startswith("extends:"):
        return f"extends: {extends}{ending}"
    if line.startswith("description:"):
        return f"description: {team}'s overlay on the {extends} cartridge{ending}"
    return line


def _rewrite_cartridge_yaml(text: str, team: str, extends: str) -> str:
    return "".join(_rewrite_line(line, team, extends) for line in text.splitlines(keepends=True))


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
    _validate(team, extends, template)

    team_dir = root / team
    needed_links = [extends] if extends == "base" else [extends, "base"]

    mkdir_steps = [{"op": "mkdir", "path": str(team_dir)}] + [
        {"op": "mkdir", "path": str(team_dir / rel)} for rel in _ancestor_dirs(template.keys())
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
    # Breadcrumb: this copies every template value verbatim except the three
    # rewritten cartridge.yaml lines. A template is not neutral content — the
    # shipped cartridges/example-team/cartridge.yaml binds every role to
    # example-skills:* and sets a fake tracker/workspace_id/auth_env, and under
    # extends: local those child scalars would win over local's own bindings
    # in core/cartridge.py's _merge. Whether example-team is the right
    # template to hand in for a given `extends` is the caller's decision, not
    # this module's; init_plan only renders whatever template it is given.
    write_steps = [
        {
            "op": "write",
            "path": str(team_dir / relative_path),
            "text": _rewrite_cartridge_yaml(text, team, extends) if relative_path == "cartridge.yaml" else text,
        }
        for relative_path, text in template.items()
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
