"""The lenient frontmatter splitter that the decompose graph and `route_reach` share.

`core.workstore.split_frontmatter` is the strict one, for work items: it refuses a file with no block. This
one never refuses, because an idea or an intake may have no frontmatter at all.
"""

from __future__ import annotations

from typing import Any, cast

import yaml


def split(text: str) -> tuple[dict[str, Any], str]:
    """Pure. The leading `---` block of `text` as a mapping, and the text after it.

    No opening line, or no closing line, is no block: `({}, text)`. A block that is not
    YAML, or not a mapping, still comes off the body and gives `{}`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    close = next((i for i, line in enumerate(lines) if i and line.strip() == "---"), None)
    if close is None:
        return {}, text
    try:
        loaded: object = yaml.safe_load("\n".join(lines[1:close]))
    except yaml.YAMLError:
        loaded = None
    meta = cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else {}
    return meta, "\n".join(lines[close + 1 :])
