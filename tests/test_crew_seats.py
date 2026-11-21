"""crew/check_seats.py's rules, over literal seat texts and over the real crew/seats, and crew/install.sh's rendering."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crew"))

import check_seats  # noqa: E402

from core.seats import RETIRED_SEATS, SEAT_ALIASES  # noqa: E402


def _seat(name: str, body: str = "You are the build seat. The voice is decoration, set in SEATS.md.\n") -> str:
    return f"---\nname: {name}\ndescription: |\n  Build.\ntools: Bash, Read\nmodel: opus\n---\n\n{body}"


SEATS_MD = """# The seat contract

## The voices

| Seat | Voice | Spoken name |
|---|---|---|
| builder | `af_sarah` | Sarah |
| reviewer | `bm_lewis` | Lewis |

## Old names (deprecated)

| Old name | Seat |
|---|---|
| sarah | builder |
| liam | retired: courier events are typed |

## The brief every seat writes
"""

CLEAN = {"builder": _seat("builder"), "reviewer": _seat("reviewer")}


def test_a_clean_roster_has_no_problems() -> None:
    assert check_seats.problems(CLEAN, SEATS_MD) == []


def test_the_tables_parse_by_heading() -> None:
    assert check_seats.voices(SEATS_MD) == {"builder": ("af_sarah", "Sarah"), "reviewer": ("bm_lewis", "Lewis")}
    assert check_seats.aliases(SEATS_MD) == {"sarah": "builder", "liam": "retired: courier events are typed"}


def test_a_stem_that_differs_from_its_frontmatter_name_is_refused() -> None:
    assert check_seats.problems({**CLEAN, "builder": _seat("build")}, SEATS_MD) == [
        "builder.md: name 'build' does not match the filename"
    ]


@pytest.mark.parametrize("stem", ["sarah", "liam", "lewis"])  # a declared alias, a retired alias, a spoken name
def test_a_person_name_as_a_seat_stem_is_refused(stem: str) -> None:
    found = check_seats.problems({**CLEAN, stem: _seat(stem)}, SEATS_MD)
    expected = (f"{stem}.md: '{stem}' is a person name; seats are named by function, and a person name may only "
                "appear as a declared alias in SEATS.md")
    assert expected in found


def test_a_seat_with_no_row_in_the_voice_table_is_refused() -> None:
    assert check_seats.problems({**CLEAN, "ops": _seat("ops")}, SEATS_MD) == [
        "ops.md: has no row in the SEATS.md voice table"
    ]


def test_a_voice_row_or_an_alias_naming_no_seat_file_is_refused() -> None:
    assert check_seats.problems({"reviewer": _seat("reviewer")}, SEATS_MD) == [
        "SEATS.md: the voice table names 'builder', which has no seat file",
        "SEATS.md: alias 'sarah' resolves to 'builder', which has no seat file",
    ]


def test_a_prompt_naming_its_voice_or_spoken_name_is_refused() -> None:
    body = "In a voice session you are **Sarah**, in the `af_sarah` voice. The voice is decoration.\n"
    assert check_seats.problems({**CLEAN, "builder": _seat("builder", body)}, SEATS_MD) == [
        "builder.md: names the voice 'af_sarah' (builder); voices live only in the SEATS.md table",
        "builder.md: names the spoken name 'Sarah' (builder); no prompt may depend on it",
    ]


def test_frontmatter_gaps_and_an_inherited_model_are_refused() -> None:
    text = "---\nname: builder\ntools: Bash\nmodel: inherit\n---\nThe voice is decoration.\n"
    assert check_seats.problems({**CLEAN, "builder": text}, SEATS_MD) == [
        "builder.md: frontmatter has no 'description'",
        "builder.md: model must be explicit, never 'inherit'",
    ]
    assert check_seats.problems({**CLEAN, "builder": "no frontmatter; the voice is decoration"}, SEATS_MD) == [
        "builder.md: no frontmatter block"
    ]


def test_a_seat_that_does_not_say_the_voice_is_optional_is_refused() -> None:
    assert check_seats.problems({**CLEAN, "builder": _seat("builder", "You build.\n")}, SEATS_MD) == [
        "builder.md: does not say the voice is optional"
    ]


def test_the_real_crew_seats_pass_with_the_function_named_roster() -> None:
    seats_dir = ROOT / "crew" / "seats"
    texts = {p.stem: p.read_text(encoding="utf-8") for p in seats_dir.glob("*.md") if p.name != "SEATS.md"}
    seats_md = (seats_dir / "SEATS.md").read_text(encoding="utf-8")
    assert check_seats.problems(texts, seats_md) == []
    assert set(texts) == {"builder", "reviewer", "recon", "ops", "planner", "writer", "retro", "triage"}
    assert set(check_seats.voices(seats_md)) == set(texts)
    table = check_seats.aliases(seats_md)
    # SEATS.md's old-names table and the loader's aliases are one list, written twice.
    assert {old: seat for old, seat in table.items() if not seat.startswith("retired")} == dict(SEAT_ALIASES)
    assert {old for old, seat in table.items() if seat.startswith("retired")} == set(RETIRED_SEATS)


def _install(prefix: Path, *args: str) -> str:
    script = ROOT / "crew" / "install.sh"
    run = subprocess.run(["bash", str(script), "--prefix", str(prefix), *args], capture_output=True, text=True,
                         check=True, env={**os.environ, "HOME": str(prefix.parent)})
    return run.stdout


def test_install_renders_the_function_names_and_removes_only_links_an_old_name_left(tmp_path: Path) -> None:
    prefix = tmp_path / "agents"
    prefix.mkdir()
    seats = ROOT / "crew" / "seats"
    (prefix / "sarah.md").symlink_to(seats / "sarah.md")  # what the last release's install left
    (prefix / "mine.md").symlink_to(tmp_path / "gone.md")  # dangling, but not ours
    assert "sarah          stale" in _install(prefix, "--check")
    assert "removed" in _install(prefix)
    state = {p.name: os.readlink(p) for p in prefix.iterdir()}
    assert "removed" not in _install(prefix)  # a second install changes nothing
    assert {p.name: os.readlink(p) for p in prefix.iterdir()} == state
    assert sorted(state) == sorted(
        ["mine.md", "builder.md", "ops.md", "planner.md", "recon.md", "retro.md", "reviewer.md", "triage.md",
         "writer.md"])
    assert all((prefix / f"{seat}.md").resolve() == seats / f"{seat}.md" for seat in check_seats.voices(
        (seats / "SEATS.md").read_text(encoding="utf-8")))
