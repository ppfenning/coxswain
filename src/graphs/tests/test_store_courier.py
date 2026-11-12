"""The courier log in the store: lines go in once and come back in order."""
import pytest

from harness import store_courier
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate


@pytest.fixture
def conn(store_url):
    c = connect(store_url)
    migrate(c, "2026-11-12T00:00:00Z", default_modules())
    yield c
    c.close()


def test_lines_come_back_oldest_first_and_a_repeated_line_id_is_added_once(conn):
    assert store_courier.lines(conn) == [] and store_courier.count(conn) == 0
    assert store_courier.append(conn, "b", "2026-11-12T10:00:01.000000Z", '{"id": "m2"}') is True
    assert store_courier.append(conn, "a", "2026-11-12T10:00:00.000000Z", '{"id": "m1"}') is True
    assert store_courier.append(conn, "a", "2026-11-12T10:00:09.000000Z", '{"id": "changed"}') is False
    assert store_courier.lines(conn) == ['{"id": "m1"}', '{"id": "m2"}']
    added = store_courier.append_many(conn, [("a", "x", "dup"), ("c", "2026-11-12T10:00:01.000000Z", '{"id": "m3"}')])
    assert added == 1 and store_courier.count(conn) == 3
    assert store_courier.lines(conn)[1:] == ['{"id": "m2"}', '{"id": "m3"}'], "the same instant orders by line id"
