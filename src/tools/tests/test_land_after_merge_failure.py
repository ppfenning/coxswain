"""A step that fails after the merge records the PR and marks the merged tasks done instead of raising."""
from test_land_from_store import _own_tempdir, repo, store  # noqa: F401
from test_land_ticket_earlier_runs import URL, _build, _land_ticket

from agent_tools import cli, route_edit


def _fail_first_mark_done(monkeypatch):
    """The first mark_done raises FileNotFoundError; every later step, and the helper's retries, run as the harness has them."""
    seen, harness = [], cli._execute_land_step

    def step(repo_, step_, forge=None):
        if step_["kind"] == "mark_done" and not seen:
            seen.append(step_["task"])
            raise FileNotFoundError(step_["path"] if "path" in step_ else step_["task"])
        return harness(repo_, step_, forge)

    monkeypatch.setattr(cli, "_execute_land_step", step)
    return seen


def test_a_mark_done_that_raises_after_the_merge_is_no_traceback(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    done, _marks, profile = _build(tmp_path, monkeypatch, store, None)
    _fail_first_mark_done(monkeypatch)
    assert _land_ticket(repo, tmp_path, profile) == 1
    assert "land: stopped at mark_done: FileNotFoundError" in capsys.readouterr().out
    assert done == {"early": URL, "remote": URL, "late": URL}


def test_an_unmarkable_task_is_named_with_its_reason(repo, tmp_path, monkeypatch, store, capsys):  # noqa: F811
    done, _marks, profile = _build(tmp_path, monkeypatch, store, None)
    _fail_first_mark_done(monkeypatch)
    monkeypatch.setattr(route_edit, "mark_done_with_pr", lambda runs_dir, row, pr, now: route_edit.EditResult("store refused", route_edit.FAILED))
    assert _land_ticket(repo, tmp_path, profile) == 1
    out = capsys.readouterr().out
    assert "remote not marked done: store refused" in out
    assert "remote" not in done


def test_the_helper_marks_each_task_once_and_returns_the_unmarked():
    steps = [{"kind": "mark_done", "task": "a", "path": "/nowhere/runs/r/tasks/p/a.json"},
             {"kind": "mark_done", "task": "a", "path": "/nowhere/runs/r/tasks/p/a.json"},
             {"kind": "merge"}]
    assert cli._mark_remaining_done(steps, URL, "t", lambda s: (True, "ok")) == [("a", "a has no work item or runs dir to mark done")]
