from pathlib import Path

import pytest

from agent_tools.chair_read_cache import (
    StampCache,
    dump_entries,
    file_stamp,
    load_cache,
    load_entries,
    save_cache,
)


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


def reloaded(cache, max_entries=4):
    fresh = StampCache(max_entries)
    return fresh, load_entries(fresh, dump_entries(cache))


def test_dump_then_load_gives_a_hit_with_the_same_value():
    cache = StampCache(4)
    cache.get("a", (1, 2), lambda: {"x": [1, 2]})
    fresh, loaded = reloaded(cache)
    read, calls = counting("unused")
    assert loaded == 1
    assert fresh.reads == (0, 0)
    assert fresh.get("a", (1, 2), read) == {"x": [1, 2]}
    assert calls == []
    assert fresh.reads == (1, 0)


def test_loaded_entry_with_changed_stamp_is_a_miss():
    cache = StampCache(4)
    cache.get("a", (1, 2), lambda: "old")
    fresh, _ = reloaded(cache)
    assert fresh.get("a", (9, 2), lambda: "new") == "new"
    assert fresh.reads == (0, 1)


def test_tuple_valued_entry_is_skipped():
    cache = StampCache(4)
    cache.get("kept", (1, 1), lambda: ["a"])
    cache.get("dropped", (1, 1), lambda: ("a",))
    assert [e["key"]["v"] for e in dump_entries(cache)["entries"]] == ["kept"]


def test_path_and_tuple_keys_survive():
    cache = StampCache(4)
    cache.get(Path("/x/y"), (1, 1), lambda: "p")
    cache.get(("a", "b"), (2, 2), lambda: "t")
    fresh, loaded = reloaded(cache)
    assert loaded == 2
    assert fresh.get(Path("/x/y"), (1, 1), lambda: "unused") == "p"
    assert fresh.get(("a", "b"), (2, 2), lambda: "unused") == "t"
    assert fresh.reads == (2, 0)


def test_load_respects_max_entries_and_keeps_the_newest():
    cache = StampCache(4)
    for key in "abc":
        cache.get(key, (1, 1), lambda key=key: key)
    fresh, loaded = reloaded(cache, max_entries=2)
    assert (loaded, len(fresh)) == (2, 2)
    assert fresh.get("c", (1, 1), lambda: "unused") == "c"
    assert fresh.get("a", (1, 1), lambda: "reread") == "reread"


def test_undecodable_entry_is_ignored():
    good = {"key": {"t": "str", "v": "a"}, "stamp": [1, 1], "value": "v"}
    bad = {"key": {"t": "zzz", "v": "b"}, "stamp": [1, 1], "value": "v"}
    fresh = StampCache(4)
    assert load_entries(fresh, {"version": 1, "entries": [bad, good, 7]}) == 1
    assert len(fresh) == 1


@pytest.mark.parametrize("doc", [{"version": 2, "entries": []}, [], {"version": 1}, None])
def test_wrong_version_or_shape_loads_zero(doc):
    fresh = StampCache(4)
    assert load_entries(fresh, doc) == 0
    assert len(fresh) == 0


def test_missing_and_malformed_files_load_zero(tmp_path):
    fresh = StampCache(4)
    assert load_cache(fresh, tmp_path / "missing.json") == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_cache(fresh, bad) == 0
    assert len(fresh) == 0


def test_save_then_load_through_a_file_hits_on_first_get(tmp_path):
    cache = StampCache(4)
    cache.get(Path("/x/y"), (1, 2), lambda: ["v"])
    target = tmp_path / "cache.json"
    save_cache(cache, target)
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]
    fresh = StampCache(4)
    assert load_cache(fresh, target) == 1
    assert fresh.get(Path("/x/y"), (1, 2), lambda: "unused") == ["v"]
    assert fresh.reads == (1, 0)


def test_save_into_a_missing_directory_does_not_raise(tmp_path):
    save_cache(StampCache(4), tmp_path / "no" / "such" / "cache.json")
