from datetime import UTC, datetime

from agent_tools.chair_facts import launch_history_from_actions, lease_refusals_from_actions

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _row(
    ts: str, status: str = "lease_held", kind: str = "launch_epic", initiative: str = "x", holder: str = "x-1"
) -> dict:
    reason = f"run: {holder} holds runs:{initiative}; refusing to start {initiative}-2"
    return {"ts": ts, "kind": kind, "status": status, "initiative": initiative, "reason": reason}


def test_one_lease_held_row_gives_equal_first_and_last():
    assert lease_refusals_from_actions([_row("2026-11-07T11:30:00Z")], NOW) == {
        "x": {"first_at": "2026-11-07T11:30:00Z", "last_at": "2026-11-07T11:30:00Z", "holder_run": "x-1"}
    }


def test_two_rows_give_the_earliest_and_newest_and_the_newest_holder():
    rows = [_row("2026-11-07T11:40:00Z", holder="x-9"), _row("2026-11-07T11:20:00Z", holder="x-1")]
    assert lease_refusals_from_actions(rows, NOW) == {
        "x": {"first_at": "2026-11-07T11:20:00Z", "last_at": "2026-11-07T11:40:00Z", "holder_run": "x-9"}
    }


def test_a_later_done_row_clears_the_streak():
    rows = [_row("2026-11-07T11:20:00Z"), _row("2026-11-07T11:40:00Z", status="done")]
    assert lease_refusals_from_actions(rows, NOW) == {}


def test_a_later_failed_row_clears_the_streak():
    rows = [_row("2026-11-07T11:20:00Z"), _row("2026-11-07T11:40:00Z", status="failed", kind="relaunch")]
    assert lease_refusals_from_actions(rows, NOW) == {}


def test_a_refusal_after_a_done_row_starts_a_new_streak():
    rows = [_row("2026-11-07T11:10:00Z"), _row("2026-11-07T11:20:00Z", status="done"), _row("2026-11-07T11:30:00Z")]
    assert lease_refusals_from_actions(rows, NOW)["x"]["first_at"] == "2026-11-07T11:30:00Z"


def test_a_row_older_than_the_window_is_ignored():
    rows = [_row("2026-11-07T10:30:00Z"), _row("2026-11-07T11:30:00Z")]
    assert lease_refusals_from_actions(rows, NOW)["x"]["first_at"] == "2026-11-07T11:30:00Z"
    assert lease_refusals_from_actions([_row("2026-11-07T10:30:00Z")], NOW) == {}


def test_an_old_done_row_does_not_break_a_streak_inside_the_window():
    rows = [_row("2026-11-07T10:00:00Z", status="done"), _row("2026-11-07T11:30:00Z")]
    assert list(lease_refusals_from_actions(rows, NOW)) == ["x"]


def test_two_initiatives_keep_separate_streaks():
    rows = [
        _row("2026-11-07T11:10:00Z", initiative="a", holder="a-1"),
        _row("2026-11-07T11:20:00Z", initiative="b", holder="b-1"),
        _row("2026-11-07T11:30:00Z", initiative="b", status="done"),
        _row("2026-11-07T11:40:00Z", initiative="a", holder="a-1"),
    ]
    assert lease_refusals_from_actions(rows, NOW) == {
        "a": {"first_at": "2026-11-07T11:10:00Z", "last_at": "2026-11-07T11:40:00Z", "holder_run": "a-1"}
    }


def test_an_unparseable_row_is_skipped_and_a_non_launch_row_neither_counts_nor_breaks():
    rows = [
        _row("2026-11-07T11:10:00Z"),
        {
            "ts": "2026-11-07T11:20:00Z",
            "kind": "launch_epic",
            "status": "lease_held",
            "initiative": "x",
            "reason": "boom",
        },
        {"ts": "2026-11-07T11:25:00Z", "kind": "steer_clear", "status": "done", "initiative": "x"},
    ]
    assert lease_refusals_from_actions(rows, NOW)["x"]["last_at"] == "2026-11-07T11:10:00Z"


def test_a_store_row_carries_its_fields_in_action_json():
    row = {
        "ts": "2026-11-07T11:30:00Z",
        "status": "lease_held",
        "action_json": '{"kind": "relaunch", "initiative": "x", "reason": "run: x-4 holds runs:x; refusing to start x-5"}',
    }
    assert lease_refusals_from_actions([row], NOW)["x"]["holder_run"] == "x-4"


def test_a_lease_held_row_adds_nothing_to_launches():
    held = _row("2026-11-07T11:30:00Z")
    assert launch_history_from_actions([held], [], NOW) == {}
    done = _row("2026-11-07T11:20:00Z", status="done")
    history = launch_history_from_actions([done, held], [], NOW)
    assert history == {"x": {"launches": [{"at": "2026-11-07T11:20:00Z", "kind": "launch_epic"}], "runs": []}}
