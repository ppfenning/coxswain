import json

from agent_tools.notify_cards import pending_cards

_BUS = {"from": "chair", "to": "operator", "ack": False, "options": ["ship", "hold"], "context": "c"}
_ASK_1 = _BUS | {"ref": "coxswain://decision/d-1", "id": "d-1", "note": "Ship it?", "asked_at": "2026-10-06T10:00:00Z"}
_ASK_2 = _BUS | {"ref": "coxswain://decision/d-2", "id": "d-2", "note": "Hold it?", "asked_at": "2026-10-06T09:00:00Z"}


def _write(tmp_path, lines):
    (tmp_path / "courier.jsonl").write_bytes("".join(f"{line}\n" for line in lines).encode())


def _ids(tmp_path):
    return sorted(card["id"] for card in pending_cards(tmp_path))


def test_an_all_null_line_keeps_the_good_card(tmp_path):
    _write(tmp_path, [json.dumps(_ASK_1), "\x00" * 8])
    assert _ids(tmp_path) == ["d-1"]


def test_a_card_line_with_embedded_nuls_is_stripped_and_returned(tmp_path):
    nulled = json.dumps(_ASK_2).replace('"id"', '"\x00id\x00"')
    _write(tmp_path, [json.dumps(_ASK_1), nulled])
    assert _ids(tmp_path) == ["d-1", "d-2"]


def test_a_non_object_and_a_torn_line_beside_a_good_card(tmp_path):
    _write(tmp_path, [json.dumps(_ASK_1), "[1, 2]", "{not json"])
    assert _ids(tmp_path) == ["d-1"]
