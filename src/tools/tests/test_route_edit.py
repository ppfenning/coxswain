import pytest

from agent_tools import route_edit, run_store

INTAKE = "---\nid: fix-it\ntitle: Old title\nrepo: r\n---\n\nOld body\n"
INITIATIVE = "---\nid: ship\ntitle: Old title\nrepo: r\n---\n\nOld body\n"


def _queue(monkeypatch, rows, stored=True):
    upserts = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: rows)
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: upserts.append(row) or stored)
    return upserts


def _intake_row():
    return {"kind": "intake", "initiative": "intake", "task_id": "fix-it", "state": "queued", "extra": {}}


def _task_row(state):
    return {"kind": "task", "initiative": "ship", "task_id": "ship", "state": state}


def _workspace(tmp_path, relative, text):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_edit_an_intake_rewrites_the_file_and_the_store_row(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", title="New: title", live_runs=())
    assert result.code == 0
    assert '-title: Old title\n+title: "New: title"\n' in result.text
    assert path.read_text(encoding="utf-8") == INTAKE.replace("title: Old title", 'title: "New: title"')
    assert [row["title"] for row in upserts] == ["New: title"]


def test_edit_an_initiative_rewrites_initiative_md(tmp_path, monkeypatch):
    _queue(monkeypatch, [_task_row("ready")])
    path = _workspace(tmp_path, "work/ship/initiative.md", INITIATIVE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", body="New body", repo="other", live_runs=())
    assert result.code == 0
    assert "+++ b/work/ship/initiative.md" in result.text
    assert path.read_text(encoding="utf-8") == "---\nid: ship\ntitle: Old title\nrepo: other\n---\n\nNew body\n"


def test_edit_refused_on_a_live_run_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_task_row("ready")])
    path = _workspace(tmp_path, "work/ship/initiative.md", INITIATIVE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", title="New", live_runs=["R1"])
    assert result.code == 2
    assert "run R1 is live" in result.text
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert upserts == []


def test_dry_run_returns_the_diff_and_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", title="New", dry_run=True, live_runs=())
    assert result.code == 0
    assert "-title: Old title\n+title: New\n" in result.text
    assert path.read_text(encoding="utf-8") == INTAKE
    assert upserts == []


def test_initiative_dry_run_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_task_row("ready")])
    path = _workspace(tmp_path, "work/ship/initiative.md", INITIATIVE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", title="New", dry_run=True, live_runs=())
    assert result.code == 0
    assert "+title: New\n" in result.text
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert upserts == []


def test_live_runs_is_required(tmp_path, monkeypatch):
    _queue(monkeypatch, [_task_row("ready")])
    with pytest.raises(TypeError):
        route_edit.apply_edit(tmp_path, tmp_path, "ship", title="New")


def test_body_file_trailing_newline_adds_no_blank_line(tmp_path, monkeypatch):
    _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    source = tmp_path / "body.txt"
    source.write_text("New body\n", encoding="utf-8")
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", body_file=str(source), live_runs=())
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == INTAKE.replace("Old body", "New body")


def test_store_down_warns_and_still_writes_the_intake_file(tmp_path, monkeypatch, capsys):
    _queue(monkeypatch, [_intake_row()], stored=False)
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", title="New", live_runs=())
    assert result.code == 0
    assert "store unavailable, filing" in capsys.readouterr().out
    assert "title: New" in path.read_text(encoding="utf-8")


ISSUE = "https://github.com/acme/widgets/issues/12"
OTHER_ISSUE = "https://github.com/acme/gadgets/issues/7"
PLAIN = "https://example.com/page"
LINKED = f'---\nid: fix-it\ntitle: Old title\nrepo: r\nlink: "{ISSUE}"\nissue: 12\nissue_repo: acme/widgets\n---\n\nOld body\n'


def _edit_intake(tmp_path, monkeypatch, text, **options):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, "intake/fix-it.md", text)
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", live_runs=(), **options)
    return result, path, upserts


def test_link_set_on_an_intake_updates_the_file_and_the_stored_extra(tmp_path, monkeypatch):
    result, path, upserts = _edit_intake(tmp_path, monkeypatch, INTAKE, link=ISSUE)
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == INTAKE.replace("repo: r\n", f'repo: r\nlink: "{ISSUE}"\nissue: 12\nissue_repo: acme/widgets\n')
    extra = upserts[-1]["extra"]
    assert (extra["link"], extra["issue"], extra["issue_repo"]) == (ISSUE, "12", "acme/widgets")


def test_link_replaced_drops_the_old_issue(tmp_path, monkeypatch):
    result, path, upserts = _edit_intake(tmp_path, monkeypatch, LINKED, link=OTHER_ISSUE)
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == LINKED.replace(ISSUE, OTHER_ISSUE).replace("issue: 12", "issue: 7").replace("acme/widgets\n", "acme/gadgets\n")
    extra = upserts[-1]["extra"]
    assert (extra["link"], extra["issue"], extra["issue_repo"]) == (OTHER_ISSUE, "7", "acme/gadgets")


def test_empty_link_clears_the_three_keys_from_file_and_extra(tmp_path, monkeypatch):
    result, path, upserts = _edit_intake(tmp_path, monkeypatch, LINKED, link="")
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == INTAKE
    assert {"link", "issue", "issue_repo"}.isdisjoint(upserts[-1]["extra"])


def test_non_issue_link_writes_the_link_alone(tmp_path, monkeypatch):
    result, path, upserts = _edit_intake(tmp_path, monkeypatch, LINKED, link=PLAIN)
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == INTAKE.replace("repo: r\n", f'repo: r\nlink: "{PLAIN}"\n')
    extra = upserts[-1]["extra"]
    assert extra["link"] == PLAIN
    assert {"issue", "issue_repo"}.isdisjoint(extra)


def test_link_dry_run_writes_nothing(tmp_path, monkeypatch):
    result, path, upserts = _edit_intake(tmp_path, monkeypatch, INTAKE, link=ISSUE, dry_run=True)
    assert result.code == 0
    assert f'+link: "{ISSUE}"\n+issue: 12\n+issue_repo: acme/widgets\n' in result.text
    assert path.read_text(encoding="utf-8") == INTAKE
    assert upserts == []


def test_link_on_an_initiative_is_refused(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_task_row("ready")])
    path = _workspace(tmp_path, "work/ship/initiative.md", INITIATIVE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", link=ISSUE, live_runs=())
    assert (result.code, result.text) == (2, "--link applies to a queued intake, not an initiative")
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert upserts == []


def test_link_on_a_task_ticket_is_refused(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [{"kind": "task", "initiative": "ship", "task_id": "t1", "state": "ready"}])
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", link=ISSUE, live_runs=())
    assert (result.code, result.text) == (2, "--link applies to a queued intake, not a task ticket")
    assert upserts == []


def test_repeating_the_same_link_returns_an_empty_diff(tmp_path, monkeypatch):
    result, path, upserts = _edit_intake(tmp_path, monkeypatch, LINKED, link=ISSUE)
    assert (result.code, result.text) == (0, "")
    assert path.read_text(encoding="utf-8") == LINKED
    assert upserts == []


def test_a_link_alone_is_a_change_and_the_nothing_message_names_it():
    assert route_edit.check_options(None, None, None, None, link="") is None
    assert route_edit.check_options(None, None, None, None).endswith("--state or --link")
