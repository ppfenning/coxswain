from agent_tools.chair_schedule_durations import Estimate, estimate_duration, median, span_seconds


def _rec(size, mix, seconds):
    return {
        "size": size,
        "mix": mix,
        "build_start": "2026-11-05T10:00:00+00:00",
        "approve_time": f"2026-11-05T10:{seconds // 60:02d}:{seconds % 60:02d}+00:00",
    }


RECORDS = [_rec("s", "a", 10), _rec("s", "a", 30), _rec("s", "a", 20), _rec("s", "b", 50), _rec("m", "a", 40)]


def test_exact_match_median():
    assert estimate_duration(RECORDS, "s", "a", 999) == Estimate(20.0, "size_mix", 3)


def test_falls_back_to_same_size_any_mix():
    assert estimate_duration(RECORDS, "s", "z", 999) == Estimate(25.0, "size", 4)


def test_falls_back_to_all_records():
    assert estimate_duration(RECORDS, "l", "a", 999) == Estimate(30.0, "all", 5)


def test_falls_back_to_default():
    assert estimate_duration([], "s", "a", 999) == Estimate(999, "default", 0)


def test_bad_records_are_ignored():
    negative = {**_rec("s", "a", 10), "build_start": "2026-11-05T11:00:00+00:00"}
    missing = {"size": "s", "mix": "a", "build_start": "2026-11-05T10:00:00+00:00"}
    unparseable = {**_rec("s", "a", 10), "approve_time": "soon"}
    assert span_seconds(negative) is None
    assert span_seconds(missing) is None
    assert span_seconds(unparseable) is None
    assert estimate_duration([negative, missing, unparseable, _rec("s", "a", 10)], "s", "a", 999) == Estimate(
        10.0, "size_mix", 1
    )
    assert estimate_duration([negative, missing], "s", "a", 999) == Estimate(999, "default", 0)


def test_zero_span_is_valid():
    assert span_seconds(_rec("s", "a", 0)) == 0.0


def test_even_count_takes_mean_of_middle_pair():
    assert median([10, 20, 30, 40]) == 25.0
