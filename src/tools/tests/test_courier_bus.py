"""The courier bus behind its old path: a file for a machine on its own, the store's log for a shared store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_tools import courier, courier_bus


def entry(ident: str, to: str = "chair", **more) -> dict:
    return {"ref": "coxswain://initiative/demo", "from": "lander", "to": to, "note": f"note {ident}", "id": ident, "ack": False, **more}


@pytest.fixture
def shared(tmp_path: Path, monkeypatch) -> Path:
    """A workspace whose bus lives in the store. The store here is a sqlite file; the bus code is the same for any store."""
    (tmp_path / "runs").mkdir()
    url = f"sqlite:///{tmp_path / 'runs' / 'shared.db'}"
    monkeypatch.setattr(courier_bus, "store_url", lambda path: url)
    monkeypatch.setattr(courier_bus, "uses_store", lambda path: True)
    monkeypatch.setattr(courier_bus, "_CONNECTIONS", {})
    monkeypatch.setattr(courier_bus, "_READERS", {})
    monkeypatch.setattr(courier_bus, "_IMPORTED", set())
    return tmp_path / "courier.jsonl"


def test_without_a_shared_store_the_bus_is_the_file_as_before(tmp_path: Path) -> None:
    path = tmp_path / "courier.jsonl"
    assert courier_bus.uses_store(path) is False, "the test profile names no shared store"
    assert courier.read_text(path) == ""
    courier.append_file(path, entry("m1"))
    assert json.loads(path.read_text().splitlines()[0])["id"] == "m1"
    assert courier.ack_file(path, "m1") is True and courier.ack_file(path, "nope") is False
    assert courier.inbox(courier.read_text(path)) == []
    assert courier.stamp(path)[1] == path.stat().st_size


def test_with_a_shared_store_the_bus_is_the_stores_log_and_the_file_is_not_written(shared: Path) -> None:
    courier.append_file(shared, entry("m1"))
    courier.append_file(shared, entry("m2", to="operator", event="needs_attention", topic="demo"))
    assert not shared.exists(), "nothing is written beside the workspace"
    assert [e["id"] for e in courier.entries(courier.read_text(shared))] == ["m1", "m2"]
    assert [e["id"] for e in courier.inbox(courier.read_text(shared), topic="demo")] == ["m2"]
    assert courier.ack_file(shared, "m1", "console") is True
    assert courier.ack_file(shared, "m1") is True
    assert courier.ack_file(shared, "missing") is False
    assert [e["id"] for e in courier.inbox(courier.read_text(shared))] == ["m2"], "an ack is one more line, folded on read"
    assert courier.stamp(shared) == (str(shared), "store", 4)


def test_the_first_use_of_the_log_loads_what_the_file_already_held_once(shared: Path) -> None:
    shared.write_text(json.dumps(entry("old-1")) + "\n" + json.dumps(entry("old-2")) + "\n" + json.dumps({**entry("old-1"), "ack": True}) + "\n")
    courier.append_file(shared, entry("new-1"))
    seen = courier.entries(courier.read_text(shared))
    assert [e["id"] for e in seen] == ["old-1", "old-2", "new-1"] and seen[0]["ack"] is True
    courier_bus._IMPORTED.clear()  # another process starts
    assert courier_bus.line_count(shared) == 4, "the file is not loaded a second time"


def test_a_store_that_cannot_be_read_is_an_empty_bus_and_a_failed_send_raises(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(courier_bus, "uses_store", lambda path: True)
    monkeypatch.setattr(courier_bus, "store_url", lambda path: "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")
    monkeypatch.setattr(courier_bus, "_CONNECTIONS", {})
    monkeypatch.setattr(courier_bus, "_READERS", {})
    assert courier.read_text(tmp_path / "courier.jsonl") == ""
    assert "courier not read from the store" in capsys.readouterr().err
    with pytest.raises(Exception):  # noqa: B017 -- whatever the driver raises, a message is never dropped quietly
        courier.append_file(tmp_path / "courier.jsonl", entry("m1"))


def test_a_read_never_migrates_and_its_session_refuses_writes_while_a_write_migrates() -> None:
    assert courier_bus.opening("read") == courier_bus.Opening(migrate=False, read_only=True)
    assert courier_bus.opening("write") == courier_bus.Opening(migrate=True, read_only=False)


def test_a_reader_sees_the_store_lines_or_while_the_log_is_missing_or_empty_the_file_lines() -> None:
    assert courier_bus.readable(["s1", "s2"], ["f1"]) == ["s1", "s2"]
    assert courier_bus.readable([], ["f1"]) == ["f1"], "an empty log shows what the first write will load"
    assert courier_bus.readable(None, ["f1"]) == ["f1"], "a missing log shows the file too"
    assert courier_bus.readable(None, []) == []
    assert courier_bus.readable_count(3, 1) == 3
    assert courier_bus.readable_count(0, 2) == 2
    assert courier_bus.readable_count(None, 2) == 2
    assert courier_bus.readable_count(None, 0) == 0


def _tables(db: Path) -> list[str]:
    import sqlite3

    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


def test_reading_a_store_no_writer_migrated_shows_the_file_and_creates_no_table(shared: Path) -> None:
    shared.write_text(json.dumps(entry("old-1")) + "\n\n" + json.dumps(entry("old-2")) + "\n")
    assert [e["id"] for e in courier.entries(courier.read_text(shared))] == ["old-1", "old-2"]
    assert courier.stamp(shared) == (str(shared), "store", 2)
    assert _tables(shared.parent / "runs" / "shared.db") == [], "a read made no table: no migration ran"


def test_reading_an_empty_log_shows_the_file_and_loads_nothing_until_the_first_write(shared: Path) -> None:
    courier.append_file(shared, entry("m0"))  # a write migrates; then the log is emptied, as on a store just set up
    db = shared.parent / "runs" / "shared.db"
    courier_bus._CONNECTIONS[courier_bus.store_url(shared)].execute("DELETE FROM courier_log")
    courier_bus._IMPORTED.clear()
    shared.write_text(json.dumps(entry("old-1")) + "\n")
    assert [e["id"] for e in courier.entries(courier.read_text(shared))] == ["old-1"]
    assert courier_bus.line_count(shared) == 1
    import sqlite3

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM courier_log").fetchone()[0] == 0, "a read loaded nothing"
    courier.append_file(shared, entry("new-1"))
    assert [e["id"] for e in courier.entries(courier.read_text(shared))] == ["old-1", "new-1"]


def test_a_reader_connection_refuses_a_write(shared: Path) -> None:
    courier.read_text(shared)
    reader = courier_bus._READERS[courier_bus.store_url(shared)]
    with pytest.raises(Exception, match="readonly|read-only|query_only"):
        reader.execute("CREATE TABLE nope (x INTEGER)")
