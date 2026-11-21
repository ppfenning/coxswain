"""Every seat carries the frontmatter a harness routes on, is named by its function, and has its voice in one table.

The tool allowlist IS the write boundary, and `model: inherit` puts whatever
the main loop happens to be running on a job that did not ask for it. Both are
mechanical, so both are checked mechanically rather than trusted to review.

The roster is named by function, and the voice is decoration kept in one table
in SEATS.md. So a seat's file stem is its frontmatter name, no stem is a
person's name (an old person name may only appear as a declared alias in
SEATS.md), every seat has a row in the voice table, and no seat prompt names
its voice or its spoken name.

`problems` is pure: it takes the seat files' texts by stem and SEATS.md's text,
and returns every problem found. `main` is the edge that reads `seats/`.
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections.abc import Mapping

REQUIRED = ("name", "description", "tools", "model")
VOICES_HEADING = "## The voices"
ALIASES_HEADING = "## Old names"
SEATS_DIR = pathlib.Path(__file__).resolve().parent / "seats"


def _section_rows(seats_md: str, heading: str) -> list[list[str]]:
    """The body rows of the first table under the `heading` line, each as its stripped cells, `**`/backticks removed."""
    lines = seats_md.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return []
    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip().strip("*`").strip() for c in line.strip().strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return rows[1:] if rows else []  # the first row is the header


def voices(seats_md: str) -> dict[str, tuple[str, str]]:
    """The voice table: seat -> (voice id, spoken name)."""
    return {row[0]: (row[1], row[2]) for row in _section_rows(seats_md, VOICES_HEADING) if len(row) >= 3}


def aliases(seats_md: str) -> dict[str, str]:
    """The old-names table: old name -> the seat it resolves to, or the cell's text when the seat retired."""
    return {row[0]: row[1] for row in _section_rows(seats_md, ALIASES_HEADING) if len(row) >= 2}


def _frontmatter(stem: str, text: str) -> tuple[str | None, list[str]]:
    if not text.startswith("---\n"):
        return None, [f"{stem}.md: no frontmatter block"]
    front = text.split("---\n", 2)[1]
    keys = {line.split(":", 1)[0] for line in front.splitlines() if line and not line.startswith((" ", "\t", "-"))}
    found = [f"{stem}.md: frontmatter has no '{key}'" for key in REQUIRED if key not in keys]
    if "model: inherit" in front:
        found.append(f"{stem}.md: model must be explicit, never 'inherit'")
    return front, found


def _seat_problems(stem: str, text: str, person_names: frozenset[str], voiced: Mapping[str, tuple[str, str]]) -> list[str]:
    front, found = _frontmatter(stem, text)
    if front is not None:
        name = next((line.split(":", 1)[1].strip() for line in front.splitlines() if line.startswith("name:")), "")
        if name != stem:
            found.append(f"{stem}.md: name '{name}' does not match the filename")
    if stem.lower() in person_names:
        found.append(f"{stem}.md: '{stem}' is a person name; seats are named by function, and a person name "
                     "may only appear as a declared alias in SEATS.md")
    if stem not in voiced:
        found.append(f"{stem}.md: has no row in the SEATS.md voice table")
    if "voice is" not in text and "In a text session" not in text:
        found.append(f"{stem}.md: does not say the voice is optional")
    for seat, (voice, spoken) in voiced.items():
        if voice and voice.lower() != "none" and voice in text:
            found.append(f"{stem}.md: names the voice '{voice}' ({seat}); voices live only in the SEATS.md table")
        if spoken and re.search(rf"\b{re.escape(spoken)}\b", text):
            found.append(f"{stem}.md: names the spoken name '{spoken}' ({seat}); no prompt may depend on it")
    return found


def problems(seats: Mapping[str, str], seats_md: str) -> list[str]:
    """Every problem in the roster: `seats` maps each seat file's stem to its text, `seats_md` is SEATS.md's text."""
    voiced = voices(seats_md)
    declared = aliases(seats_md)
    person_names = frozenset({*(old.lower() for old in declared), *(spoken.lower() for _, spoken in voiced.values())})
    found = [p for stem in sorted(seats) for p in _seat_problems(stem, seats[stem], person_names, voiced)]
    found += [f"SEATS.md: the voice table names '{seat}', which has no seat file" for seat in voiced if seat not in seats]
    found += [
        f"SEATS.md: alias '{old}' resolves to '{seat}', which has no seat file"
        for old, seat in declared.items()
        if not seat.startswith("retired") and seat not in seats
    ]
    return found


def main(seats_dir: pathlib.Path = SEATS_DIR) -> int:
    """Edge: read every seat file and SEATS.md under `seats_dir`, print the problems or the count, return the exit code."""
    texts = {p.stem: p.read_text(encoding="utf-8") for p in sorted(seats_dir.glob("*.md")) if p.name != "SEATS.md"}
    found = problems(texts, (seats_dir / "SEATS.md").read_text(encoding="utf-8"))
    print("\n".join(found) if found else f"{len(texts)} seats OK")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
