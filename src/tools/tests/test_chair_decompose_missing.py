import json
from datetime import UTC, datetime

from agent_tools.chair_decompose_missing import MISSING_IDEA_BACKOFF_S, missing_idea_paths

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
REASON = "routing: no idea file at /ideas/a.md"


def _row(path: str, ts: str, status: str = "failed", reason: str = REASON) -> dict:
    return {
        "kind": "launch_decompose",
        "intake_ids": [path],
        "status": status,
        "reason": reason,
        "ts": ts,
    }


def test_window_is_one_hour() -> None:
    assert MISSING_IDEA_BACKOFF_S == 3600


def test_fresh_failed_missing_file_row_is_in_the_set() -> None:
    assert missing_idea_paths([_row("/ideas/a.md", "2026-11-11T11:30:00+00:00")], NOW) == frozenset({"/ideas/a.md"})


def test_row_older_than_the_window_is_not() -> None:
    assert missing_idea_paths([_row("/ideas/a.md", "2026-11-11T10:59:59+00:00")], NOW) == frozenset()


def test_failed_row_for_another_reason_is_not() -> None:
    row = _row("/ideas/a.md", "2026-11-11T11:30:00+00:00", reason="lease held")
    assert missing_idea_paths([row], NOW) == frozenset()


def test_later_success_for_the_same_path_clears_it() -> None:
    rows = [
        _row("/ideas/a.md", "2026-11-11T11:30:00+00:00"),
        _row("/ideas/a.md", "2026-11-11T11:40:00+00:00", status="launched", reason=""),
    ]
    assert missing_idea_paths(rows, NOW) == frozenset()


def test_later_failure_with_another_reason_clears_it() -> None:
    rows = [
        _row("/ideas/a.md", "2026-11-11T11:30:00+00:00"),
        _row("/ideas/a.md", "2026-11-11T11:40:00+00:00", reason="lease held"),
    ]
    assert missing_idea_paths(rows, NOW) == frozenset()


def test_row_order_does_not_decide_which_is_newest() -> None:
    rows = [
        _row("/ideas/a.md", "2026-11-11T11:40:00+00:00", status="launched", reason=""),
        _row("/ideas/a.md", "2026-11-11T11:30:00+00:00"),
    ]
    assert missing_idea_paths(rows, NOW) == frozenset()


def test_two_paths_are_judged_separately() -> None:
    rows = [
        _row("/ideas/a.md", "2026-11-11T11:30:00+00:00"),
        _row("/ideas/b.md", "2026-11-11T11:30:00+00:00"),
        _row("/ideas/b.md", "2026-11-11T11:45:00+00:00", status="launched", reason=""),
        _row("/ideas/c.md", "2026-11-11T09:00:00+00:00"),
    ]
    assert missing_idea_paths(rows, NOW) == frozenset({"/ideas/a.md"})


def test_action_json_document_is_read() -> None:
    doc = json.dumps(_row("/ideas/a.md", "2026-11-11T11:30:00+00:00"))
    assert missing_idea_paths([{"action_json": doc}], NOW) == frozenset({"/ideas/a.md"})


def test_naive_stamp_is_read_as_utc() -> None:
    assert missing_idea_paths([_row("/ideas/a.md", "2026-11-11T11:30:00")], NOW) == frozenset({"/ideas/a.md"})


def test_malformed_rows_are_ignored() -> None:
    bare_string_ids = {**_row("/ideas/a.md", "2026-11-11T11:30:00+00:00"), "intake_ids": "/ideas/a.md"}
    no_stamp = {k: v for k, v in _row("/ideas/a.md", "x").items() if k != "ts"}
    other_kind = {**_row("/ideas/a.md", "2026-11-11T11:50:00+00:00", status="launched"), "kind": "relaunch"}
    rows = [
        {"action_json": "{not json"},
        bare_string_ids,
        _row("/ideas/a.md", "not a time"),
        no_stamp,
        other_kind,
        _row("/ideas/b.md", "2026-11-11T11:30:00+00:00"),
    ]
    assert missing_idea_paths(rows, NOW) == frozenset({"/ideas/b.md"})
