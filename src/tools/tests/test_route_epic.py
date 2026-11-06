"""`route_epic` core: the rendered file text, the validator, and the edge that writes one epic definition."""

from pathlib import Path

import pytest

from agent_tools import route_epic
from core.epic_files import parse_epic, read_epic_definitions


def test_render_id_only():
    assert route_epic.render_epic("alpha", None, None) == "---\nid: alpha\n---\n\n"


def test_render_id_and_title():
    assert route_epic.render_epic("alpha", "Alpha epic", None) == "---\nid: alpha\ntitle: Alpha epic\n---\n\n"


def test_render_id_title_and_branch():
    text = route_epic.render_epic("alpha", "Alpha epic", "epic/alpha")
    assert text == "---\nid: alpha\ntitle: Alpha epic\ntarget_branch: epic/alpha\n---\n\n"


@pytest.mark.parametrize("title", ["route.py: initiative_files", 'say "hi" # now', "[draft] ship it", " padded "])
def test_awkward_title_round_trips_through_parse_epic(title):
    text = route_epic.render_epic("alpha", title, "epic/alpha")
    assert parse_epic(text, "alpha.md") == {"id": "alpha", "title": title, "target_branch": "epic/alpha"}


def test_invalid_reason_accepts_good_input_and_names_bad_input():
    assert route_epic.invalid_reason("alpha", None) is None
    assert route_epic.invalid_reason("alpha", "epic/alpha") is None
    assert "lowercase slug" in route_epic.invalid_reason("Not An Id", None)
    assert "valid branch name" in route_epic.invalid_reason("alpha", "bad branch")


def test_invalid_id_is_refused_and_writes_nothing(tmp_path: Path):
    code, message = route_epic.create_epic(tmp_path, "Bad_Id", None, None, False)
    assert (code, "lowercase slug" in message) == (route_epic.REFUSED, True)
    assert not (tmp_path / "epics").exists()


def test_invalid_branch_is_refused_and_writes_nothing(tmp_path: Path):
    code, message = route_epic.create_epic(tmp_path, "alpha", None, "has space", False)
    assert (code, "valid branch name" in message) == (route_epic.REFUSED, True)
    assert not (tmp_path / "epics").exists()


def test_existing_file_is_refused_and_left_unchanged(tmp_path: Path):
    path = tmp_path / "epics" / "alpha.md"
    path.parent.mkdir()
    path.write_text("---\nid: alpha\ntitle: Hand written\n---\nProse\n")
    code, message = route_epic.create_epic(tmp_path, "alpha", "New title", None, False)
    assert (code, "already exists" in message) == (route_epic.REFUSED, True)
    assert path.read_text() == "---\nid: alpha\ntitle: Hand written\n---\nProse\n"


def test_missing_directory_is_created_and_file_reads_back(tmp_path: Path):
    code, message = route_epic.create_epic(tmp_path, "alpha", "Alpha: the first", "epic/alpha", False)
    assert code == route_epic.DONE and str(tmp_path / "epics" / "alpha.md") in message
    assert (
        tmp_path / "epics" / "alpha.md"
    ).read_text() == '---\nid: alpha\ntitle: "Alpha: the first"\ntarget_branch: epic/alpha\n---\n\n'
    assert read_epic_definitions(tmp_path) == {
        "alpha": {"id": "alpha", "title": "Alpha: the first", "target_branch": "epic/alpha"}
    }


def test_dry_run_returns_the_text_and_writes_nothing(tmp_path: Path):
    code, message = route_epic.create_epic(tmp_path, "alpha", "Alpha", None, True)
    assert (code, message) == (route_epic.DONE, "---\nid: alpha\ntitle: Alpha\n---\n\n")
    assert not (tmp_path / "epics").exists()


UNSAFE_TITLES = ["123", "true", "null", "~", "2026-01-01", "a\nb", "a\ntarget_branch: x", "a\n---\nb"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("title", UNSAFE_TITLES)
def test_title_that_would_not_read_back_is_refused_and_writes_nothing(tmp_path: Path, title: str, dry_run: bool):
    code, message = route_epic.create_epic(tmp_path, "alpha", title, None, dry_run)
    assert (code, "would" in message) == (route_epic.REFUSED, True)
    assert not (tmp_path / "epics").exists()


@pytest.mark.parametrize("branch", ["123", "true", "null"])
def test_branch_that_would_not_read_back_is_refused_and_writes_nothing(tmp_path: Path, branch: str):
    code, _ = route_epic.create_epic(tmp_path, "alpha", None, branch, False)
    assert code == route_epic.REFUSED
    assert not (tmp_path / "epics").exists()


@pytest.mark.parametrize("epic_id", ["123", "true", "null"])
def test_id_that_would_not_read_back_is_refused_and_writes_nothing(tmp_path: Path, epic_id: str):
    code, _ = route_epic.create_epic(tmp_path, epic_id, None, None, False)
    assert code == route_epic.REFUSED
    assert not (tmp_path / "epics").exists()


def test_unreadable_reason_names_a_bare_numeric_title():
    assert route_epic.unreadable_reason("---\nid: alpha\ntitle: 123\n---\n\n", "alpha", "123", None) is not None


def test_malformed_sibling_does_not_fail_a_correct_write(tmp_path: Path):
    (tmp_path / "epics").mkdir()
    (tmp_path / "epics" / "broken.md").write_text("no frontmatter\n")
    code, _ = route_epic.create_epic(tmp_path, "alpha", "Alpha", None, False)
    assert code == route_epic.DONE
    assert parse_epic((tmp_path / "epics" / "alpha.md").read_text(), "alpha.md")["title"] == "Alpha"
