from agent_tools import route_edit, run_store

INTAKE = "---\nid: fix-it\ntitle: Old title\nrepo: r\n---\n\nOld body\n"
WITH_AFTER = "---\nid: fix-it\ntitle: Old title\nafter: [x, y]\nrepo: r\n---\n\nOld body\n"


def _queue(monkeypatch, rows):
    upserts = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: rows)
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: upserts.append(row) or True)
    return upserts


def _intake_row():
    return {"kind": "intake", "initiative": "intake", "task_id": "fix-it", "state": "queued", "extra": {}}


def _workspace(tmp_path, text):
    path = tmp_path / "intake" / "fix-it.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def _edit(tmp_path, **kwargs):
    return route_edit.apply_edit(tmp_path, tmp_path, "fix-it", live_runs=(), **kwargs)


def test_set_two_ids_writes_a_list_in_the_order_given(tmp_path, monkeypatch):
    _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, INTAKE)
    result = _edit(tmp_path, after=["b", "a"])
    assert result.code == 0
    assert "+after: [b, a]\n" in result.text
    assert path.read_text(encoding="utf-8") == INTAKE.replace("repo: r\n", "repo: r\nafter: [b, a]\n")


def test_replace_swaps_the_list_in_place(tmp_path, monkeypatch):
    _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, WITH_AFTER)
    result = _edit(tmp_path, after=["z"])
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == WITH_AFTER.replace("[x, y]", "[z]")


def test_empty_list_clears_the_key(tmp_path, monkeypatch):
    _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, WITH_AFTER)
    result = _edit(tmp_path, after=[])
    assert result.code == 0
    assert "-after: [x, y]\n" in result.text
    assert path.read_text(encoding="utf-8") == INTAKE


def test_clearing_an_absent_key_is_a_no_op(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, INTAKE)
    assert _edit(tmp_path, after=[]) == ("", 0)
    assert path.read_text(encoding="utf-8") == INTAKE
    assert upserts == []


def test_task_ticket_is_refused_with_one_line(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [{"kind": "task", "initiative": "ship", "task_id": "t1", "state": "ready"}])
    result = route_edit.apply_edit(tmp_path, tmp_path, "t1", after=["a"], live_runs=())
    assert result == ("--after applies to a queued intake, not a task ticket", 2)
    assert upserts == []


def test_initiative_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [{"kind": "task", "initiative": "ship", "task_id": "ship", "state": "ready"}])
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", after=["a"], live_runs=())
    assert result == ("--after applies to a queued intake, not an initiative", 2)
    assert upserts == []


def test_self_dependency_is_refused(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, INTAKE)
    result = _edit(tmp_path, after=["a", " fix-it "])
    assert result == ("'fix-it' cannot come after itself", 2)
    assert path.read_text(encoding="utf-8") == INTAKE
    assert upserts == []


def test_dry_run_prints_the_after_line_and_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, INTAKE)
    result = _edit(tmp_path, after=["a"], dry_run=True)
    assert result.code == 0
    assert "+after: [a]\n" in result.text
    assert path.read_text(encoding="utf-8") == INTAKE
    assert upserts == []


def test_ids_are_stripped_and_deduplicated():
    assert route_edit.clean_after([" a ", "b", "a", ""]) == ["a", "b"]
    assert route_edit.plan_edit(INTAKE, after=[" a ", "a"])[0].endswith("after: [a]\n---\n\nOld body\n")


def test_clearing_keeps_other_lines_and_block_lists_byte_for_byte():
    text = "---\nid: fix-it\nafter:\n  - x\n  - y\nattempts:\n  - n: 1\nrepo: r\n---\n\nbody\n"
    assert route_edit.plan_edit(text, after=[])[0] == "---\nid: fix-it\nattempts:\n  - n: 1\nrepo: r\n---\n\nbody\n"


def test_after_alone_counts_as_a_change_and_none_does_not():
    assert route_edit.check_options(None, None, None, None, after=[]) is None
    assert route_edit.check_options(None, None, None, None).startswith("nothing to change")
