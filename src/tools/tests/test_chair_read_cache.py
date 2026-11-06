import pytest

from agent_tools.chair_read_cache import StampCache, file_stamp


def counting(value):
    calls = []

    def read():
        calls.append(1)
        return value

    return read, calls


def test_same_stamp_returns_stored_value_without_reading():
    cache = StampCache(4)
    read, calls = counting("v1")
    assert cache.get("a", (1, 2), read) == "v1"
    assert cache.get("a", (1, 2), read) == "v1"
    assert len(calls) == 1
    assert cache.reads == (1, 1)


def test_changed_stamp_rereads():
    cache = StampCache(4)
    cache.get("a", (1, 2), lambda: "old")
    assert cache.get("a", (3, 2), lambda: "new") == "new"
    assert cache.get("a", (3, 2), lambda: "unused") == "new"


def test_none_stamp_is_not_stored():
    cache = StampCache(4)
    read, calls = counting("v")
    cache.get("a", None, read)
    cache.get("a", None, read)
    assert len(calls) == 2
    assert len(cache) == 0


def test_keep_drops_absent_keys():
    cache = StampCache(4)
    for key in "abc":
        cache.get(key, (1, 1), lambda key=key: key)
    cache.keep(["a"])
    assert len(cache) == 1
    read, calls = counting("x")
    cache.get("a", (1, 1), read)
    cache.get("b", (1, 1), read)
    cache.get("c", (1, 1), read)
    assert len(calls) == 2


def test_bound_evicts_least_recently_used():
    cache = StampCache(2)
    cache.get("a", (1, 1), lambda: "a")
    cache.get("b", (1, 1), lambda: "b")
    cache.get("a", (1, 1), lambda: "unused")
    cache.get("c", (1, 1), lambda: "c")
    assert len(cache) == 2
    read, calls = counting("b2")
    assert cache.get("b", (1, 1), read) == "b2"
    assert len(calls) == 1


def test_max_entries_must_be_positive():
    with pytest.raises(ValueError):
        StampCache(0)


def test_file_stamp(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("abc")
    stamp = file_stamp(path)
    assert stamp is not None and stamp[1] == 3
    assert file_stamp(tmp_path / "missing") is None
