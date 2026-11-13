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
    assert courier.read_text(tmp_path / "courier.jsonl") == ""
    assert "courier not read from the store" in capsys.readouterr().err
    with pytest.raises(Exception):  # noqa: B017 -- whatever the driver raises, a message is never dropped quietly
        courier.append_file(tmp_path / "courier.jsonl", entry("m1"))
