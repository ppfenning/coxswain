from agent_tools import route_edit, run_store, store_cli

PR = "https://github.com/o/r/pull/259"
NOW = "2026-11-05T10:00:00Z"
ONE_RUN = [("ship-2", "p1")]


def _row(state):
    return {
        "kind": "task", "initiative": "ship", "task_id": "t1", "phase": "p1", "state": state, "needs": [],
        "title": "A title", "surfaces": ["a.py"], "body": "\nBody\n", "extra": {"id": "t1", "phase": "p1", "repo": "r"},
        "holder": None, "epoch": None, "expires_at": None,
    }


class _Store:
    """A store double: one task row, its `set_state` writes, and the `landed` each `mark_landed` put on a run's record."""

    def __init__(self, state):
        self.row, self.states, self.marks, self.landed = _row(state), [], [], {}

    def read_queue(self, runs_dir, initiative=None, kind=None):
        return [dict(self.row)]

    def set_state(self, runs_dir, initiative, task, state, by, expected=None):
        self.states.append((initiative, task, state, by, expected))
        if expected is not None and self.row["state"] != expected:
            return store_cli.StateRefused("refused", self.row["state"])
        self.row["state"] = state
        return store_cli.StateSet({"state": state})

    def mark_landed(self, runs_dir, run, phase, task, pr, at):
        self.marks.append((run, phase, task, pr, at))
        self.landed[run] = {"pr": pr, "at": at}
        return store_cli.Landed({})

    def task_record(self, runs_dir, run, phase, task):
        return {"landed": self.landed[run]} if run in self.landed else {}


def _store(monkeypatch, state, runs=ONE_RUN):
    fake = _Store(state)
    monkeypatch.setattr(run_store, "read_queue", fake.read_queue)
    monkeypatch.setattr(run_store, "task_run_phases", lambda runs_dir, initiative, task: list(runs))
    monkeypatch.setattr(run_store, "task_record", fake.task_record)
    monkeypatch.setattr(store_cli, "set_state", fake.set_state)
    monkeypatch.setattr(store_cli, "mark_landed", fake.mark_landed)
    return fake


def _mark(store, tmp_path, pr=PR):
    return route_edit.mark_done_with_pr(tmp_path, store.read_queue(tmp_path)[0], pr, NOW)


def test_an_approved_row_ends_done_with_the_pr_recorded(tmp_path, monkeypatch):
    store = _store(monkeypatch, "approved")
    assert _mark(store, tmp_path) is None
    assert store.row["state"] == "done"
    assert store.landed == {"ship-2": {"pr": PR, "at": NOW}}


def test_a_second_call_writes_nothing_and_reports_success(tmp_path, monkeypatch):
    store = _store(monkeypatch, "approved")
    assert _mark(store, tmp_path) is None
    assert _mark(store, tmp_path) is None
    assert len(store.states) == 1 and len(store.marks) == 1 and store.row["state"] == "done"


def test_a_retry_finds_the_pr_on_the_run_whose_phase_matches_not_the_newest(tmp_path, monkeypatch):
    store = _store(monkeypatch, "approved", runs=[("ship-3", "p9"), ("ship-2", "p1")])
    assert _mark(store, tmp_path) is None
    assert _mark(store, tmp_path) is None
    assert list(store.landed) == ["ship-2"] and len(store.states) == 1 and len(store.marks) == 1


def test_with_no_run_the_row_ends_done_with_a_note_and_a_retry_is_not_refused(tmp_path, monkeypatch):
    store = _store(monkeypatch, "approved", runs=[])
    first = _mark(store, tmp_path)
    assert first is not None and first.code == route_edit.DONE and "no run holds t1" in first.text
    retry = _mark(store, tmp_path)
    assert retry is not None and retry.code == route_edit.DONE
    assert store.row["state"] == "done" and len(store.states) == 1 and store.marks == []


def test_a_done_row_without_a_recorded_pr_gets_it_recorded_with_no_state_write(tmp_path, monkeypatch):
    store = _store(monkeypatch, "done")
    assert _mark(store, tmp_path) is None
    assert store.states == [] and store.landed == {"ship-2": {"pr": PR, "at": NOW}}


def test_a_done_row_with_another_pr_is_refused_and_left_unchanged(tmp_path, monkeypatch):
    store = _store(monkeypatch, "done")
    store.landed["ship-2"] = {"pr": "https://github.com/o/r/pull/1", "at": "earlier"}
    result = _mark(store, tmp_path)
    assert result is not None and result.code == route_edit.REFUSED and "pull/1" in result.text
    assert store.states == [] and store.marks == []


def test_a_ready_row_ends_done_with_the_pr_recorded(tmp_path, monkeypatch):
    store = _store(monkeypatch, "ready")
    assert _mark(store, tmp_path) is None
    assert store.row["state"] == "done"
    assert store.landed == {"ship-2": {"pr": PR, "at": NOW}}


def test_a_quarantined_row_ends_done_with_the_pr_recorded(tmp_path, monkeypatch):
    store = _store(monkeypatch, "quarantined")
    assert _mark(store, tmp_path) is None
    assert store.row["state"] == "done"
    assert store.landed == {"ship-2": {"pr": PR, "at": NOW}}


def test_a_dropped_row_is_refused_naming_the_state_and_left_unchanged(tmp_path, monkeypatch):
    store = _store(monkeypatch, "dropped")
    result = _mark(store, tmp_path)
    assert result is not None and result.code == route_edit.REFUSED and "dropped" in result.text
    assert store.row["state"] == "dropped" and store.states == [] and store.marks == []


def test_a_draft_row_is_refused_naming_the_state_and_left_unchanged(tmp_path, monkeypatch):
    store = _store(monkeypatch, "draft")
    result = _mark(store, tmp_path)
    assert result is not None and result.code == route_edit.REFUSED and "'draft'" in result.text
    assert store.row["state"] == "draft" and store.states == [] and store.marks == []
