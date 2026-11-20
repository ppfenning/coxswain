"""One landing-record row that does not read: the partition into records and unreadable rows, and the stack layer."""
from __future__ import annotations

import json

from core import landing_record as lr
from core.landing import stack_unknown


def _good(initiative: str, **fields: object) -> str:
    return json.dumps({"initiative": initiative, "branch": f"init/{initiative}", "parent": "main", "state": "building", **fields})


def test_a_row_reads_as_its_record_or_as_unreadable_with_the_reason() -> None:
    assert lr.read_row("a", _good("a")) == json.loads(_good("a"))
    assert lr.read_row("a", _good("a").encode()) == json.loads(_good("a")), "bytes, as a driver may return them"
    not_json = lr.read_row("a", "{not json")
    assert isinstance(not_json, lr.Unreadable) and not_json.initiative == "a" and "not JSON" in not_json.why
    assert not_json.branch == "", "a row that does not decode names no branch"
    not_object = lr.read_row("a", "[1, 2]")
    assert isinstance(not_object, lr.Unreadable) and not_object.why
    bad_key = lr.read_row("a", _good("a", phases=[1]))
    assert isinstance(bad_key, lr.Unreadable) and "phases" in bad_key.why
    assert bad_key.branch == "init/a", "a row that decodes keeps the branch it names, for the stack"
    other = lr.read_row("a", _good("b"))
    assert isinstance(other, lr.Unreadable) and "'b'" in other.why, "keyed by the row's own column, never by its JSON"
    nameless = lr.read_row("a", json.dumps({"branch": "init/a"}))
    assert isinstance(nameless, lr.Unreadable) and nameless.why == "record_json names no initiative"
    assert isinstance(lr.read_row("a", None), lr.Unreadable)


def test_one_malformed_row_never_hides_the_others() -> None:
    table = lr.listing([("a", _good("a")), ("b", "{not json"), ("c", _good("c"))])
    assert [r["initiative"] for r in table.records] == ["a", "c"]
    assert [u.initiative for u in table.unreadable] == ["b"]
    assert table.initiatives() == {"a", "b", "c"}, "an unreadable row is still a row: the initiative has a record"
    assert lr.listing([]) == lr.Listing()


def _record(initiative: str, parent: str, state: str = "building") -> lr.LandingRecord:
    return {"initiative": initiative, "branch": f"init/{initiative}", "parent": parent, "state": state}


def test_the_layer_below_is_the_record_whose_branch_is_the_parent_or_an_unreadable_row_that_may_be() -> None:
    up = _record("up", "init/low")
    low = _record("low", "epic-x")
    assert lr.layer_below(up, lr.Listing((up, low)), stacked=True, epic_branch="epic-x") == low
    assert lr.layer_below(_record("up", "main"), lr.Listing((low,)), stacked=False, epic_branch=None) is None

    named = lr.Unreadable("low", "phases is malformed", "init/low")
    assert lr.layer_below(up, lr.Listing((up,), (named,)), stacked=False, epic_branch=None) == named, "it names the parent"
    elsewhere = lr.Unreadable("other", "phases is malformed", "init/other")
    assert lr.layer_below(up, lr.Listing((up,), (elsewhere,)), stacked=True, epic_branch="epic-x") is None

    blind = lr.Unreadable("low", "record_json is not JSON")
    assert lr.layer_below(up, lr.Listing((up,), (blind,)), stacked=True, epic_branch="epic-x") == blind, (
        "in a stacked epic a row that names no branch may be the layer below: unknown")
    assert lr.layer_below(up, lr.Listing((up,), (blind,)), stacked=False, epic_branch=None) is None, "no stack: no layer"
    on_epic = _record("up", "epic-x")
    assert lr.layer_below(on_epic, lr.Listing((on_epic,), (blind,)), stacked=True, epic_branch="epic-x") is None, (
        "the epic branch is the bottom of the stack, never a layer")


def test_a_row_says_its_epic_when_it_decodes_far_enough_and_else_its_epic_is_unknown() -> None:
    in_x = lr.read_row("a", _good("a", epic="x", phases=[1]))
    assert isinstance(in_x, lr.Unreadable) and (in_x.branch, in_x.epic) == ("init/a", "x")
    in_none = lr.read_row("a", _good("a", epic=None, phases=[1]))
    assert isinstance(in_none, lr.Unreadable) and in_none.epic == "", "a null epic: in no epic"
    unsaid = lr.read_row("a", _good("a", phases=[1]))
    assert isinstance(unsaid, lr.Unreadable) and unsaid.epic is None, "no epic key: unknown"
    garbled = lr.read_row("a", _good("a", epic=7))
    assert isinstance(garbled, lr.Unreadable) and garbled.epic is None, "a malformed epic: unknown"
    blind = lr.read_row("a", "{not json")
    assert isinstance(blind, lr.Unreadable) and blind.epic is None


