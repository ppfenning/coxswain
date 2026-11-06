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


_MERGES = [
    {"event": "tasks_merged", "into": "a", "tasks": ["a", "b"]},
    {"event": "tasks_merged", "into": "c", "tasks": ["c", "d"]},
]


def _plain_row() -> dict:
    return route.status_rows([{"id": "run-1", "pid": 7, "alive": True, "started": "10:00"}])[0]


def test_two_tasks_merged_events_print_two_lines_under_the_row() -> None:
    assert route.render_status([_plain_row()], events={"run-1": _MERGES}) == (
        "run-1: alive (pid 7, started 10:00)\n  merged: a, b into a\n  merged: c, d into c"
    )


def test_a_render_without_events_is_unchanged() -> None:
    plain = route.render_status([_plain_row()])
    assert plain == "run-1: alive (pid 7, started 10:00)"
    assert route.render_status([_plain_row()], events={}) == plain
    assert route.render_status([_plain_row()], events={"other": _MERGES}) == plain


def test_merge_event_lines_skip_other_events() -> None:
    assert route.merge_event_lines([{"event": "noted"}, _MERGES[0], {"event": "tasks_merged"}]) == ["merged: a, b into a"]


def test_initiative_events_reads_only_initiative_rows_that_carry_them() -> None:
    queue = [
        {"kind": "initiative", "task_id": "x", "initiative": "init", "extra": {"events": _MERGES}},
        {"kind": "initiative", "task_id": "y", "extra": {}},
        {"kind": "task", "task_id": "t", "extra": {"events": _MERGES}},
    ]
    assert route.initiative_events(queue) == {"init": _MERGES}


def test_events_keyed_by_initiative_print_under_its_run_key_row() -> None:
    queue = [{"kind": "initiative", "task_id": "x", "initiative": "init-a", "extra": {"events": _MERGES[:1]}}]
    rows = route.status_rows(
        [
            {"id": "init-a-12", "pid": 7, "alive": True, "started": "10:00"},
            {"id": "other-3", "pid": 8, "alive": True, "started": "10:01"},
        ]
    )
    events = cli._run_events([r["id"] for r in rows], route.initiative_events(queue))
    assert events == {"init-a-12": _MERGES[:1]}
    assert route.render_status(rows, events=events) == (
        "init-a-12: alive (pid 7, started 10:00)\n  merged: a, b into a\nother-3: alive (pid 8, started 10:01)"
    )
