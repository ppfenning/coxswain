"""`route edit --body` and `--body-file` on a queued intake refuse a body the decompose would refuse for reach."""
import pytest

from agent_tools import route_edit, run_store
from graphs.delivery.ticket_lint import idea_reach_refusal

REACHING = "Ship it with ~/deploy/release.sh once the tests pass."
PLAIN = "Fix the parser in `src/parser.py` so a short row returns None."


def _intake(extra: str = "", body: str = "Old body") -> str:
    return f"---\nid: fix-it\ntitle: Old title\nrepo: r\n{extra}---\n\n{body}\n"


def _setup(tmp_path, monkeypatch, text):
    row = {"kind": "intake", "initiative": "intake", "task_id": "fix-it", "state": "queued", "extra": {}}
    upserts = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: [row])
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: upserts.append(row) or True)
    path = tmp_path / "intake" / "fix-it.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path, upserts


def _edit(tmp_path, **kwargs):
    return route_edit.apply_edit(tmp_path, tmp_path, "fix-it", live_runs=(), **kwargs)


def test_a_body_naming_a_home_path_is_refused_with_the_decompose_reason_and_nothing_is_written(tmp_path, monkeypatch):
    path, upserts = _setup(tmp_path, monkeypatch, _intake())
    result = _edit(tmp_path, body=REACHING)
    assert result == route_edit.EditResult(idea_reach_refusal(REACHING, [], ""), route_edit.REFUSED)
    assert path.read_text(encoding="utf-8") == _intake()
    assert upserts == []


def test_a_dry_run_is_refused_too(tmp_path, monkeypatch):
    path, _ = _setup(tmp_path, monkeypatch, _intake())
    result = _edit(tmp_path, body=REACHING, dry_run=True)
    assert result.code == route_edit.REFUSED
    assert result.text == idea_reach_refusal(REACHING, [], "")
    assert path.read_text(encoding="utf-8") == _intake()


def test_a_reach_allow_entry_in_the_intake_lets_the_edit_apply(tmp_path, monkeypatch):
    path, upserts = _setup(tmp_path, monkeypatch, _intake("reach_allow: [~/deploy]\n"))
    assert idea_reach_refusal(REACHING, [], "", ["~/deploy"]) is None
    result = _edit(tmp_path, body=REACHING)
    assert result.code == route_edit.DONE
    assert path.read_text(encoding="utf-8") == _intake("reach_allow: [~/deploy]\n", REACHING)
    assert len(upserts) == 1


def test_a_body_with_no_rooted_path_is_applied(tmp_path, monkeypatch):
    path, _ = _setup(tmp_path, monkeypatch, _intake())
    result = _edit(tmp_path, body=PLAIN)
    assert result.code == route_edit.DONE
    assert path.read_text(encoding="utf-8") == _intake(body=PLAIN)


@pytest.mark.parametrize(
    ("extra", "refused"),
    [("", True), ("reach_allow: [~/deploy]\n", False)],
)
def test_body_file_takes_the_same_route_as_body(extra, refused, tmp_path, monkeypatch):
    path, _ = _setup(tmp_path, monkeypatch, _intake(extra))
    body_file = tmp_path / "body.txt"
    body_file.write_text(REACHING + "\n", encoding="utf-8")
    result = _edit(tmp_path, body_file=str(body_file))
    if refused:
        assert result == route_edit.EditResult(idea_reach_refusal(REACHING, [], ""), route_edit.REFUSED)
        assert path.read_text(encoding="utf-8") == _intake(extra)
    else:
        assert result.code == route_edit.DONE
        assert path.read_text(encoding="utf-8") == _intake(extra, REACHING)


def test_an_edit_that_gives_no_body_is_not_checked(tmp_path, monkeypatch):
    path, _ = _setup(tmp_path, monkeypatch, _intake(body=REACHING))
    result = _edit(tmp_path, title="New title")
    assert result.code == route_edit.DONE
    assert path.read_text(encoding="utf-8") == _intake(body=REACHING).replace("Old title", "New title")
