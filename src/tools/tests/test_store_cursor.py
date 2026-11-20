from agent_tools import chair_tick_cache, store_cursor


def test_seqs_reads_mapping_rows_and_plain_pairs_alike():
    assert store_cursor.seqs([{"name": "*", "seq": 7}, {"name": "work_items", "seq": 3}]) == {"*": 7, "work_items": 3}
    assert store_cursor.seqs([("hosts", 2)]) == {"hosts": 2}
    assert store_cursor.seqs([]) == {}


def test_the_cursor_replaces_the_statistics_of_the_tables_it_watches_and_leaves_the_rest():
    statistics = {"work_items": 120, "leases": 40, "store_cursor": 9}
    assert store_cursor.merged(statistics, {"*": 5, "work_items": 2}) == {
        "work_items": 2, "leases": 40, "store_cursor": 9, "*": 5}
    assert store_cursor.merged(statistics, None) == statistics
    assert statistics == {"work_items": 120, "leases": 40, "store_cursor": 9}  # the caller's mapping is left alone


def test_a_reader_of_a_watched_table_is_keyed_by_its_seq_and_one_of_another_by_its_statistics():
    before = store_cursor.merged({"work_items": 120, "leases": 40}, {"*": 5, "work_items": 2})
    after = store_cursor.merged({"work_items": 120, "leases": 40}, {"*": 6, "work_items": 3})
    stamp = chair_tick_cache.table_stamp
    # The statistics have not caught up with the write; the cursor has, so the work_items reader reads again.
    assert stamp(("work_items",), before) != stamp(("work_items",), after)
    assert stamp(("leases",), before) == stamp(("leases",), after)
