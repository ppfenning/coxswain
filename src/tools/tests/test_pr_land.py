from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_tools.pr_land import LEVEL_MERGED, MERGE_FAILED, LandDeps, carried_tasks, land_pr

REPO = Path("/repo")
URL = "https://github.com/o/r/pull/7"
BODY = "Run: r1\nTask: t-one\nReview verdict: approve"


def _pr(state: str = "OPEN", body: str = BODY) -> dict:
    return {
        "number": 7,
        "state": state,
        "url": URL,
        "headRefName": "b",
        "headRefOid": "abc",
        "baseRefName": "main",
        "body": body,
    }


class Fakes:
    """Recording edges; the clock advances only when `sleep` is called."""

    def __init__(self, pr=..., required=("test",), rows=(), merge=(True, "merged"), marks=None, timeout_s=10.0):
        self.pr, self.required, self.rows, self.merge_result = (_pr() if pr is ... else pr), required, rows, merge
        self.marks, self.timeout_s = marks or {}, timeout_s
        self.merges, self.marked, self.sleeps, self.clock = [], [], [], [0.0]

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock[0] += seconds

    def _merge(self, repo, number):
        self.merges.append(number)
        return self.merge_result

    def _mark(self, task, url):
        self.marked.append((task, url))
        return self.marks.get(task, (True, f"{task} marked done"))

    def deps(self) -> LandDeps:
        return LandDeps(
            read_pr=lambda repo, number: self.pr,
            required_for_pr=lambda repo, number: self.required,
            check_rows=lambda repo, sha: self.rows,
            merge=self._merge,
            mark_done=self._mark,
            sleep=self._sleep,
            now=lambda: self.clock[0],
            timeout_s=self.timeout_s,
            poll_s=2.0,
        )


def _land(fakes: Fakes):
    return land_pr(REPO, 7, fakes.deps())


def test_red_required_check_refuses_without_merging() -> None:
    fakes = Fakes(rows=[{"name": "test", "state": "failure"}])
    result = _land(fakes)
    assert (result.event, result.reason) == (MERGE_FAILED, "failed: test")
    assert (fakes.merges, fakes.marked, fakes.sleeps) == ([], [], [])


def test_required_check_that_never_reports_times_out_as_missing() -> None:
    fakes = Fakes(rows=[{"name": "lint", "state": "success"}], timeout_s=6.0)
    result = _land(fakes)
    assert result.event == MERGE_FAILED
    assert result.reason == "timed out after 6s; not reported: test"
    assert (fakes.merges, fakes.marked, fakes.sleeps) == ([], [], [2.0, 2.0, 2.0])


def test_pending_check_times_out_naming_it() -> None:
    result = _land(Fakes(rows=[{"name": "test", "state": "pending"}], timeout_s=1.0))
    assert result.reason == "timed out after 1s; not green yet: test"


def test_unreadable_rows_keep_waiting_and_never_merge() -> None:
    fakes = Fakes(rows=None, timeout_s=2.0)
    result = _land(fakes)
    assert result.reason == "timed out after 2s; check rows could not be read"
    assert fakes.merges == []


def test_ready_merges_and_marks_carried_tasks_with_the_url() -> None:
    fakes = Fakes(rows=[{"name": "test", "state": "success"}])
    result = _land(fakes)
    assert (result.event, result.url, result.tasks_marked) == (LEVEL_MERGED, URL, ("t-one",))
    assert result.text == f"merged {URL}; marked 1 task(s) done"
    assert (fakes.merges, fakes.marked) == ([7], [("t-one", URL)])


def test_checks_that_turn_green_while_waiting_merge() -> None:
    fakes = Fakes(timeout_s=10.0)
    batches = iter([[{"name": "test", "state": "pending"}], [{"name": "test", "state": "success"}]])
    deps = replace(fakes.deps(), check_rows=lambda repo, sha: next(batches))
    result = land_pr(REPO, 7, deps)
    assert result.event == LEVEL_MERGED
    assert (fakes.sleeps, fakes.merges) == ([2.0], [7])


def test_empty_required_set_refuses() -> None:
    fakes = Fakes(required=())
    result = _land(fakes)
    assert result.event == MERGE_FAILED
    assert "could not be read, or none are required" in result.reason
    assert fakes.merges == []


def test_unreadable_required_set_refuses() -> None:
    fakes = Fakes(required=None)
    result = _land(fakes)
    assert result.event == MERGE_FAILED
    assert "could not be read, or none are required" in result.reason
    assert fakes.merges == []


def test_unreadable_pr_refuses() -> None:
    fakes = Fakes(pr=None)
    assert _land(fakes).event == MERGE_FAILED
    assert fakes.merges == []


def test_closed_unmerged_pr_refuses() -> None:
    fakes = Fakes(pr=_pr("CLOSED"))
    result = _land(fakes)
    assert (result.event, result.reason) == (MERGE_FAILED, "pull request 7 is closed and was not merged")
    assert (fakes.merges, fakes.marked) == ([], [])


def test_already_merged_pr_skips_the_merge_and_still_marks_tasks() -> None:
    fakes = Fakes(pr=_pr("MERGED"), required=None)
    result = _land(fakes)
    assert (result.event, result.tasks_marked) == (LEVEL_MERGED, ("t-one",))
    assert (fakes.merges, fakes.marked) == ([], [("t-one", URL)])


def test_refused_merge_returns_the_forge_reason_and_marks_nothing() -> None:
    fakes = Fakes(rows=[{"name": "test", "state": "success"}], merge=(False, "base branch policy prohibits the merge"))
    result = _land(fakes)
    assert (result.event, result.reason) == (MERGE_FAILED, "base branch policy prohibits the merge")
    assert (fakes.merges, fakes.marked) == ([7], [])


def test_body_with_no_task_merges_and_reports_zero_marked() -> None:
    fakes = Fakes(pr=_pr(body="no marker here"), rows=[{"name": "test", "state": "success"}])
    result = _land(fakes)
    assert (result.event, result.tasks_marked, fakes.marked) == (LEVEL_MERGED, (), [])
    assert result.text == f"merged {URL}; marked 0 task(s) done"


def test_task_that_cannot_be_marked_is_named_and_not_counted() -> None:
    body = "Phase: p1\n\n- t-a: A\n- t-b: B"
    fakes = Fakes(
        pr=_pr(body=body), rows=[{"name": "test", "state": "success"}], marks={"t-b": (False, "no store row for t-b")}
    )
    result = _land(fakes)
    assert result.tasks_marked == ("t-a",)
    assert result.text == f"merged {URL}; marked 1 task(s) done\nnot marked t-b: no store row for t-b"


def test_carried_tasks_reads_the_single_task_line() -> None:
    assert carried_tasks("Run: r1\nTask: t-one\nReview verdict: approve") == ("t-one",)


def test_carried_tasks_reads_phase_bullets_and_skips_dropped() -> None:
    body = "Phase: p1\nok\n\n- t-a: A\n  Review: approve\n\n- t-b: B\n\nDropped:\n- t-c: too big\n\n🚣 Built"
    assert carried_tasks(body) == ("t-a", "t-b")


def test_carried_tasks_keeps_order_and_drops_repeats() -> None:
    assert carried_tasks("Task: t-b\nTask: t-a\nTask: t-b") == ("t-b", "t-a")


def test_carried_tasks_ignores_bullets_outside_a_phase_body() -> None:
    assert carried_tasks("Task: t-one\n\nsummary\n- note: not a task") == ("t-one",)


def test_carried_tasks_of_a_body_with_no_marker_is_empty() -> None:
    assert carried_tasks("") == ()
    assert carried_tasks("just prose") == ()
