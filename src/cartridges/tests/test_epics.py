from __future__ import annotations

import pytest

from core.epics import format_epic_line, rollup, valid_epic_id


def test_membership_grouping_sorted() -> None:
    members = {"b2": "beta", "a1": "alpha", "b1": "beta"}
    records = rollup(members, {"b1"}, {})
    assert [r["id"] for r in records] == ["alpha", "beta"]
    assert records[1]["initiatives"] == [{"id": "b1", "landed": True}, {"id": "b2", "landed": False}]
    assert (records[1]["landed"], records[1]["total"]) == (1, 2)


def test_initiatives_without_epic_are_omitted() -> None:
    assert rollup({"a": None, "b": None}, {"a"}, {"x": {"title": "X"}}) == []


def test_partial_epic_is_not_complete() -> None:
    (record,) = rollup({"a": "e", "b": "e"}, {"a"}, {})
    assert record["complete"] is False


def test_all_landed_is_complete() -> None:
    (record,) = rollup({"a": "e", "b": "e"}, {"a", "b"}, {})
    assert record["complete"] is True
    assert format_epic_line(record) == "e 2/2 landed complete"


def test_bad_id_refused() -> None:
    with pytest.raises(ValueError, match="epic.*'Bad_Id'"):
        valid_epic_id("Bad_Id")
    with pytest.raises(ValueError, match="epic"):
        valid_epic_id("-lead")
    assert valid_epic_id("a-1") == "a-1"


def test_default_title_and_branch() -> None:
    assert rollup({"a": "e"}, set(), {}) == [
        {
            "id": "e",
            "title": "e",
            "target_branch": None,
            "initiatives": [{"id": "a", "landed": False}],
            "landed": 0,
            "total": 1,
            "complete": False,
        }
    ]
    (record,) = rollup({"a": "e"}, set(), {"e": {"title": "Epic E", "target_branch": "epic/e"}})
    assert (record["title"], record["target_branch"]) == ("Epic E", "epic/e")


def test_format_epic_line_partial() -> None:
    (record,) = rollup({"a": "e", "b": "e"}, {"a"}, {})
    assert format_epic_line(record) == "e 1/2 landed"
