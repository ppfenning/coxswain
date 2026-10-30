import datetime
import socket

from agent_tools import chair, chair_land, chair_resolve
from agent_tools.cli import main

HOST = socket.gethostname()
NOW = datetime.datetime.now(datetime.UTC)


def _record(**extra):
    now = datetime.datetime.now(datetime.UTC).isoformat()  # a fresh heartbeat on every call, not at import
    return {"session": "chair-2026-10-29", "pid": 4321, "host": HOST, "taken_at": now, "heartbeat_at": now, "runs": [], **extra}


def test_holder_is_recognised_by_claude_pid_and_by_session_id_and_a_foreign_pid_is_not():
    record = _record(session_id="s-1")
    assert chair.resolve_holder_label(record, "live", {"CLAUDE_PID": "4321"}, HOST, None) == "chair-2026-10-29"
    assert chair.resolve_holder_label(record, "live", {"CLAUDE_CODE_SESSION_ID": "s-1"}, HOST, None) == "chair-2026-10-29"
    assert chair.resolve_holder_label(record, "live", {"CLAUDE_PID": "99"}, HOST, None) is None
    assert chair.resolve_holder_label(record, "crashed", {"CLAUDE_PID": "4321"}, HOST, None) is None


def test_a_mismatched_explicit_label_is_kept_so_guard_refuses_it():
    record = _record()
    label = chair.resolve_holder_label(record, "live", {"CLAUDE_PID": "4321"}, HOST, "chair-other")
    assert label == "chair-other"
    assert chair.guard(record, label, "live").startswith("refusing: the landing loop is held by chair-2026-10-29")


def test_take_records_session_id_and_cwd_and_an_old_lease_has_neither():
    taken, _ = chair.take(None, "c", 1, HOST, NOW, 10, False, claude_session="s-1", cwd="/ws")
    assert (taken["session_id"], taken["cwd"]) == ("s-1", "/ws")
    bare, _ = chair.take(None, "c", 1, HOST, NOW, 10, False)
    assert "session_id" not in bare and "cwd" not in bare


def test_resolve_bare_branches():
    assert chair_resolve.resolve_bare(None, "none", HOST, NOW).kind == "take"
    assert chair_resolve.resolve_bare(_record(), "stale", HOST, NOW).kind == "take"
    assert chair_resolve.resolve_bare(_record(), "crashed", HOST, NOW).kind == "take"
    resumed = chair_resolve.resolve_bare(_record(session_id="s-1", cwd="/ws"), "crashed", HOST, NOW)
    assert (resumed.kind, resumed.session_id, resumed.cwd) == ("resume", "s-1", "/ws")
    assert chair_resolve.resolve_bare(_record(session_id="s-1"), "live", HOST, NOW).default == "attach"
    assert chair_resolve.resolve_bare(_record(), "live", HOST, NOW).default == "observer"
    assert chair_resolve.resolve_bare(_record(session_id="s-1", host="elsewhere"), "live", HOST, NOW).default == "observer"


def test_choose_maps_one_keypress():
    assert [chair_resolve.choose(k, "attach") for k in ("a", "b", "c", "\n", "x")] == ["takeover", "observer", "cancel", "attach", "cancel"]


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _seed(tmp_path, **extra):
    chair.write(tmp_path, _record(pid=4321, **extra))


