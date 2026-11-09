from pathlib import Path

import yaml

from agent_tools.chair_apply_release import apply_release, release_ticket

DAY = "2026-11-09"


def _text(state: str) -> str:
    return f"---\nid: t1\ntitle: Plain title\nstate: {state}\nsurfaces: [a.py]\n---\nBody.\n"


def test_an_approved_ticket_becomes_ready_with_a_changed_body() -> None:
    new, refusal = release_ticket(_text("approved"), "stuck", DAY)
    assert refusal is None
    assert new == _text("ready").replace("Body.\n", "Body.\n\nReleased from approval on 2026-11-09: stuck. Rebuild on the current main.\n")


def test_an_already_ready_ticket_is_returned_unchanged() -> None:
    assert release_ticket(_text("ready"), "stuck", DAY) == (_text("ready"), None)


def test_the_note_carries_the_reason_and_the_day() -> None:
    new, _ = release_ticket(_text("done"), "steer deadlock", DAY)
    assert new is not None and "Released from approval on 2026-11-09: steer deadlock." in new


def test_a_colon_in_the_reason_leaves_the_frontmatter_valid() -> None:
    new, _ = release_ticket(_text("approved"), "blocked: needs rebase", DAY)
    assert new is not None
    header = yaml.safe_load(new.split("\n---\n")[0].removeprefix("---\n"))
    assert header == {"id": "t1", "title": "Plain title", "state": "ready", "surfaces": ["a.py"]}
    assert "blocked: needs rebase." in new


def test_a_blocked_ticket_is_refused_with_its_state() -> None:
    assert release_ticket(_text("blocked"), "stuck", DAY) == (None, "release: work item state is 'blocked', not releasing")


def test_the_edge_rewrites_each_file_and_refuses_a_missing_ticket(tmp_path: Path) -> None:
    for name in ("t1", "t2"):
        path = tmp_path / "work" / "i" / "p1" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_text("approved"), encoding="utf-8")
    assert apply_release(tmp_path, "i", ["t1", "t2"], "stuck", DAY) == (True, "released t1, t2")
    for name in ("t1", "t2"):
        text = (tmp_path / "work" / "i" / "p1" / f"{name}.md").read_text(encoding="utf-8")
        assert "state: ready\n" in text and "Released from approval on 2026-11-09: stuck." in text
    assert apply_release(tmp_path, "i", ["nope"], "stuck", DAY) == (False, "ticket not found")
