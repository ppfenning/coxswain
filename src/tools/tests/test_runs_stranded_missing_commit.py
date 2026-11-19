import subprocess

from agent_tools.chair_read_stranded import phase_branch_has_commits
from agent_tools.runs_stranded import stranded

ABSENT = lambda item: False  # noqa: E731
PRESENT = lambda item: True  # noqa: E731


def _item(**kw) -> dict:
    return {"id": "t1", "initiative": "init", "phase": "p1", "state": "approved", **kw}


def _approved_record(**kw) -> dict:
    return {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "repo": "/r",
            "review": {"verdict": "approve"}, "arbitration": "arbiter: skipped (both approved)", "landed": False, **kw}


def test_an_approved_item_absent_from_its_phase_branch_yields_a_reground_row():
    (row,) = stranded([], [_item()], on_phase_branch=ABSENT)
    assert row["run"] is None
    assert row["task"] == "t1"
    assert row["branch"] == "epic/init/p1"
    assert "re-ground" in row["remedy"]
    assert "runs land" not in row["remedy"]


def test_no_row_when_the_commit_is_on_the_phase_branch():
    assert stranded([], [_item()], on_phase_branch=PRESENT) == []


def test_no_row_when_a_live_run_exists():
    assert stranded([], [_item(live_run=True)], on_phase_branch=ABSENT) == []


def test_no_row_for_an_item_already_marked_merged():
    assert stranded([], [_item(merged=True)], on_phase_branch=ABSENT) == []


def test_no_duplicate_beside_an_approved_unlanded_record():
    rows = stranded([_approved_record()], [_item()], on_phase_branch=ABSENT)
    assert [row["run"] for row in rows] == ["r1"]


def test_no_row_for_a_rebuilt_item():
    assert stranded([], [_item(rebuilt_from=["r0"])], on_phase_branch=ABSENT) == []


def test_no_row_for_a_ready_item_awaiting_a_rebuild():
    assert stranded([], [_item(state="ready", land_conflict=True)], on_phase_branch=ABSENT) == []


def test_a_superseded_code_quarantine_leaves_the_item_reported_once():
    record = _approved_record(cause="code")
    assert [row["run"] for row in stranded([record], [_item()], on_phase_branch=ABSENT)] == [None]


def test_no_callable_keeps_todays_behaviour():
    assert stranded([], [_item()]) == []


def _git(path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(path), *args],
                   check=True, capture_output=True)


def test_the_git_edge_reads_a_phase_branch_with_and_without_a_commit_beyond_main(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = tmp_path / "app"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(app)], check=True)
    _git(app, "commit", "--quiet", "--allow-empty", "-m", "init")
    item = {"initiative": "init", "phase": "p1", "repo": "~/app"}
    assert phase_branch_has_commits(item) is False  # no branch yet
    _git(app, "branch", "epic/init/p1")
    assert phase_branch_has_commits(item) is False  # the branch is empty
    _git(app, "checkout", "--quiet", "epic/init/p1")
    _git(app, "commit", "--quiet", "--allow-empty", "-m", "t1")
    assert phase_branch_has_commits(item) is True
    assert phase_branch_has_commits({**item, "repo": None}) is True  # no repo: not reported
    assert phase_branch_has_commits({**item, "repo": "~/not-here"}) is True  # no such directory: not reported