def test_takeover_ends_the_lease_after_the_grace_with_a_fake_clock_and_stamps_taken_over_from(tmp_path):
    _seed(tmp_path)
    clock = _Clock()
    ok, line = chair_resolve.takeover(tmp_path, "chair-new", 777, HOST, "s-2", "/ws", heartbeat_minutes=10, clock=clock.time, sleep=clock.sleep, grace=5.0, poll=1.0)
    assert ok and line == "chair taken over from chair-2026-10-29"
    assert clock.now == 5.0
    record = chair.read(tmp_path)
    assert (record["session"], record["taken_over_from"], record["session_id"]) == ("chair-new", "chair-2026-10-29", "s-2")
    assert chair.session_line(record, "live", {"CLAUDE_PID": "777"}, HOST) == "chair: taken over from chair-2026-10-29"
    chat = (tmp_path / "leader.chat.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(chat) == 1 and "chair-new asks chair-2026-10-29 to release" in chat[0]


def test_a_holder_that_honours_the_request_during_the_grace_cuts_it_short_and_the_takeover_still_names_it(tmp_path):
    _seed(tmp_path)
    clock = _Clock()

    def sleep(seconds):  # the old holder's beat loop wakes during the grace and sees the request
        clock.sleep(seconds)
        assert chair.read(tmp_path)["release_requested_by"] == "chair-new"
        assert chair.honour_release_request(tmp_path, "chair-2026-10-29", 4321, HOST)

    ok, line = chair_resolve.takeover(tmp_path, "chair-new", 777, HOST, None, None, heartbeat_minutes=10, clock=clock.time, sleep=sleep, grace=20.0, poll=1.0)
    assert ok and clock.now == 1.0 and line == "chair taken over from chair-2026-10-29"
    record = chair.read(tmp_path)
    assert record["taken_over_from"] == "chair-2026-10-29" and "release_requested_by" not in record
    assert chair.session_line(record, "live", {"CLAUDE_PID": "777"}, HOST) == "chair: taken over from chair-2026-10-29"


def test_beat_loop_honours_a_takeover_request_and_the_takeover_does_not_wait_the_grace(tmp_path):
    _seed(tmp_path)
    chair.write(tmp_path, chair.request_release(chair.read(tmp_path), "chair-new", NOW))
    clock = _Clock()
    rc = chair.beat_loop("chair-2026-10-29", 4321, runs_dir=tmp_path, interval=0, clock=clock, alive=lambda pid, sig: None, environ={})
    assert rc == 0 and chair.read(tmp_path) is None
    ok, line = chair_resolve.takeover(tmp_path, "chair-new", 777, HOST, None, None, heartbeat_minutes=10, clock=clock.time, sleep=clock.sleep, grace=5.0)
    assert ok and clock.now == 0.0 and "taken over" not in line


def test_land_worker_stops_beating_once_the_release_is_honoured():
    beats = []
    worker = chair_land.LandWorker(lambda: beats.append(1), 30.0, lambda: 0.0, lambda s: None, released=lambda: True)
    worker._running.add("/repo")
    worker._beat_loop()
    assert worker.released and beats == []


def test_adopt_session_fills_a_missing_session_id_for_the_pid_holder_only():
    record = _record()
    adopted = chair.adopt_session(record, "live", {"CLAUDE_PID": "4321", "CLAUDE_CODE_SESSION_ID": "s-7"}, HOST)
    assert adopted["session_id"] == "s-7"
    assert chair.adopt_session(record, "live", {"CLAUDE_PID": "99", "CLAUDE_CODE_SESSION_ID": "s-7"}, HOST) is record
    assert chair.adopt_session(_record(session_id="s-1"), "live", {"CLAUDE_PID": "4321", "CLAUDE_CODE_SESSION_ID": "s-7"}, HOST)["session_id"] == "s-1"


def test_the_land_guard_needs_no_label_for_the_holder_and_still_refuses_others(tmp_path, monkeypatch, capsys):
    from agent_tools import cli

    chair.write(tmp_path, _record(pid=4321))
    monkeypatch.setattr(cli, "_leader_pid_alive", lambda record: True)
    monkeypatch.setenv("CLAUDE_PID", "4321")
    assert cli._leader_guard_or_refuse(tmp_path, cli._UNLABELED, False) is None
    assert cli._leader_guard_or_refuse(tmp_path, "chair-other", False, explicit_label="chair-other") == 2
    monkeypatch.setenv("CLAUDE_PID", "99")
    assert cli._leader_guard_or_refuse(tmp_path, cli._UNLABELED, False) == 2
    assert "refusing: the landing loop is held by chair-2026-10-29 (live)" in capsys.readouterr().out


def test_status_adoption_reads_under_the_lock_so_a_release_request_written_meanwhile_survives(tmp_path, monkeypatch, capsys):
    import argparse

    from agent_tools import cli

    chair.write(tmp_path, chair.request_release(_record(), "chair-new", NOW))
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda a: (None, tmp_path, None))
    monkeypatch.setattr(cli, "_leader_read_or_refuse", lambda runs_dir: (_record(), None))  # the stale copy read before the request
    monkeypatch.setattr(cli, "_leader_pid_alive", lambda record: True)
    monkeypatch.setenv("CLAUDE_PID", "4321")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-7")
    assert cli._route_leader_status(argparse.Namespace()) == 0
    written = chair.read(tmp_path)
    assert written["session_id"] == "s-7" and written["release_requested_by"] == "chair-new"


def test_the_holder_beats_and_releases_with_no_label_and_a_hand_typed_one_is_not_overridden(tmp_path, monkeypatch, capsys):
    import argparse

    from agent_tools import cli

    chair.write(tmp_path, _record(pid=4321))
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda a: (None, tmp_path, None))
    monkeypatch.setattr(cli, "_leader_pid_alive", lambda record: True)
    monkeypatch.setenv("CLAUDE_PID", "4321")
    monkeypatch.setenv("COX_SESSION_LABEL", "chair-other")
    assert cli._leader_guard_or_refuse(tmp_path, cli._holder_label(argparse.Namespace(label=None)), False, explicit_label=None) is None  # the env label is not a typed one
    assert cli._leader_guard_or_refuse(tmp_path, "chair-other", False, explicit_label="chair-other") == 2  # even when it equals the env label
    assert cli._route_chair_beat(argparse.Namespace(label=None, pid=None, run=None)) == 0
    assert cli._route_chair_beat(argparse.Namespace(label="chair-other", pid=None, run=None)) == 2
    assert cli._route_chair_release(argparse.Namespace(label=None, pid=None)) == 0
    assert chair.read(tmp_path) is None


