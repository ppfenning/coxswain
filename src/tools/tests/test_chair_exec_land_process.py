from agent_tools.chair_exec import Deps, perform

LAND_ARGV_HEAD = ["cox", "runs", "land"]
LAUNCH = {"kind": "rescue", "initiative": "alpha", "task_id": "t9", "epoch": 1}


def _land(task: str, repo: str) -> dict:
    return {"kind": "land", "task_id": task, "repo": repo, "run": "run-1", "epoch": 1}


def _landed(action: dict) -> dict:
    return {"action": action, "status": "landed", "reason": "merge: ok\nmark_done: ok\n"}


class _ProcessSink:
    """A LandSink with `start`: it records each call and hands back what the test preloaded at the next collect."""

    def __init__(self, full: tuple[str, ...] = (), finished: list | None = None) -> None:
        self._full, self.finished = set(full), finished or []
        self.started: list = []
        self.submitted: list = []

    def pending(self, repo: str) -> bool:
        return False

    def full(self, repo: str) -> bool:
        return repo in self._full

    def landing(self) -> list:
        return []

    def start(self, action: dict, argv: list[str]) -> dict:
        self.started.append((action, argv))
        return {"action": action, "status": "in_progress", "reason": ""}

    def submit(self, action: dict, work) -> dict:
        self.submitted.append(action)
        return {"action": action, "status": "in_progress", "reason": ""}

    def collect(self) -> list:
        done, self.finished = self.finished, []
        return done


def _deps(calls: list, sink: _ProcessSink) -> Deps:
    """A Deps whose `run` blocks forever on a land argv, so the test fails by hanging if a land runs in line."""

    def run(argv):
        if argv[:3] == LAND_ARGV_HEAD:
            raise AssertionError(f"deps.run would block on a land: {argv}")
        calls.append(("run", argv))
        return 0, ""

    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: calls.append(("record", action["kind"], action["status"])),
        run_id=lambda action: "run-1",
        repo_for=lambda action: action.get("repo", "r"),
        rev_parse=lambda repo, ref: None,
        launch_stagger_s=0,
        lands=sink,
    )


def _runs(calls: list) -> list:
    return [c for c in calls if c[0] == "run"]


def test_a_tick_starts_both_lands_and_still_runs_the_launch() -> None:
    calls: list = []
    sink = _ProcessSink()
    results = perform([_land("t1", "/r1"), _land("t2", "/r2"), LAUNCH], _deps(calls, sink), lambda: 1, False)
    assert [r["status"] for r in results] == ["in_progress", "in_progress", "done"]
    assert [a["repo"] for a, _ in sink.started] == ["/r1", "/r2"]
    assert all(argv[:3] == LAND_ARGV_HEAD for _, argv in sink.started)
    assert sink.submitted == []
    assert [c[1][:4] for c in _runs(calls)] == [["cox", "route", "launch", "rescue"]]
    assert [c for c in calls if c[0] == "record"] == [("record", "rescue", "done")]


def test_the_next_tick_records_the_collected_land_before_the_launch() -> None:
    calls: list = []
    sink = _ProcessSink(finished=[_landed(_land("t1", "/r1"))])
    results = perform([LAUNCH], _deps(calls, sink), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed", "done"]
    assert [c for c in calls if c[0] == "record"] == [("record", "land", "landed"), ("record", "rescue", "done")]
    assert sink.started == []


def test_a_full_repo_gets_in_progress_and_no_start() -> None:
    calls: list = []
    sink = _ProcessSink(full=("/r1",))
    results = perform([_land("t1", "/r1"), _land("t2", "/r2")], _deps(calls, sink), lambda: 1, False)
    assert [r["status"] for r in results] == ["in_progress", "in_progress"]
    assert [a["repo"] for a, _ in sink.started] == ["/r2"]
    assert sink.submitted == []


def test_a_failed_collected_land_is_recorded_once_and_escalated_once() -> None:
    calls: list = []
    action = {"kind": "land_phase", "phase": "p1", "repo": "/r1", "run": "run-1", "initiative": "alpha", "epoch": 1}
    output = "land: stopped at wait_checks: ci red"
    failed = {"action": action, "status": "not_landed", "reason": output}
    sink = _ProcessSink(finished=[failed])
    deps = _deps(calls, sink)
    results = perform([], deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["not_landed", "escalated"]
    assert results[0]["reason"] == output
    assert "ci red" in results[1]["reason"]
    recorded = [c for c in calls if c[0] == "record"]
    assert recorded == [("record", "land_phase", "not_landed"), ("record", "needs_chair", "escalated")]
    assert perform([], deps, lambda: 1, False) == []
    assert [c for c in calls if c[0] == "record"] == recorded


def test_a_land_phase_with_task_runs_still_goes_through_submit() -> None:
    calls: list = []
    sink = _ProcessSink()
    action = {**_land("t1", "/r1"), "kind": "land_phase", "phase": "p1", "task_runs": [{"run": "run-2", "task_id": "t1"}]}
    results = perform([action], _deps(calls, sink), lambda: 1, False)
    assert [r["status"] for r in results] == ["in_progress"]
    assert sink.started == []
    assert sink.submitted == [action]
