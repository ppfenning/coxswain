import argparse
import json

from agent_tools import courier
from agent_tools.cli import _courier_send


def _setup(tmp_path):
    ws = tmp_path / "workspace"
    task = ws / "runs" / "r1" / "tasks" / "t"
    task.mkdir(parents=True)
    (task / "x.json").write_text(json.dumps({"id": "r1"}), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\n")
    return str(profile), ws / "courier.jsonl"


def _send(profile, ref="coxswain://run/r1", **flags):
    base = {"profile": profile, "ref": ref, "to": "cos", "note": "n"}
    return _courier_send(argparse.Namespace(**{**base, **flags}))


def _lines(bus):
    return [json.loads(line) for line in bus.read_text(encoding="utf-8").splitlines()]


def test_run_exited_send_with_three_fields_appends_one_entry_parse_run_exited_reads(tmp_path):
    profile, bus = _setup(tmp_path)
    assert _send(profile, event="run_exited", field=["run=r1", "initiative=i", "outcome=merged"]) == 0
    (entry,) = _lines(bus)
    assert courier.parse_run_exited(entry) == ("r1", "i", "merged")


def test_unknown_event_returns_2_and_leaves_the_bus_untouched(tmp_path, capsys):
    profile, bus = _setup(tmp_path)
    assert _send(profile) == 0
    before = bus.read_bytes()
    assert _send(profile, event="nope") == 2
    assert capsys.readouterr().out.splitlines()[-1].startswith("courier:")
    assert bus.read_bytes() == before


def test_field_without_an_equals_sign_returns_2_and_leaves_the_bus_untouched(tmp_path, capsys):
    profile, bus = _setup(tmp_path)
    assert _send(profile, event="run_exited", field=["bare"]) == 2
    assert capsys.readouterr().out.startswith("courier:")
    assert not bus.exists()


def test_run_exited_send_still_refuses_a_reference_that_does_not_resolve(tmp_path, capsys):
    profile, bus = _setup(tmp_path)
    assert _send(profile, ref="coxswain://run/missing", event="run_exited", field=["run=missing"]) == 2
    assert "does not resolve" in capsys.readouterr().out
    assert not bus.exists()


def test_plain_send_carries_only_the_original_keys(tmp_path):
    profile, bus = _setup(tmp_path)
    assert _send(profile) == 0
    (entry,) = _lines(bus)
    assert sorted(entry) == ["ack", "from", "id", "note", "ref", "to"]