def _bare(tmp_path, monkeypatch, key):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    monkeypatch.setattr("agent_tools.cli._spawn", lambda argv: type("B", (), {"pid": 1})())
    monkeypatch.setattr("agent_tools.cli._read_key", lambda: key)
    calls = []
    monkeypatch.setattr("os.execvp", lambda *a: calls.append(a))
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\nskills_roots: [{tmp_path / 'skills'}]\n")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    chair.write(ws / "runs", _record(pid=1, session_id="s-8"))
    return calls


def test_bare_cox_cancel_execs_nothing_and_takeover_runs_the_takeover_then_claude(tmp_path, monkeypatch, capsys):
    calls = _bare(tmp_path, monkeypatch, "c")
    assert main([]) == 0
    assert "chair: cancelled" in capsys.readouterr().out and calls == []
    taken = []
    monkeypatch.setattr("agent_tools.cli._read_key", lambda: "a")
    monkeypatch.setattr("agent_tools.chair_resolve.takeover", lambda *a, **k: taken.append(a[1]) or (True, "chair taken over from chair-2026-10-29"))
    assert main([]) == 0
    assert len(taken) == 1 and calls and calls[0][0] == "claude" and "--resume" not in calls[0][1]
    assert "chair taken over from chair-2026-10-29" in capsys.readouterr().out


def test_a_crashed_lease_without_a_session_takes_instead_of_resuming(tmp_path, monkeypatch, capsys):
    calls = _bare(tmp_path, monkeypatch, "")
    chair.write(tmp_path / "workspace" / "runs", _record(pid=999999))
    assert main([]) == 0
    assert "chair taken:" in capsys.readouterr().out and "--resume" not in calls[0][1]


def test_bare_cox_resumes_a_crashed_lease_in_its_cwd_and_the_session_line_says_resumed(tmp_path, monkeypatch, capsys):
    import os

    calls = _bare(tmp_path, monkeypatch, "")
    ws = tmp_path / "workspace"
    chair.write(ws / "runs", _record(pid=999999, session_id="s-9", cwd=str(ws)))
    assert main([]) == 0
    assert calls[0][1][:3] == ["claude", "--resume", "s-9"]
    record = chair.read(ws / "runs")
    assert (record["session"], record["session_id"], record["resumed"]) == ("chair-2026-10-29", "s-9", True)
    assert chair.session_line(record, "live", {"CLAUDE_PID": str(os.getpid())}, HOST) == "chair: resumed chair-2026-10-29"
    import argparse

    from agent_tools import cli

    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda a: (None, ws / "runs", None))
    monkeypatch.setattr(cli, "_leader_pid_alive", lambda record: True)
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    capsys.readouterr()
    assert cli._route_leader_status(argparse.Namespace()) == 0
    assert capsys.readouterr().out == "chair: resumed chair-2026-10-29\n"
    assert cli._route_leader_status(argparse.Namespace()) == 0  # the stamp announced the resume once and is gone
    assert capsys.readouterr().out == "chair: held by this session\n"
    assert "resumed" not in chair.read(ws / "runs")


def test_the_holder_retakes_with_no_label(tmp_path, monkeypatch, capsys):
    import argparse

    from agent_tools import cli

    chair.write(tmp_path, _record(pid=4321))
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda a: (None, tmp_path, None))
    monkeypatch.setattr(cli, "_leader_pid_alive", lambda record: True)
    monkeypatch.setenv("CLAUDE_PID", "4321")
    assert cli._route_chair_take(argparse.Namespace(label=None, pid=None, steal=True)) == 0
    assert "chair lease retaken: chair-2026-10-29 (pid 4321)" in capsys.readouterr().out
    assert cli._route_chair_take(argparse.Namespace(label="chair-other", pid=None, steal=False)) == 2


def test_print_argv_names_each_bare_cox_branch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\nskills_roots: [{tmp_path / 'skills'}]\n")
    runs = ws / "runs"
    take = "chair: would take the chair as the session starts"
    offer = "(a) take over  (b) open a non-chair session  (c) cancel"
    cases = [
        (None, [take], "--resume"),
        (_record(pid=999999, session_id="s-9", cwd=str(ws)), ["chair: would resume chair-2026-10-29 with claude --resume s-9", "and take the lease"], "--resume"),
        (_record(pid=999999), [take], "--resume"),
        (_record(pid=1, session_id="s-8"), ["chair: would attach to chair-2026-10-29's conversation with claude --resume s-8", offer, "[Enter: attach]"], "--resume"),
        (_record(pid=1), ["chair: would open a non-chair session", offer, "[Enter: open a non-chair session]"], "--resume"),
        (_record(pid=1, host="elsewhere", session_id="s-8"), ["chair: would open a non-chair session", offer, "elsewhere"], "--resume"),
    ]
    for record, expected, flag in cases:
        chair.write(runs, record)
        assert main(["--profile", str(profile), "--print-argv"]) == 0
        out, err = capsys.readouterr()
        assert all(text in err for text in expected), err
        assert "chair:" not in out and out.splitlines()[-2].startswith("['claude'")  # stdout keeps the argv and cwd; the branch is named on stderr
        assert (flag in out) == any("claude --resume" in text for text in expected[:1])
