import argparse
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_tools import chair, chair_chat, chair_exec, chair_report, chair_run, cli, courier
from agent_tools.chair_run import RunDeps, tick

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)
LEASE = {"holder": "me", "host": "box", "epoch": 3, "mine": True, "released": False, "stale": False}
EPIC = {"epic_id": "i1", "branch_head": "h", "levels": [{"tasks": [{"id": "t1", "state": "ready"}], "merged": False, "phase": "p"}],
        "exited": True, "signature": "s", "reported": [], "land_at": "level"}
HOLD = ["target_kind=initiative", "target=i1"]
LEASE_FUNCTIONS = ("read", "write", "locked", "take", "renew_lease", "release_lease", "_read_lease", "clear")


def _facts(lease):
    return {
        "lease": lease, "chat_epics": [EPIC],
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
        "approved": [], "initiatives": [], "quarantines": [], "intake": [], "work_store_ready": False,
        "sources_configured": False, "last_housekeeping_at": NOW.isoformat(), "housekeeping_hours": 24,
    }


def _deps(ws: Path, lease=LEASE) -> RunDeps:
    exec_deps = chair_exec.Deps(
        run=lambda argv: (0, ""), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda h, o: "",
        record=lambda r: None, run_id=lambda action: "run-1", repo_for=lambda action: "r", launch_stagger_s=0.0,
    )
    return RunDeps(
        facts_deps=object(), exec_deps=exec_deps, report_deps=chair_report.Deps(echo=lambda line: None),
        beat=lambda: None, current_epoch=lambda: 3, holds=lambda: True, release=lambda: None, sleep=lambda s: None,
        now=lambda: NOW, gather=lambda d, n: _facts(lease), chat=cli._chat_deps(ws),
    )


class Rig:
    """A workspace with a real chair lock record, and a spy on every `chair` function that reads or writes it."""

    def __init__(self, tmp_path, monkeypatch):
        self.ws, self.runs, self.calls = tmp_path, tmp_path / "runs", []
        self.runs.mkdir()
        chair.write(self.runs, {"session": "me", "pid": 1, "host": "box"})
        self.before = self.snapshot()
        monkeypatch.setattr(cli, "_courier_workspace", lambda a: tmp_path)
        for name in LEASE_FUNCTIONS:
            monkeypatch.setattr(chair, name, self._spy(name, getattr(chair, name)))

    def _spy(self, name, real):
        return lambda *a, **k: (self.calls.append(name), real(*a, **k))[1]

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.runs.iterdir()}

    def bus(self):
        return courier.entries((self.ws / "courier.jsonl").read_text())

    def events(self, name):
        return [e for e in self.bus() if e.get("event") == name]

    def untouched(self):
        return self.calls == [] and self.snapshot() == self.before and bool(self.before)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def _send(kind, args):
    return cli._route_chair_converse_send(argparse.Namespace(kind=kind, args=args, sender="session"))


def test_the_lease_spy_sees_a_read_of_the_lease(rig):
    chair.read(rig.runs)
    assert rig.calls == ["read"] and not rig.untouched()


def test_a_sent_directive_applies_on_the_next_tick_and_leaves_the_lease_untouched(rig, capsys):
    assert _send("hold", HOLD) == 0, capsys.readouterr().out
    assert [e["event"] for e in rig.bus()] == ["chat_directive"]
    line = tick(_deps(rig.ws), False, NOW)
    applied = rig.events("directive_applied")
    assert len(applied) == 1 and applied[0]["to"] == "session" and "chat: 1 applied, 0 refused, 0 answered" in line
    assert cli._read_chat_doc(rig.ws / "chair_chat_state.json")[0]["holds"] == ["initiative:i1"]
    assert rig.bus()[0]["acks"] == ["chair"]
    assert rig.untouched()


def test_a_directive_is_not_applied_before_a_tick_runs(rig):
    _send("hold", HOLD)
    assert cli._read_chat_doc(rig.ws / "chair_chat_state.json")[0]["holds"] == [] and rig.events("directive_applied") == []


