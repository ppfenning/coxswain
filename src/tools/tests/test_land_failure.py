import json
import re

from agent_tools.land_failure import PREFIX, failure_line, failure_of, first_error_line

_LINE_SEPARATOR = chr(0x2028)


def _fields(line: str) -> dict[str, object]:
    """Read labelled fields back the way the chair would: bare ints, JSON strings."""
    pairs = re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|-?\d+)(?= |$)', line.removeprefix(PREFIX))
    return {label: json.loads(raw) for label, raw in pairs}


def test_banner_line_is_skipped_for_the_git_error():
    assert first_error_line("forge: github\nfatal: not a git repository\n") == "fatal: not a git repository"


def test_progress_lines_are_skipped_for_the_error():
    assert first_error_line("Pushing to origin...\nEnumerating objects: 5, done.\nerror: failed to push") == (
        "error: failed to push"
    )


def test_blank_only_and_banner_only_text_give_empty_string():
    assert first_error_line("\n  \n") == ""
    assert first_error_line("forge: github\n") == ""


def test_absent_command_and_code_are_left_out():
    line = failure_line("merge", "github", "", None, "boom")
    assert line == 'land failed: step="merge" forge="github" error="boom"'
    assert "None" not in line
    assert "command=" not in line
    assert "code=" not in line


def test_round_trip_with_every_field_reads_back_each_value():
    line = failure_line("merge", "github", "gh pr merge 7", 1, "forge: github\nfatal: nope")
    output = f"pushing\n{line}\nland failed: later\n"
    assert failure_of(output) == line
    assert _fields(failure_of(output) or "") == {
        "step": "merge",
        "forge": "github",
        "command": "gh pr merge 7",
        "code": 1,
        "error": "fatal: nope",
    }


def test_newlines_in_any_field_keep_one_line_and_survive_the_round_trip():
    command = 'git commit -m "a\nb"; code=9\r\nland failed: fake'
    forge = f"git{_LINE_SEPARATOR}hub"
    line = failure_line("mer\nge", forge, command, 0, "x\ny")
    assert len(line.splitlines()) == 1
    assert failure_of(f"before\n{line}\nafter") == line
    assert _fields(line) == {"step": "mer\nge", "forge": forge, "command": command, "code": 0, "error": "x"}


def test_indented_failure_line_is_found_and_returned_stripped():
    line = failure_line("merge", "github", "", 2, "boom")
    assert failure_of(f"log\n    {line}  \n") == line


def test_output_without_the_prefix_gives_none():
    assert failure_of("pushing\nall good\n") is None
    assert failure_of("") is None
