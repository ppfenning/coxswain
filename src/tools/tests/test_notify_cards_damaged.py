import json

from agent_tools.notify_cards import pending_cards

_BUS = {"from": "chair", "to": "operator", "ack": False, "options": ["ship", "hold"], "context": "c"}
_ASK_1 = _BUS | {"ref": "coxswain://decision/d-1", "id": "d-1", "note": "Ship it?", "asked_at": "2026-10-06T10:00:00Z"}
_ASK_2 = _BUS | {"ref": "coxswain://decision/d-2", "id": "d-2", "note": "Hold it?", "asked_at": "2026-10-06T09:00:00Z"}
_NUL_ASK_2 = json.dumps(_ASK_2).replace('"d-2"', '"d\x00-2"')


def _write(tmp_path, lines):
    (tmp_path / "courier.jsonl").write_text("".join(f"{line}\n" for line in lines))


def _ids(tmp_path):
    return sorted(card["id"] for card in pending_cards(tmp_path))


def test_an_all_null_line_keeps_the_valid_cards(tmp_path):
    _write(tmp_path, [json.dumps(_ASK_1), "null", "\x00\x00\x00"])
    assert _ids(tmp_path) == ["d-1"]


def test_a_card_line_with_embedded_nuls_is_stripped_and_returned(tmp_path):
    _write(tmp_path, [json.dumps(_ASK_1), _NUL_ASK_2])
    assert _ids(tmp_path) == ["d-1", "d-2"]


def test_null_lines_and_a_nul_card_together_return_every_good_card(tmp_path):
    _write(tmp_path, ["null", json.dumps(_ASK_1), "\x00null\x00", _NUL_ASK_2, "{torn"])
    assert _ids(tmp_path) == ["d-1", "d-2"]
