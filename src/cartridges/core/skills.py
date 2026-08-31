"""Skill index: map a bound skill NAME to the skill bodies that claim it.

The cartridge loader refuses to resolve when a bound name does not resolve to
exactly one body. That check needs an index, and building one means touching the
filesystem — so it lives here, at the edge, and never inside `load`.

A skill name is `plugin:skill`. A plugin is a directory carrying
`.claude-plugin/plugin.json`; its skills are `skills/<name>/SKILL.md`. Two
plugins may legitimately be installed that both declare a skill of the same
name, which is exactly the ambiguity the loader exists to catch — so the index
maps a name to a LIST, and reports the collision rather than picking a winner.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

__all__ = ["index_from_roots"]


def _plugin_name(plugin_dir: Path) -> str | None:
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return name if isinstance(name, str) and name else None


def index_from_roots(roots: Iterable[Path | str]) -> Mapping[str, list[Path]]:
    """Scan roots for plugins and return {"plugin:skill": [body, ...]}.

    A root may be a plugin itself or a directory containing plugins; both are
    common (`~/repos/example-skills` vs `~/.claude/plugins`), and guessing wrong
    would silently produce an empty index — which would then fail every binding
    at load with a misleading message.
    """
    index: dict[str, list[Path]] = {}
    for root in (Path(r).expanduser() for r in roots):
        if not root.is_dir():
            continue
        candidates = [root] if _plugin_name(root) else sorted(p for p in root.iterdir() if p.is_dir())
        for plugin_dir in candidates:
            plugin = _plugin_name(plugin_dir)
            if plugin is None:
                continue
            for body in sorted((plugin_dir / "skills").glob("*/SKILL.md")):
                index.setdefault(f"{plugin}:{body.parent.name}", []).append(body)
    return index
