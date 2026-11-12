"""Cartridge snapshots in the store: stored once under their sha and read back whole."""
import pytest

from harness import store_cartridges
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate


@pytest.fixture
def conn(store_url):
    c = connect(store_url)
    migrate(c, "2026-11-12T00:00:00Z", default_modules())
    yield c
    c.close()


def test_a_snapshot_is_stored_once_under_its_sha_and_read_back(conn):
    assert store_cartridges.get(conn, "s1") is None and not store_cartridges.has(conn, "s1")
    assert store_cartridges.put(conn, "s1", "2026-11-12T10:00:00Z", "chair-host", '{"a": 1}') is True
    assert store_cartridges.put(conn, "s1", "2026-11-12T11:00:00Z", "lane-a", '{"a": 2}') is False, "a sha is never rewritten"
    assert store_cartridges.get(conn, "s1") == '{"a": 1}' and store_cartridges.has(conn, "s1")
