"""The queue directory intake source: filename order, optional frontmatter, and
a consume step that only ever moves a file, never deletes one."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.intake import IntakeError, consume, read_queue


def write(root: Path, name: str, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def test_reading_order_follows_filename_not_creation_order(tmp_path: Path) -> None:
    """A copy that reorders mtimes must not reorder the queue."""
    root = tmp_path / "intake"
    write(root, "002-second.md", "second\n")
    write(root, "001-first.md", "first\n")
    items = read_queue(root)
    assert [item["id"] for item in items] == ["001-first", "002-second"]


def test_frontmatter_kind_and_title_are_parsed(tmp_path: Path) -> None:
    root = tmp_path / "intake"
    write(root, "idea.md", "---\nkind: bug\ntitle: Login is broken\n---\n\nDetails here.\n")
    item = read_queue(root)[0]
    assert item["kind"] == "bug"
    assert item["title"] == "Login is broken"
    assert item["body"] == "Details here."


def test_a_bare_file_gets_defaults_and_the_full_text_as_body(tmp_path: Path) -> None:
    """A queue that rejects a note with no frontmatter is a queue nobody uses."""
    root = tmp_path / "intake"
    write(root, "note.md", "Just an idea, jotted down.\n")
    item = read_queue(root)[0]
    assert item["kind"] == "idea"
    assert item["title"] == "note"
    assert item["body"] == "Just an idea, jotted down."


def test_unclosed_frontmatter_raises_naming_the_file(tmp_path: Path) -> None:
    root = tmp_path / "intake"
    bad = write(root, "broken.md", "---\nkind: idea\nno closing fence\n")
    with pytest.raises(IntakeError, match="broken.md"):
        read_queue(root)


def test_invalid_yaml_frontmatter_raises_naming_the_file(tmp_path: Path) -> None:
    root = tmp_path / "intake"
    bad = write(root, "bad-yaml.md", "---\nkind: [unclosed\n---\n\nbody\n")
    with pytest.raises(IntakeError, match="bad-yaml.md"):
        read_queue(root)


def test_missing_root_returns_empty_list(tmp_path: Path) -> None:
    assert read_queue(tmp_path / "no-such-dir") == []


def test_consuming_removes_an_item_from_the_queue(tmp_path: Path) -> None:
    root = tmp_path / "intake"
    write(root, "idea.md", "An idea.\n")
    [item] = read_queue(root)
    consume(Path(item["path"]))
    assert read_queue(root) == []


def test_consumed_item_lands_in_consumed_with_content_intact(tmp_path: Path) -> None:
    root = tmp_path / "intake"
    path = write(root, "idea.md", "---\nkind: bug\ntitle: Thing\n---\n\nThe body.\n")
    destination = consume(path)
    assert destination == root / "consumed" / "idea.md"
    assert destination.is_file()
    assert "The body." in destination.read_text(encoding="utf-8")


def test_double_consume_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "intake"
    path = write(root, "idea.md", "An idea.\n")
    consume(path)
    # Simulate a second run finding the same filename queued again.
    again = write(root, "idea.md", "A different idea, same name.\n")
    with pytest.raises(IntakeError, match="already exists"):
        consume(again)


def test_consume_returns_the_new_path(tmp_path: Path) -> None:
    root = tmp_path / "intake"
    path = write(root, "idea.md", "An idea.\n")
    result = consume(path)
    assert result == root / "consumed" / "idea.md"
    assert not path.exists()
