"""Epic definition files."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.epic_files import parse_epic, read_epic_definitions
from core.epics import rollup
from core.workstore import WorkStoreError


def test_valid_file_returns_all_three_keys() -> None:
    text = "---\nid: payments\ntitle: Payments rework\ntarget_branch: release/payments\n---\nbody\n"
    assert parse_epic(text, "payments.md") == {
        "id": "payments",
        "title": "Payments rework",
        "target_branch": "release/payments",
    }


def test_absent_keys_default() -> None:
    assert parse_epic("---\n---\n", "payments.md") == {"id": "payments", "title": None, "target_branch": None}


def test_id_that_disagrees_with_the_file_name_is_refused() -> None:
    with pytest.raises(ValueError, match="must equal the file name"):
        parse_epic("---\nid: other\n---\n", "payments.md")


def test_bad_branch_is_refused() -> None:
    with pytest.raises(ValueError, match="valid branch name"):
        parse_epic("---\ntarget_branch: bad branch\n---\n", "payments.md")


def test_bad_id_is_refused() -> None:
    with pytest.raises(ValueError, match="lowercase slug"):
        parse_epic("---\n---\n", "Payments.md")


def test_absent_directory_gives_empty_dict(tmp_path: Path) -> None:
    assert read_epic_definitions(tmp_path) == {}


def test_directory_is_read_and_feeds_rollup(tmp_path: Path) -> None:
    (tmp_path / "epics").mkdir()
    (tmp_path / "epics" / "payments.md").write_text("---\ntitle: Payments rework\n---\n", encoding="utf-8")
    (tmp_path / "epics" / "notes.txt").write_text("ignored", encoding="utf-8")
    definitions = read_epic_definitions(tmp_path)
    assert list(definitions) == ["payments"]
    assert rollup({"alpha": "payments"}, set(), definitions)[0]["title"] == "Payments rework"


BROKEN_FRONTMATTER = {
    "none": "no frontmatter here\n",
    "unclosed": "---\ntitle: x\n",
    "yaml": "---\ntitle: [unclosed\n---\n",
    "mapping": "---\n- a\n- b\n---\n",
}


@pytest.mark.parametrize("text", BROKEN_FRONTMATTER.values(), ids=BROKEN_FRONTMATTER.keys())
def test_broken_frontmatter_is_a_value_error_in_the_core(text: str) -> None:
    with pytest.raises(ValueError):
        parse_epic(text, "payments.md")


@pytest.mark.parametrize("text", BROKEN_FRONTMATTER.values(), ids=BROKEN_FRONTMATTER.keys())
def test_broken_frontmatter_names_the_full_path_at_the_edge(tmp_path: Path, text: str) -> None:
    (tmp_path / "epics").mkdir()
    bad = tmp_path / "epics" / "payments.md"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(WorkStoreError, match=str(bad)):
        read_epic_definitions(tmp_path)


def test_malformed_file_names_its_path(tmp_path: Path) -> None:
    (tmp_path / "epics").mkdir()
    bad = tmp_path / "epics" / "payments.md"
    bad.write_text("---\ntarget_branch: bad branch\n---\n", encoding="utf-8")
    with pytest.raises(WorkStoreError, match=str(bad)):
        read_epic_definitions(tmp_path)


def test_an_epic_file_may_name_its_issue_and_ask_for_a_stack() -> None:
    from core.epic_files import parse_epic

    got = parse_epic("---\ntitle: Payments\nissue: 41\nepic_stack: true\ntarget_branch: epic/payments\n---\n", "payments.md")
    assert (got["issue"], got["epic_stack"], got["target_branch"]) == (41, True, "epic/payments")
    assert "issue" not in parse_epic("---\n---\n", "payments.md") and "epic_stack" not in parse_epic("---\nepic_stack: yes please\n---\n", "payments.md")
