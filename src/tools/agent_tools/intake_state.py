"""The rule for one intake entry's queue state: `queued`, `decomposed`, or `landed`.

Pure core, no I/O. `route.intake_groups` builds `intake_index` once and calls `intake_group` once per entry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*$", re.DOTALL | re.MULTILINE)
_INTAKE_LINE = re.compile(r"^intake:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_STATE_LINE = re.compile(r"^state:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def intake_field(text: str) -> str | None:
    """The first `intake:` line of the leading frontmatter block, quotes stripped; None with no block or no line."""
    block = _FRONTMATTER.match(text)
    line = _INTAKE_LINE.search(block.group(1)) if block else None
    return line.group(1).strip("\"'") if line else None


def initiative_state(text: str) -> str | None:
    """The first `state:` line of the leading frontmatter block, quotes stripped; None with no block or no line."""
    block = _FRONTMATTER.match(text)
    line = _STATE_LINE.search(block.group(1)) if block else None
    return line.group(1).strip("\"'") if line else None


def _is_dropped(initiative: dict) -> bool:
    return initiative_state(initiative.get("text", "")) == "dropped"


def intake_index(initiatives: Sequence[dict]) -> dict[str, list]:
    """Intake path to the initiatives whose `intake:` line names it, in input order; parses each initiative once."""
    index: dict[str, list] = {}
    for initiative in initiatives:
        path = intake_field(initiative.get("text", ""))
        if path is not None and not _is_dropped(initiative):
            index.setdefault(path, []).append(initiative)
    return index


def intake_group(
    entry: dict, initiatives_by_id: dict, initiatives: list, naming_index: Mapping[str, list] | None = None
) -> str:
    """`naming_index` is `intake_index(initiatives)`; without it each call re-parses every initiative."""
    initiative_id = entry.get("initiative")
    if initiative_id is not None:
        initiative = initiatives_by_id.get(initiative_id)
        return "landed" if initiative and initiative["done"] else "decomposed"
    naming = (
        naming_index.get(entry["path"], [])
        if naming_index is not None
        else [i for i in initiatives if intake_field(i.get("text", "")) == entry["path"] and not _is_dropped(i)]
    )
    if naming:
        return "landed" if all(i["done"] for i in naming) else "decomposed"
    if not entry.get("done"):
        return "queued"
    cited = any(entry["path"] in i.get("text", "") for i in initiatives)
    return "decomposed" if cited else "landed"
