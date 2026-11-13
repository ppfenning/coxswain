from agent_tools.dash_feed_memo import EMPTY, reuse, split_reuse


def _boom():
    raise AssertionError("compute must not be called")


def test_hit_returns_stored_value_without_calling_compute():
    memo = {"runs": ("k1", [1, 2])}
    value, new, reused = reuse(memo, "runs", "k1", _boom)
    assert (value, reused) == ([1, 2], True)
    assert new == memo


def test_changed_key_calls_compute_once_and_stores_result():
    calls = []

    def compute():
        calls.append(1)
        return "fresh"

    value, new, reused = reuse({"runs": ("k1", "old")}, "runs", "k2", compute)
    assert (value, reused, len(calls)) == ("fresh", False, 1)
    assert new["runs"] == ("k2", "fresh")


def test_empty_memo_is_a_miss():
    value, new, reused = reuse(EMPTY, "spend", ("fp", 0), lambda: 7)
    assert (value, reused, dict(new)) == (7, False, {"spend": (("fp", 0), 7)})


def test_unrelated_section_is_untouched():
    memo = {"runs": ("k1", "r"), "spend": ("s1", "s")}
    _, new, _ = reuse(memo, "spend", "s2", lambda: "s-new")
    assert new["runs"] == ("k1", "r")
    assert new["spend"] == ("s2", "s-new")


def test_old_memo_is_unchanged_after_a_miss():
    memo = {"runs": ("k1", "r")}
    reuse(memo, "runs", "k2", lambda: "r2")
    reuse(memo, "queue", "q", lambda: "q1")
    assert memo == {"runs": ("k1", "r")}


def test_returned_memo_is_read_only():
    _, new, _ = reuse(EMPTY, "runs", "k", lambda: 1)
    try:
        new["runs"] = ("x", 2)
    except TypeError:
        pass
    else:
        raise AssertionError("memo accepted a write")
    assert dict(EMPTY) == {}


def test_time_floored_key_hits_within_a_step_and_misses_across_it():
    step = 60

    def key(t):
        return ("fp", t - t % step)

    _, memo, _ = reuse(EMPTY, "history", key(120), lambda: "a")
    _, _, same = reuse(memo, "history", key(179), _boom)
    _, _, later = reuse(memo, "history", key(180), lambda: "b")
    assert (same, later) == (True, False)


def test_split_reuse_orders_by_names_and_drops_unknown():
    results = [("live", True), ("chair", False), ("zzz", False), ("spend", True)]
    got = split_reuse(["chair", "spend", "runs", "live"], results)
    assert got == (["chair"], ["spend", "live"])
