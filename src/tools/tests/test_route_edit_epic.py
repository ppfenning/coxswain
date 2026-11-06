from agent_tools import queue_export, queue_rows, route_edit, run_store

INITIATIVE = "---\nid: ship\ntitle: Old title\nrepo: r\n---\n\nOld body\n"
WITH_EPIC = "---\nid: ship\ntitle: Old title\nrepo: r\nepic: big-one\n---\n\nOld body\n"
INTAKE = "---\nid: fix-it\ntitle: Old title\nrepo: r\n---\n\nOld body\n"
PHASES = ["a", "b"]


class Store:
    """The run store as the route edit tests fake it, except that an upsert merges into the row it names."""

    def __init__(self, monkeypatch, rows):
        self.rows, self.upserts = rows, []
        monkeypatch.setattr(run_store, "read_queue", self.read_queue)
        monkeypatch.setattr(run_store, "upsert_row", self.upsert_row)

    def read_queue(self, runs_dir, initiative=None, kind=None):
        return [
            row for row in self.rows
            if (initiative is None or row.get("initiative") == initiative) and (kind is None or row.get("kind", "task") == kind)
        ]

    def upsert_row(self, runs_dir, partial):
        self.upserts.append(partial)
        self.rows = [
            {**row, **partial} if (row["initiative"], row["task_id"]) == (partial["initiative"], partial["task_id"]) else row
            for row in self.rows
        ]
        return True

    def initiative_row(self):
        return next(row for row in self.rows if row.get("kind") == "initiative")


def _initiative_row(text):
    parsed = queue_rows.parse_item("initiative", ("ship", "initiative.md"), text)
    return {**parsed, "extra": {**parsed["extra"], "phases": PHASES}}


def _task_row(state="approved"):
    return {"kind": "task", "initiative": "ship", "task_id": "t1", "state": state}


def _setup(tmp_path, monkeypatch, text=INITIATIVE, task_state="approved"):
    store = Store(monkeypatch, [_initiative_row(text), _task_row(task_state)])
    path = tmp_path / "work" / "ship" / "initiative.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return store, path


def _edit(tmp_path, id="ship", live_runs=(), **options):
    return route_edit.apply_edit(tmp_path, tmp_path, id, live_runs=live_runs, **options)


def test_setting_an_epic_writes_the_file_the_stored_extra_and_leaves_no_held_file(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch)
    result = _edit(tmp_path, epic="big-one")
    assert result.code == 0
    assert "+epic: big-one\n" in result.text
    assert path.read_text(encoding="utf-8") == WITH_EPIC
    assert store.initiative_row()["extra"] == {"id": "ship", "repo": "r", "epic": "big-one", "phases": PHASES}
    assert queue_rows.epic_of_row(store.initiative_row()) == "big-one"
    assert queue_export.read_held(tmp_path, store.rows) == []


def test_none_removes_the_epic_from_the_file_and_the_stored_extra_and_keeps_phases(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch, text=WITH_EPIC)
    result = _edit(tmp_path, epic="none")
    assert result.code == 0
    assert "-epic: big-one\n" in result.text
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert store.initiative_row()["extra"] == {"id": "ship", "repo": "r", "phases": PHASES}
    assert queue_export.read_held(tmp_path, store.rows) == []


def test_the_same_epic_twice_is_an_empty_diff_and_writes_nothing(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch, text=WITH_EPIC)
    assert _edit(tmp_path, epic="big-one") == ("", 0)
    assert path.read_text(encoding="utf-8") == WITH_EPIC
    assert store.upserts == []


def test_none_on_an_initiative_without_an_epic_is_an_empty_diff(tmp_path, monkeypatch):
    store, _ = _setup(tmp_path, monkeypatch)
    assert _edit(tmp_path, epic="none") == ("", 0)
    assert store.upserts == []


def test_an_invalid_epic_is_refused_with_the_validators_message_and_writes_nothing(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch)
    result = _edit(tmp_path, epic="Big One")
    assert result.code == 2
    assert "epic id must be a lowercase slug" in result.text
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert store.upserts == []


def test_an_epic_alone_is_a_change_and_the_nothing_message_names_it():
    assert route_edit.check_options(None, None, None, None, epic="big-one") is None
    assert "--epic" in route_edit.check_options(None, None, None, None)


def test_an_epic_only_edit_skips_the_guard_for_a_started_initiative_with_a_live_run(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch, task_state="approved")
    result = _edit(tmp_path, epic="big-one", live_runs=["R1"])
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == WITH_EPIC


def test_an_epic_with_a_title_still_goes_through_the_guard(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch, task_state="approved")
    result = _edit(tmp_path, epic="big-one", title="New", live_runs=["R1"])
    assert result.code == 2
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert store.upserts == []


def test_an_epic_is_refused_on_a_queued_intake(tmp_path, monkeypatch):
    store = Store(monkeypatch, [{"kind": "intake", "initiative": "intake", "task_id": "fix-it", "state": "queued", "extra": {}}])
    path = tmp_path / "intake" / "fix-it.md"
    path.parent.mkdir(parents=True)
    path.write_text(INTAKE, encoding="utf-8")
    result = _edit(tmp_path, id="fix-it", epic="big-one")
    assert result.code == 2
    assert "--epic applies to an initiative" in result.text
    assert path.read_text(encoding="utf-8") == INTAKE
    assert store.upserts == []


def test_an_epic_is_refused_on_a_task_ticket(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch)
    result = _edit(tmp_path, id="t1", epic="big-one")
    assert result.code == 2
    assert "--epic applies to an initiative" in result.text
    assert store.upserts == []


def test_dry_run_returns_the_diff_and_writes_neither_store_nor_file(tmp_path, monkeypatch):
    store, path = _setup(tmp_path, monkeypatch)
    result = _edit(tmp_path, epic="big-one", dry_run=True)
    assert result.code == 0
    assert "+epic: big-one\n" in result.text
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert store.upserts == []
