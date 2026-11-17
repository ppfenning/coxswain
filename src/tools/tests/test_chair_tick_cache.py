"""The reader cache's decisions, on example counters: when a reader's rows still stand, and which readers a write moves."""

from agent_tools.chair_tick_cache import MAX_AGE_S, ReaderCache, is_fresh, store_note, table_stamp
from agent_tools.run_store import rows_by_task

READERS = {"work_items": ("work_items",), "queue": ("work_items",), "hosts": ("hosts",), "newest_run_hosts": ("runs",)}
QUIET = {"work_items": 10, "hosts": 2, "runs": 7, "chair_actions": 40}


def test_table_stamp_reads_only_the_named_tables_in_name_order():
    assert table_stamp(("runs", "hosts"), QUIET) == (("hosts", 2), ("runs", 7))
    assert table_stamp(("missing",), QUIET) == (("missing", -1),)
    assert table_stamp(("hosts",), None) is None


def _reads_after(counters: dict[str, int]) -> set[str]:
    """Which readers go to the store on a second tick under `counters`, after a first tick under QUIET."""
    cache, read = ReaderCache(lambda: 0.0), []
    for counts in (QUIET, counters):
        for name, tables in READERS.items():
            cache.get(name, tables, counts, lambda name=name: read.append(name))
    return set(read[len(READERS):])


def test_a_quiet_store_rereads_no_reader():
    assert _reads_after(dict(QUIET)) == set()


def test_one_write_rereads_exactly_the_readers_of_its_table():
    assert _reads_after({**QUIET, "work_items": 11}) == {"work_items", "queue"}
    assert _reads_after({**QUIET, "hosts": 3}) == {"hosts"}
    assert _reads_after({**QUIET, "chair_actions": 41}) == set()  # no stamped reader reads it


def test_unknown_counters_reread_every_reader():
    cache, read = ReaderCache(lambda: 0.0), []
    for _ in range(2):
        for name, tables in READERS.items():
            cache.get(name, tables, None, lambda name=name: read.append(name))
    assert len(read) == 2 * len(READERS)


def test_an_entry_stands_while_its_stamp_holds_and_it_is_younger_than_the_age_bound():
    stamp = table_stamp(("hosts",), QUIET)
    assert is_fresh(stamp, 100.0, stamp, 100.0 + MAX_AGE_S - 1)
    assert not is_fresh(stamp, 100.0, stamp, 100.0 + MAX_AGE_S)
    assert not is_fresh(stamp, 100.0, table_stamp(("hosts",), {"hosts": 3}), 101.0)
    assert not is_fresh(None, 100.0, None, 101.0)
    assert not is_fresh(stamp, 100.0, stamp, 99.0)  # a clock that went back reads again


def test_the_cache_reads_once_per_stamp_and_counts_what_it_did():
    now = [0.0]
    reads: list[str] = []
    cache = ReaderCache(lambda: now[0])

    def read() -> list[str]:
        reads.append("x")
        return ["row"]

    assert cache.get("hosts", ("hosts",), QUIET, read) == ["row"]
    assert cache.get("hosts", ("hosts",), {**QUIET, "runs": 99}, read) == ["row"]  # another table moved
    assert len(reads) == 1
    assert cache.get("hosts", ("hosts",), {**QUIET, "hosts": 3}, read) == ["row"]
    now[0] = MAX_AGE_S + 1
    cache.get("hosts", ("hosts",), {**QUIET, "hosts": 3}, read)
    assert len(reads) == 3
    assert cache.tick_counts() == ({"hosts": 3}, {"hosts": 1})


def test_unknown_counters_store_nothing_and_a_new_key_replaces_the_old():
    cache = ReaderCache(lambda: 0.0)
    assert cache.get("hosts", ("hosts",), None, lambda: 1) == 1
    assert cache.get("hosts", ("hosts",), None, lambda: 2) == 2
    assert cache.get("newest", ("runs",), QUIET, lambda: "a", key=("i1",)) == "a"
    assert cache.get("newest", ("runs",), QUIET, lambda: "b", key=("i1", "i2")) == "b"
    assert cache.get("newest", ("runs",), QUIET, lambda: "c", key=("i1",)) == "c"  # the first key was dropped


def test_store_note_names_the_readers_one_tick_read():
    before = ({"hosts": 1, "queue": 1}, {"hosts": 0})
    assert store_note(before, ({"hosts": 1, "queue": 2}, {"hosts": 1})) == "store: read=queue reused=1"
    assert store_note(before, before) == "store: read=none reused=0"


def test_rows_by_task_keeps_the_first_row_of_each_key_as_task_state_does():
    rows = [{"initiative": "a", "task_id": "t1", "state": "ready"}, {"initiative": "a", "task_id": "t1", "state": "done"}]
    assert rows_by_task(rows) == {("a", "t1"): rows[0]}
