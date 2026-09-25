"""check_release_index: docs/releases/index.md must carry a section for every
release page, naming each component's tag. The section's shape is the page's
own — a backticked version heading and a per-component table — and the check
reads it loosely: the heading exists and every tag appears before the next
heading. `index_section` renders that shape for the writer."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devtools.release_check import Drift


def index_section(version: str, component_tags: dict[str, str], components: Mapping | None = None) -> str:
    """The page's table for one release, rows in `component_tags` order (the
    manifest's, which is the order a notes PR writes). `components` supplies
    repo and required/flag when given; without it the row still names the tag."""
    components = components or {}
    rows = []
    for name, tag in component_tags.items():
        spec = components.get(name, {})
        repo = f"`{spec['repo']}`" if spec.get("repo") else spec.get("path", "")
        flag = "required" if spec.get("required") else (f"flag: `{spec['flag']}`" if spec.get("flag") else "")
        if spec.get("provides"):
            flag = f"{flag}, provides `{spec['provides']}`"
        if tag != f"v{version}":
            flag = f"{flag} (pinned)"
        rows.append(f"| {name} | {repo} | `{tag}` | {flag} |")
    table = "\n".join(["| Component | Repository or path | Tag | Required or flag |", "| --- | --- | --- | --- |", *rows])
    return f"## `{version}`\n\n{table}\n\nSee the [{version} release notes]({version}.md) for what landed in each component."


def _section_text(index_text: str, version: str) -> str | None:
    """The text under `## version` / `## \`version\`` up to the next `## `, or None."""
    match = re.search(rf"^## `?{re.escape(version)}`?\s*$", index_text, re.MULTILINE)
    if match is None:
        return None
    rest = index_text[match.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def check_release_index(facts: Mapping) -> list[Drift]:
    from devtools.release_check import Drift

    components = facts.get("manifest", {}).get("components", {})
    current = str(facts.get("manifest", {}).get("coxswain", {}).get("version", ""))
    index_text = facts.get("releases_index", "")
    index_file = facts.get("releases_index_path", "docs/releases/index.md")
    return [
        Drift("release_index", index_file, None, index_file, None, f"add its section for {version}")
        for version in sorted(facts.get("release_versions", set()))
        for tags in [{
            # The current version's row carries each component's manifest tag,
            # older than `v<version>` for a pinned component. An older section
            # expects `v<version>` only of a component tagged at the current
            # version; a pinned one kept whatever it was at then, so only its row is required.
            name: (str(spec.get("tag")) if version == current
                   else (f"v{version}" if spec.get("tag") == f"v{current}" else None))
            for name, spec in components.items()
        }]
        if (section := _section_text(index_text, version)) is None
        or any(f"| {name} |" not in section or (tag is not None and tag not in section) for name, tag in tags.items())
    ]


def gather_release_index_facts(umbrella: str) -> dict:
    releases_dir = Path(umbrella) / "docs" / "releases"
    index_path = releases_dir / "index.md"
    versions = {p.stem for p in releases_dir.glob("*.md") if p.stem != "index"} if releases_dir.is_dir() else set()
    return {
        "release_versions": versions,
        "releases_index": index_path.read_text() if index_path.exists() else "",
        "releases_index_path": str(index_path),
    }
