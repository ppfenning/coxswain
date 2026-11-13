from pathlib import Path

import pytest
import yaml

from agent_tools.chair_apply_restore import apply_restore, restore_ticket

DAY = "2026-11-10"


def _text(state: str) -> str:
    return f"---\nid: t1\ntitle: Plain title\nstate: {state}\nsurfaces: [a.py]\n---\nBody.\n"


def _write(tmp_path: Path, name: str, state: str) -> Path:
    path = tmp_path / "work" / "i" / "p1" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_text(state), encoding="utf-8")
    return path


def test_a_ready_ticket_becomes_approved_with_the_note() -> None:
    new, refusal = restore_ticket(_text("ready"), "hold released it wrongly.", DAY)
    assert refusal is None
    assert new == _text("approved").replace("Body.\n", "Body.\n\nRestored to approved on 2026-11-10: hold released it wrongly.\n")


def test_an_already_approved_ticket_is_returned_unchanged() -> None:
    assert restore_ticket(_text("approved"), "why", DAY) == (_text("approved"), None)


def test_a_colon_in_the_reason_leaves_the_frontmatter_valid() -> None:
    new, _ = restore_ticket(_text("ready"), "blocked: wrong release", DAY)
    assert new is not None
    header = yaml.safe_load(new.split("\n---\n")[0].removeprefix("---\n"))
    assert header["state"] == "approved"


def test_a_done_ticket_is_refused_with_its_state() -> None:
    assert restore_ticket(_text("done"), "why", DAY) == (None, "restore: work item state is 'done', not restoring")


@pytest.mark.parametrize("reason,day", [("a\nb", DAY), ("why", "2026\n11"), ("", DAY), ("why", " ")])
def test_a_reason_or_day_that_is_empty_or_multiline_is_refused(reason: str, day: str) -> None:
    assert restore_ticket(_text("ready"), reason, day) == (None, "restore: reason and day must be non-empty single lines")


def test_the_edge_rewrites_a_ready_ticket(tmp_path: Path) -> None:
    path = _write(tmp_path, "t1", "ready")
    assert apply_restore(tmp_path, "i", ["t1"], "why", DAY) == (True, "restored t1")
    assert "state: approved\n" in path.read_text(encoding="utf-8")


def test_the_edge_refuses_a_done_ticket_and_changes_no_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "t1", "done")
    assert apply_restore(tmp_path, "i", ["t1"], "why", DAY) == (False, "restore: work item state is 'done', not restoring (t1)")
    assert path.read_text(encoding="utf-8") == _text("done")


def test_one_refusal_in_a_multi_ticket_action_writes_nothing(tmp_path: Path) -> None:
    first, second = _write(tmp_path, "t1", "ready"), _write(tmp_path, "t2", "done")
    ok, _ = apply_restore(tmp_path, "i", ["t1", "t2"], "why", DAY)
    assert ok is False
    assert (first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8")) == (_text("ready"), _text("done"))


def test_the_edge_refuses_a_missing_ticket_and_an_empty_action(tmp_path: Path) -> None:
    assert apply_restore(tmp_path, "i", ["nope"], "why", DAY) == (False, "ticket not found")
    assert apply_restore(tmp_path, "i", [], "why", DAY) == (False, "restore: no ticket named")
