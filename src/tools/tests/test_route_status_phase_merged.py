from agent_tools import cli, route


def _item(id: str, phase: str, state: str, merged: bool = False) -> dict:
    return {"id": id, "initiative": "init", "phase": phase, "state": state, "merged": merged}


def _row(items: list[dict]) -> dict:
    detail = {"run-1": {"phases": route.phase_summary(items)["init"], "tally": route.task_tally(items)}}
    entry = {"id": "run-1", "pid": 7, "alive": True, "started": "10:00"}
    return route.status_rows([entry], detail=detail)[0]


def test_phase_one_merged_and_phase_two_building() -> None:
    items = [_item("a", "p1", "approved", True), _item("b", "p1", "done"), _item("c", "p2", "approved"), _item("d", "p2", "ready")]
    assert route.phase_summary(items) == {
        "init": [{"phase": "p1", "state": "merged", "tasks": 2}, {"phase": "p2", "state": "building", "tasks": 2}]
    }
    assert route._status_line(_row(items)) == "run-1: alive (pid 7, started 10:00) phases: p1 merged, p2 building tally: 1 approved, 1 merged, 1 done"


def test_both_phases_landed() -> None:
    items = [_item("a", "p1", "done"), _item("b", "p2", "done"), _item("c", "p2", "dropped")]
    assert [p["state"] for p in route.phase_summary(items)["init"]] == ["landed", "landed"]
    assert route._status_line(_row(items)) == "run-1: alive (pid 7, started 10:00) phases: p1 landed, p2 landed tally: 2 done"


def test_sixteen_tasks_read_as_seven_approved_and_nine_merged() -> None:
    items = [_item(f"a{n}", "p1", "approved") for n in range(7)] + [_item(f"m{n}", "p1", "approved", True) for n in range(9)]
    assert route.task_tally(items) == {"approved": 7, "merged": 9, "done": 0, "total": 16}
    assert route._status_line(_row(items)).endswith("tally: 7 approved, 9 merged")


def test_a_row_without_detail_keeps_the_old_line() -> None:
    row = route.status_rows([{"id": "run-1", "pid": 7, "alive": True, "started": "10:00", "summary": "epic done"}])[0]
    assert "phases" not in row and "tally" not in row
    assert route._status_line(row) == "run-1: alive (pid 7, started 10:00) epic done"


def _runs() -> list[dict]:
    return route.status_rows([{"id": "run-1", "pid": 7, "alive": True, "started": "10:00"}, {"id": "run-2", "pid": None}])


def test_two_tasks_merged_events_print_two_lines_under_their_row() -> None:
    events = {
        "run-1": [
            {"event": "tasks_merged", "into": "a", "tasks": ["a", "b"]},
            {"event": "other"},
            {"event": "tasks_merged", "into": "c", "tasks": ["c", "d", "e"]},
        ]
    }
    assert route.render_status(_runs(), events=events).splitlines() == [
        "run-1: alive (pid 7, started 10:00)",
        "merged: a, b into a",
        "merged: c, d, e into c",
        "run-2: no pidfile",
    ]


def test_no_events_leaves_the_render_unchanged() -> None:
    plain = route.render_status(_runs())
    assert plain == "run-1: alive (pid 7, started 10:00)\nrun-2: no pidfile"
    assert route.render_status(_runs(), events=None) == plain
    assert route.render_status(_runs(), events={}) == plain
    assert route.render_status(_runs(), events={"run-1": []}) == plain


def test_a_suffixed_run_id_shows_its_initiatives_merge_lines() -> None:
    event = {"event": "tasks_merged", "into": "a", "tasks": ["a", "b"]}
    queue = [
        {"kind": "initiative", "initiative": "demo", "task_id": "demo", "extra": {"events": [event]}},
        {"kind": "initiative", "initiative": "quiet", "task_id": "quiet", "extra": {}},
        {"kind": "task", "initiative": "demo", "task_id": "a", "extra": {"events": [event]}},
    ]
    runs = route.status_rows([{"id": "demo-3", "pid": 7, "alive": True, "started": "10:00"}, {"id": "quiet-1", "pid": None}])
    events = cli._initiative_events(queue, [r["id"] for r in runs])
    assert events == {"demo-3": [event]}
    assert route.render_status(runs, events=events).splitlines() == [
        "demo-3: alive (pid 7, started 10:00)",
        "merged: a, b into a",
        "quiet-1: no pidfile",
    ]


def test_malformed_queue_rows_are_skipped_not_raised() -> None:
    queue = [{"task_id": "x"}, {"kind": "initiative", "task_id": "y", "extra": {"events": "oops"}}, {"kind": "initiative", "task_id": "z", "extra": "bad"}]
    assert cli._initiative_events(queue, ["x-1", "y-1", "z-1"]) == {}
