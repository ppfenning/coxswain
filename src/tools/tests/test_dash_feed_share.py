from agent_tools.dash_feed_share import is_stale, publish, read_if_changed, release, try_own

_NS = 1_000_000_000


def test_second_try_own_fails_while_first_is_held(tmp_path):
    first = try_own(tmp_path)
    assert first is not None
    assert try_own(tmp_path) is None


def test_released_lock_lets_the_next_caller_own(tmp_path):
    release(try_own(tmp_path))
    assert try_own(tmp_path) is not None


def test_publish_then_read_returns_line_once(tmp_path):
    publish(tmp_path, '{"a": 1}\n')
    line, stamp = read_if_changed(tmp_path, None)
    assert line == '{"a": 1}\n'
    assert read_if_changed(tmp_path, stamp) == (None, stamp)
    assert [p.name for p in tmp_path.iterdir()] == ["feed.snapshot"]


def test_republish_with_new_size_is_read_again(tmp_path):
    publish(tmp_path, "one\n")
    _, stamp = read_if_changed(tmp_path, None)
    publish(tmp_path, "three\n")
    assert read_if_changed(tmp_path, stamp)[0] == "three\n"


def test_missing_snapshot_reads_nothing(tmp_path):
    assert read_if_changed(tmp_path, None) == (None, None)


def test_is_stale_boundary():
    assert not is_stale(0, 3 * 2 * _NS, 2)
    assert is_stale(0, 3 * 2 * _NS + 1, 2)
