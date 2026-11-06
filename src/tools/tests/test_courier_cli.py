import argparse
import json

from agent_tools.cli import _courier_ack, _courier_inbox, _courier_send, _inbox_line, _parse_fields


def _setup(tmp_path):
    ws = tmp_path / "workspace"
    task = ws / "runs" / "r1" / "tasks" / "t"
    task.mkdir(parents=True)
    (task / "x.json").write_text(json.dumps({"id": "r1"}), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\n")
    return str(profile), ws


def _send(profile, note="n", **flags):
    base = {"profile": profile, "ref": "coxswain://run/r1", "to": "cos", "note": note}
    return _courier_send(argparse.Namespace(**{**base, **flags}))


def _inbox(profile, **flags):
    return _courier_inbox(argparse.Namespace(**{"profile": profile, "label": "cos", **flags}))


def _lines(ws):
    return [json.loads(line) for line in (ws / "courier.jsonl").read_text(encoding="utf-8").splitlines()]


def test_parse_fields_splits_on_the_first_equals_and_refuses_a_bare_word():
    assert _parse_fields(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    assert _parse_fields(["oops"]) is None
    assert _parse_fields(["=v"]) is None


def test_a_typed_send_is_found_by_topic_and_event_and_no_other_entry_is(tmp_path, capsys):
    profile, ws = _setup(tmp_path)
    assert _send(profile, note="wanted", event="stuck", topic="epic-1", field=["k=v"]) == 0
    assert _send(profile, note="other topic", event="stuck", topic="epic-2") == 0
    assert _send(profile, note="other event", event="level_merged", topic="epic-1") == 0
    assert _send(profile, note="untyped") == 0
    assert _lines(ws)[0]["fields"] == {"k": "v"}
    capsys.readouterr()
    assert _inbox(profile, topic="epic-1", event="stuck") == 0
    out = capsys.readouterr().out
    assert "wanted" in out and "[event=stuck topic=epic-1]" in out
    assert out.count("\n") == 1


def test_a_wake_inbox_prints_a_stuck_entry_and_omits_a_level_merged_one(tmp_path, capsys):
    profile, _ = _setup(tmp_path)
    _send(profile, note="is stuck", event="stuck")
    _send(profile, note="is merged", event="level_merged")
    capsys.readouterr()
    _inbox(profile, wake=True)
    out = capsys.readouterr().out
    assert "is stuck" in out and "is merged" not in out


def test_two_consumers_each_see_an_entry_until_each_has_acked_it(tmp_path, capsys):
    profile, ws = _setup(tmp_path)
    _send(profile, note="shared")
    entry_id = _lines(ws)[0]["id"]
    capsys.readouterr()

    def seen(consumer):
        _inbox(profile, consumer=consumer)
        return "shared" in capsys.readouterr().out

    assert seen("a") and seen("b")
    assert _courier_ack(argparse.Namespace(profile=profile, id=entry_id, consumer="a")) == 0
    assert not seen("a") and seen("b")
    assert _courier_ack(argparse.Namespace(profile=profile, id=entry_id, consumer="b")) == 0
    assert not seen("a") and not seen("b")


def test_a_bad_field_pair_or_unknown_event_prints_a_courier_message_and_returns_2(tmp_path, capsys):
    profile, ws = _setup(tmp_path)
    assert _send(profile, field=["oops"]) == 2
    assert capsys.readouterr().out.startswith("courier:")
    assert _send(profile, event="nonsense") == 2
    assert capsys.readouterr().out.startswith("courier:")
    assert not (ws / "courier.jsonl").exists()


def test_without_the_new_flags_send_inbox_and_ack_print_and_write_what_they_did_before(tmp_path, capsys):
    profile, ws = _setup(tmp_path)
    assert _send(profile, note="plain") == 0
    entry = _lines(ws)[0]
    assert set(entry) == {"ref", "from", "to", "note", "id", "ack"}
    capsys.readouterr()
    _inbox(profile)
    assert capsys.readouterr().out == f"{entry['id']}: cli -> cos: plain (coxswain://run/r1)\n"
    assert _inbox_line(entry) == f"{entry['id']}: cli -> cos: plain (coxswain://run/r1)"
    assert _courier_ack(argparse.Namespace(profile=profile, id=entry["id"])) == 0
    assert _lines(ws)[1] == {**entry, "ack": True}
    _inbox(profile)
    assert capsys.readouterr().out == ""