def test_an_epic_waits_on_an_unreadable_row_that_may_be_its_member_and_unknown_means_maybe() -> None:
    """Finding 2: a row whose metadata said nothing counted as "not in this epic", and the epic opened early."""
    named = lr.Unreadable("m", "bad", epic="x")
    assert lr.maybe_in_epic(named, "x", None) and not lr.maybe_in_epic(named, "y", "y"), "its own JSON decides"
    assert not lr.maybe_in_epic(lr.Unreadable("m", "bad", epic=""), "x", None), "its JSON says it is in no epic"
    unknown = lr.Unreadable("m", "record_json is not JSON")
    assert lr.maybe_in_epic(unknown, "x", None), "no file, or a file naming no epic: maybe in this one"
    assert lr.maybe_in_epic(unknown, "x", "x") and not lr.maybe_in_epic(unknown, "x", "y"), "a file naming an epic decides"


def test_an_unreadable_row_of_another_epic_never_holds_a_land_in_this_one() -> None:
    """Finding 3: any row with no branch, anywhere in the store, held every upper layer of every stacked epic."""
    up: lr.LandingRecord = {"initiative": "up", "branch": "init/up", "parent": "init/low", "state": "building", "epic": "x"}
    other = lr.Unreadable("elsewhere", "phases is malformed", epic="y")
    assert lr.layer_below(up, lr.Listing((up,), (other,)), stacked=True, epic_branch="epic-x") is None
    same = lr.Unreadable("low", "phases is malformed", epic="x")
    assert lr.layer_below(up, lr.Listing((up,), (same,)), stacked=True, epic_branch="epic-x") == same
    unknown = lr.Unreadable("low", "record_json is not JSON")
    assert lr.layer_below(up, lr.Listing((up,), (other, unknown)), stacked=True, epic_branch="epic-x") == unknown


def test_the_run_holds_when_the_layer_below_may_be_a_row_that_does_not_read() -> None:
    """Finding 4: the epic driver read an unreadable layer as absent and took forward a branch that may be gone."""
    low: lr.LandingRecord = {"initiative": "low", "branch": "init/low", "parent": "epic-x", "state": "landed", "epic": "x"}
    up: lr.LandingRecord = {"initiative": "up", "branch": "init/up", "parent": "init/low", "state": "building", "epic": "x"}
    assert lr.stack_resolved_parent(up, lr.Listing((low, up)), stacked=True, epic_branch="epic-x") == ("epic-x", None)
    bad = lr.Unreadable("low", "phases is malformed", "init/low", "x")
    assert lr.stack_resolved_parent(up, lr.Listing((up,), (bad,)), stacked=True, epic_branch="epic-x") == ("init/low", bad)
    top: lr.LandingRecord = {"initiative": "top", "branch": "init/top", "parent": "init/mid", "state": "building", "epic": "x"}
    mid: lr.LandingRecord = {"initiative": "mid", "branch": "init/mid", "parent": "init/low", "state": "landed", "epic": "x"}
    assert lr.stack_resolved_parent(top, lr.Listing((mid, top), (bad,)), stacked=True, epic_branch="epic-x") == (
        "init/low", bad), "a layer further down that does not read stops the walk there"


def test_a_needed_initiative_whose_record_does_not_read_makes_the_stack_base_unknown() -> None:
    members = {"a": {"branch": "init/a", "landed": False}, "c": {"branch": "init/c", "landed": True}}
    unread = {"b": "record_json is not JSON"}
    held = stack_unknown(["b", "a"], members, unread)
    assert held is not None and "b" in held and "record_json is not JSON" in held, "b may be the layer: it comes first"
    assert stack_unknown(["c", "b"], members, unread) is not None, "a landed member is no answer, so b still counts"
    assert stack_unknown(["a", "b"], members, unread) is None, "a is the answer before b is reached"
    assert stack_unknown(["a"], members, {}) is None and stack_unknown([], members, unread) is None
