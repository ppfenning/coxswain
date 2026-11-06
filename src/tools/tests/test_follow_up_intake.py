from agent_tools import run_store
from agent_tools.follow_up_intake import file_follow_up, follow_up_intake_file

HOME = "/home/user"
_FOLLOW_UP = {
    "repo": "~/repos/other",
    "title": "Add the widget",
    "body": "Do it.",
    "tasks": ["t1"],
    "lint": "Run ruff.",
}


def test_the_intake_file_is_exact_for_one_follow_up():
    assert follow_up_intake_file(_FOLLOW_UP, after="init-a", date="2026-11-06", home=HOME) == {
        "intake/2026-11-06-add-the-widget.md": (
            "---\nid: add-the-widget\ntitle: Add the widget\nrepo: ~/repos/other\nafter: [init-a]\n---\n\n"
            "lint: Run ruff.\n\nDo it.\n"
        )
    }


def test_after_is_listed_and_a_tilde_repo_stays_in_tilde_form():
    absolute = {**_FOLLOW_UP, "repo": "/home/user/repos/other"}
    for follow_up in (_FOLLOW_UP, absolute):
        (text,) = follow_up_intake_file(follow_up, after="init-a", date="2026-11-06", home=HOME).values()
        assert "after: [init-a]\n" in text
        assert "repo: ~/repos/other\n" in text


def test_filing_twice_leaves_one_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)
    kwargs = {"after": "init-a", "date": "2026-11-06", "home": HOME}

    first = file_follow_up(tmp_path / "runs", tmp_path, _FOLLOW_UP, **kwargs)
    second = file_follow_up(tmp_path / "runs", tmp_path, _FOLLOW_UP, **kwargs)

    assert first == second == "intake/2026-11-06-add-the-widget.md"
    assert [p.name for p in (tmp_path / "intake").glob("*.md")] == ["2026-11-06-add-the-widget.md"]
