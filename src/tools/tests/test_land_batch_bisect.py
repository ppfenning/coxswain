"""Bisecting a failing batch: counts and orders of the lands, the landed sources and the returned ones."""

from agent_tools.land_batch_bisect import BisectResult, bisect_land

DETAIL = "ci red"


def fake_land(bad: set[str]):
    """A land that records every batch it is given and fails whenever the batch holds a bad source."""
    calls: list[list[str]] = []

    def land(batch: list[str]) -> str | None:
        calls.append(list(batch))
        return DETAIL if bad & set(batch) else None

    return land, calls


def test_one_bad_in_three_takes_five_calls():
    land, calls = fake_land({"b"})
    result = bisect_land(["a", "b", "c"], land)
    assert result == BisectResult(landed=["a", "c"], returned=[("b", DETAIL)], ci_runs=5)
    assert calls == [["a", "b", "c"], ["a", "b"], ["a"], ["b"], ["c"]]


def test_none_bad_is_one_call():
    land, calls = fake_land(set())
    result = bisect_land(["a", "b", "c"], land)
    assert result == BisectResult(landed=["a", "b", "c"], returned=[], ci_runs=1)
    assert calls == [["a", "b", "c"]]


def test_single_bad_source_is_returned_after_one_call():
    land, calls = fake_land({"x"})
    result = bisect_land(["x"], land)
    assert result == BisectResult(landed=[], returned=[("x", DETAIL)], ci_runs=1)
    assert calls == [["x"]]


def test_all_bad_returned_in_input_order():
    land, calls = fake_land({"a", "b", "c"})
    result = bisect_land(["a", "b", "c"], land)
    assert result.landed == []
    assert result.returned == [("a", DETAIL), ("b", DETAIL), ("c", DETAIL)]
    assert result.ci_runs == len(calls) == 5


def test_left_half_takes_the_larger_share():
    land, calls = fake_land({"a", "b", "c", "d", "e"})
    bisect_land(["a", "b", "c", "d", "e"], land)
    assert calls[:2] == [["a", "b", "c", "d", "e"], ["a", "b", "c"]]


def test_landed_follows_the_order_they_landed_in():
    land, calls = fake_land({"c"})
    result = bisect_land(["a", "b", "c", "d"], land)
    assert result.landed == ["a", "b", "d"]
    assert result.returned == [("c", DETAIL)]
    assert calls == [["a", "b", "c", "d"], ["a", "b"], ["c", "d"], ["c"], ["d"]]
    assert result.ci_runs == 5


def test_left_half_finishes_before_the_right_starts():
    land, calls = fake_land({"a", "d"})
    result = bisect_land(["a", "b", "c", "d"], land)
    assert calls == [["a", "b", "c", "d"], ["a", "b"], ["a"], ["b"], ["c", "d"], ["c"], ["d"]]
    assert result.landed == ["b", "c"]
    assert result.returned == [("a", DETAIL), ("d", DETAIL)]


def test_empty_list_makes_no_call():
    def land(batch: list[str]) -> str | None:
        raise AssertionError("land must not be called")

    assert bisect_land([], land) == BisectResult(landed=[], returned=[], ci_runs=0)


def test_sources_are_opaque():
    bad, good = object(), object()
    result = bisect_land([good, bad], lambda batch: DETAIL if bad in batch else None)
    assert result.landed == [good]
    assert result.returned == [(bad, DETAIL)]