def test_a_question_gets_an_answer_entry_linked_by_id_and_is_acked(rig):
    asked = chair_chat.build_message("session", "question", {"kind": "left_on_release", "subject": "i1"}, "q1", chair_run.CHAT_REF)
    courier.append_file(rig.ws / "courier.jsonl", asked)
    tick(_deps(rig.ws), False, NOW)
    answers = rig.events("chat_answer")
    assert len(answers) == 1 and answers[0]["fields"]["question"] == "q1" and "i1: 1 left" in answers[0]["note"]
    assert cli._find_chat_answer((rig.ws / "courier.jsonl").read_text(), "q1")["id"] == "q1.answer"
    assert next(e for e in rig.bus() if e["id"] == "q1")["acks"] == ["chair"]
    assert rig.untouched()


def test_an_unknown_entry_is_left_unacked(rig):
    odd = {**chair_chat.build_message("session", "directive", {"kind": "hold"}, "d1", chair_run.CHAT_REF), "event": "needs_attention"}
    torn = {**chair_chat.build_message("session", "directive", {"kind": "hold"}, "d2", chair_run.CHAT_REF), "fields": {"kind": 3}}
    courier.append_file(rig.ws / "courier.jsonl", odd)
    courier.append_file(rig.ws / "courier.jsonl", torn)
    line = tick(_deps(rig.ws), False, NOW)
    assert all(not e.get("acks") for e in rig.bus()) and len(rig.bus()) == 2 and "chat:" not in line


def test_the_step_does_not_run_for_a_dry_run_or_a_standby_tick(rig):
    _send("hold", HOLD)
    tick(_deps(rig.ws), True, NOW)
    tick(_deps(rig.ws, lease={**LEASE, "mine": False, "holder": "other"}), False, NOW)
    assert rig.events("directive_applied") == [] and rig.untouched()


def test_a_failure_mid_step_is_a_note_and_the_next_tick_replays_without_duplicating(rig):
    _send("hold", HOLD)
    deps = _deps(rig.ws)
    failures = []

    def append_once_broken(entry):
        failures.append(entry) if not failures else deps.chat.append(entry)
        if len(failures) == 1 and failures[0] is entry:
            raise OSError("disk full")

    line = tick(replace(deps, chat=replace(deps.chat, append=append_once_broken)), False, NOW)
    assert "chat step failed: disk full" in line and rig.events("directive_applied") == []
    assert "acks" not in rig.bus()[0] and cli._read_chat_doc(rig.ws / "chair_chat_state.json")[0]["applied"] == []
    tick(deps, False, NOW)
    assert len(rig.events("directive_applied")) == 1 and rig.bus()[0]["acks"] == ["chair"]
    assert cli._read_chat_doc(rig.ws / "chair_chat_state.json")[0]["applied"] == [rig.bus()[0]["id"]]


def test_the_command_parses_through_the_real_table_and_the_real_chat_deps_apply_it(rig):
    assert cli.main(["route", "chair", "converse", "send", "hold", *HOLD]) == 0
    assert cli.main(["route", "chair", "converse", "ask", "what_failed", "--wait", "0"]) == 0
    chat = cli._chat_deps(rig.ws)
    assert [e["event"] for e in chair_run.unacked_chat(chat.read())] == ["chat_directive", "chat_question"]
    tick(_deps(rig.ws), False, NOW)
    assert chair_run.unacked_chat(chat.read()) == [] and len(rig.events("chat_answer")) == 1 and rig.untouched()


def test_ask_prints_the_answer_or_says_the_chair_has_not_answered(rig, capsys):
    args = argparse.Namespace(kind="what_failed", subject="", sender="session", wait=0.0)
    assert cli._route_chair_converse_ask(args) == 0
    assert "has not answered" in capsys.readouterr().out
    tick(_deps(rig.ws), False, NOW)
    qid = rig.bus()[0]["id"]
    sleeps = []
    found = cli._await_chat_answer(rig.ws / "courier.jsonl", qid, 5.0, 1.0, sleeps.append, lambda: 0.0)
    assert found is not None and sleeps == [] and found["note"].startswith("Nothing is quarantined")
    assert rig.untouched()


def test_state_prints_the_summary_the_five_latest_events_and_its_age_without_the_lease(rig, capsys):
    for n in range(7):
        _send("hold", ["target_kind=initiative", f"target=i{n}"])
    tick(_deps(rig.ws), False, NOW)
    assert cli._route_chair_converse_state(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "Directives applied: 7" in out and "i1 (1 left)" in out and "Holds: initiative:i0" in out
    assert out.count("directive_applied") == 5 and "last acting tick" in out and rig.untouched()
