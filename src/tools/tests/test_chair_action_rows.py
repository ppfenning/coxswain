"""A kept chair_actions row: it refuses edits, a copy of it is the caller's own, and each view of it is worked out once."""

import copy
import json
import pickle

import pytest

from agent_tools.chair_action_rows import FrozenRow, freeze, row_view
from agent_tools.chair_facts import action_doc

ROW = {"ts": "2026-11-17T10:00:00Z", "kind": "needs_chair", "target": "i1", "action_json": {"cause": "no_run", "kind": "needs_chair"}}

calls: list[object] = []


def counted(row):
    calls.append(row)
    return {"seen": row["ts"]}


def test_a_frozen_row_holds_the_same_items_and_refuses_every_edit():
    row = freeze(ROW)
    assert row == ROW
    for edit in (
        lambda: row.__setitem__("ts", "x"), lambda: row.__delitem__("ts"), lambda: row.pop("ts"), row.popitem, row.clear,
        lambda: row.update(ts="x"), lambda: row.setdefault("new", 1),
    ):
        with pytest.raises(TypeError, match="never edited"):
            edit()
    with pytest.raises(TypeError):
        row |= {"ts": "x"}
    assert row == ROW


def test_a_copy_of_a_frozen_row_is_a_plain_dict_the_caller_may_edit():
    row = freeze(ROW)
    for made in (dict(row), {**row}, copy.copy(row), copy.deepcopy(row), pickle.loads(pickle.dumps(row))):
        assert type(made) is dict and made == ROW
        made["ts"] = "edited"
    assert row["ts"] == ROW["ts"]
    assert json.loads(json.dumps(row)) == ROW


def test_freeze_hands_back_a_frozen_row_as_it_is():
    row = freeze(ROW)
    assert freeze(row) is row
    assert isinstance(row, FrozenRow)


def test_a_view_of_a_frozen_row_is_worked_out_once_and_kept_frozen():
    calls.clear()
    row = freeze(ROW)
    first, second = row_view(row, counted), row_view(row, counted)
    assert first == {"seen": ROW["ts"]} and first is second
    assert len(calls) == 1
    with pytest.raises(TypeError):
        first["seen"] = "x"  # type: ignore[index]


def test_a_plain_row_keeps_no_view():
    calls.clear()
    plain = dict(ROW)
    assert row_view(plain, counted) == row_view(plain, counted) == {"seen": ROW["ts"]}
    assert len(calls) == 2


def test_action_doc_of_a_frozen_row_equals_the_doc_of_the_plain_row():
    row = freeze(ROW)
    assert action_doc(row) == action_doc(dict(ROW)) == {**ROW, "cause": "no_run"}
    assert action_doc(row) is action_doc(row)
    assert action_doc(action_doc(row)) == action_doc(row)  # a doc read again is the same doc
