"""Parse a release's migration declaration from its notes text and decide whether the migrate scenario met it."""

import re
from typing import NamedTuple

_LINE = re.compile(r"^Migrations:(.*)$", re.MULTILINE)


class MigrationError(NamedTuple):
    message: str


def validate_migrations(names: tuple[str, ...]) -> MigrationError | None:
    """None when every name is a non-empty single token and none repeats; an empty list is valid."""
    for name in names:
        if not name or len(name.split()) != 1:
            return MigrationError(f"migration id must be one token with no spaces: {name!r}")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        return MigrationError(f"duplicate migration id: {duplicates[0]}")
    return None


def parse_declaration(notes_text: str) -> tuple[str, ...] | MigrationError | None:
    """None when no Migrations line exists, () for 'none', else the ordered ids. The first such line wins."""
    match = _LINE.search(notes_text)
    if match is None:
        return None
    body = match.group(1).strip()
    if not body:
        return MigrationError("Migrations line has nothing after it; write ids or 'none'")
    if body == "none":
        return ()
    names = tuple(part.strip() for part in body.split(","))
    return validate_migrations(names) or names


def migrate_requirement_met(declared: tuple[str, ...], result: str | None) -> bool:
    """Result is 'pass', 'fail', 'skip' or None for absent. Declared migrations need 'pass'; none declared accepts any."""
    if declared:
        return result == "pass"
    return True
