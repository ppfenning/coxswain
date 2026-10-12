"""Text transforms that register a new release-notes page: its section in
docs/releases/index.md and its entry in the docs nav. Both are pure text to
text; the caller reads and writes the files."""

from __future__ import annotations

import re

from devtools.release import release_index_text


def add_index_rows(index_text: str, version: str, components: list[tuple[str, str]]) -> str:
    """`index_text` with `version`'s section added, unchanged when it is already there.
    `components` is (name, tag) pairs; repo and flag columns stay blank without a manifest spec."""
    if re.search(rf"^## `?{re.escape(version)}`?\s*$", index_text, re.MULTILINE):
        return index_text
    manifest = {"components": {name: {"tag": tag} for name, tag in components}}
    tagged = [name for name, tag in components if tag == f"v{version}"]
    return release_index_text(index_text, version, manifest, tagged)


def add_nav_line(nav_text: str, version: str) -> str:
    """`nav_text` with `version`'s release entry directly above the first existing one,
    same indentation and form. Unchanged when present; ValueError when nothing anchors it."""
    if re.search(rf"^[ \t]*- [^:\n]+: releases/{re.escape(version)}\.md[ \t]*$", nav_text, re.MULTILINE):
        return nav_text
    lines = nav_text.splitlines(keepends=True)
    anchor = re.compile(r"^([ \t]*)- [^:\n]+: releases/(?!index\.md)\S+\.md[ \t]*$")
    at = next((i for i, line in enumerate(lines) if anchor.match(line.rstrip("\r\n"))), None)
    if at is None:
        raise ValueError(f"no releases/<version>.md entry found in nav to anchor {version} on")
    indent = anchor.match(lines[at].rstrip("\r\n")).group(1)
    new = f"{indent}- {version}: releases/{version}.md\n"
    return "".join([*lines[:at], new, *lines[at:]])
