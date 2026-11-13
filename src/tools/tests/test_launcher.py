import datetime
import socket
from pathlib import Path

import pytest

from agent_tools import chair
from agent_tools.chair_holder import HolderLease
from agent_tools.cli import _bare_launcher_split, _launcher_argv, main


def test_launcher_argv_is_pure_and_takes_the_resolved_plugin_root_as_data():
    argv, warning = _launcher_argv(Path("/plugins/coxswain"), ["/plugins"], False, ["-r", "x"])
    assert argv == ["claude", "--plugin-dir", "/plugins/coxswain", "-r", "x"] and warning is None
    argv, warning = _launcher_argv(None, ["/plugins"], False, [])
    assert argv == ["claude"]
    assert warning == "coxswain plugin not found under ['/plugins']; starting plain claude"


def test_bare_launcher_split_leaves_an_ordinary_subcommands_own_dashdash_untouched():
    assert _bare_launcher_split(["route", "file", "--", "-hi"]) is None


class _FakeBeater:
    pid = 424242


def _fake_spawn(argv):
    return _FakeBeater()


def _profile(tmp_path: Path, skills_root: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    p = tmp_path / "profile.yaml"
    p.write_text(f"workspace_dir: {ws}\nskills_roots: [{skills_root}]\n")
    return p


def _with_plugin(tmp_path: Path) -> Path:
    skills_root = tmp_path / "skills"
    plugin_dir = skills_root / "coxswain" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text("{}")
    return skills_root


def test_print_argv_with_the_plugin_present_names_plugin_dir_and_workspace_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--plugin-dir" in out and str(skills_root / "coxswain") in out
    assert str(tmp_path / "workspace") in out


def test_print_argv_without_the_plugin_prints_the_fallback_line_and_a_plain_argv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "coxswain plugin not found under" in out
    assert "--plugin-dir" not in out


def test_no_plugin_flag_omits_the_flag_even_when_the_plugin_is_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--no-plugin", "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--plugin-dir" not in out
    assert "coxswain plugin not found" not in out


def test_extra_arguments_after_dashdash_pass_through_verbatim_with_no_dashdash_reinserted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv", "--", "-r", "resume-me"])
    out = capsys.readouterr().out
    assert rc == 0
    expected = str(["claude", "--plugin-dir", str(skills_root / "coxswain"), "-r", "resume-me"])
    assert out.splitlines()[0] == expected


def test_a_missing_profile_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    rc = main(["--profile", str(tmp_path / "nope.yaml"), "--print-argv"])
    assert rc == 2


def test_claude_not_on_path_exits_2_with_a_one_line_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None)
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "claude" in out and "PATH" in out


def test_the_profile_is_found_through_the_env_var_when_no_flag_is_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    rc = main(["--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--plugin-dir" in out and str(tmp_path / "workspace") in out


def test_no_arg_off_a_tty_prints_route_status_and_does_not_open_curses(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    profile = _profile(tmp_path, tmp_path / "skills")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    main(["route", "status"])
    expected = capsys.readouterr().out
    main([])
    assert capsys.readouterr().out == expected


def test_bare_cox_on_a_tty_with_no_args_takes_the_chair_and_runs_the_launcher(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    monkeypatch.setattr("agent_tools.cli._spawn", _fake_spawn)
    calls = []
    monkeypatch.setattr("os.execvp", lambda *a: calls.append(a))
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls and calls[0][0] == "claude"
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    assert f"chair taken: chair-{today}" in out
    assert "chair: beating from pid 424242" in out


def test_a_live_foreign_chair_is_printed_not_stolen_and_the_launcher_still_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    calls = []
    monkeypatch.setattr("os.execvp", lambda *a: calls.append(a))
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    now = datetime.datetime.now(datetime.UTC)
    chair.write(
        tmp_path / "workspace" / "runs",
        {
            "session": "chair-someone-else",
            "pid": 1,
            "host": socket.gethostname(),
            "taken_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "runs": [],
        },
    )
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chair: held by chair-someone-else (pid 1) on" in out
    assert "chair taken:" not in out
    assert calls and calls[0][0] == "claude"


def test_a_stale_chair_from_an_earlier_bare_cox_today_is_retaken_not_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    monkeypatch.setattr("agent_tools.cli._spawn", _fake_spawn)
    calls = []
    monkeypatch.setattr("os.execvp", lambda *a: calls.append(a))
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    stale_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20)
    chair.write(
        tmp_path / "workspace" / "runs",
        {
            "session": f"chair-{today}",
            "pid": 999999,
            "host": socket.gethostname(),
            "taken_at": stale_at.isoformat(),
            "heartbeat_at": stale_at.isoformat(),
            "runs": [],
        },
    )
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"chair taken: chair-{today}" in out
    assert "pass --steal" not in out
    assert calls and calls[0][0] == "claude"


def _bare(tmp_path, monkeypatch, takes=None, spawn=_fake_spawn):
    """A terminal session beside a profile that may carry `chair: session_takes:`; returns the execvp calls."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    monkeypatch.setattr("agent_tools.cli._spawn", spawn)
    execs = []
    monkeypatch.setattr("os.execvp", lambda *a: execs.append(a))
    profile = _profile(tmp_path, _with_plugin(tmp_path))
    if takes:
        profile.write_text(profile.read_text() + f"chair:\n  session_takes: {takes}\n")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    return execs


def _hold_record(tmp_path, session, pid):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    record = {"session": session, "pid": pid, "host": socket.gethostname(), "taken_at": now, "heartbeat_at": now, "runs": []}
    chair.write(tmp_path / "workspace" / "runs", record)
    return record


def _service_beside_the_session(monkeypatch, tmp_path):
    """A tickless lease row and a record naming a service: its pid is alive, runs `chair run`, and sits in the service cgroup."""
    expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    lease = HolderLease("chair-svc", 4242, socket.gethostname(), expires, False)
    monkeypatch.setattr("agent_tools.cli._read_holder_inputs", lambda runs_dir: (lease, None, True))
    monkeypatch.setattr("agent_tools.cli._pid_command", lambda pid: "python -m agent_tools.cli chair run --label chair-svc")
    monkeypatch.setattr("agent_tools.cli._pid_cgroup", lambda pid: "0::/user.slice/app.slice/coxswain-chair.service\n")
    monkeypatch.setattr("agent_tools.chair.pid_alive", lambda pid: True)
    return _hold_record(tmp_path, "chair-svc", 4242)


def _watch_the_lease(monkeypatch):
    calls = []
    for name in ("acquire_lease", "release_lease", "renew_lease"):
        monkeypatch.setattr(f"agent_tools.chair.{name}", lambda *a, _n=name, **k: calls.append(_n) or "")
    return calls


def test_a_session_beside_a_live_service_leaves_the_lease_and_the_record_and_prints_the_held_line(tmp_path, monkeypatch, capsys):
    execs = _bare(tmp_path, monkeypatch)
    record = _service_beside_the_session(monkeypatch, tmp_path)
    lease_calls = _watch_the_lease(monkeypatch)
    assert main([]) == 0
    assert capsys.readouterr().out.splitlines() == ["chair held by chair-svc (service)"]
    assert lease_calls == []
    assert chair.read(tmp_path / "workspace" / "runs") == record
    assert execs and execs[0][0] == "claude"


def test_a_record_only_loop_with_no_lease_row_is_still_live_and_reads_as_a_chair_loop_off_the_service(tmp_path, monkeypatch, capsys):
    execs = _bare(tmp_path, monkeypatch, takes="always")
    record = _service_beside_the_session(monkeypatch, tmp_path)
    monkeypatch.setattr("agent_tools.cli._read_holder_inputs", lambda runs_dir: None)
    monkeypatch.setattr("agent_tools.cli._pid_cgroup", lambda pid: "0::/user.slice/session-3.scope\n")
    assert main([]) == 0
    assert capsys.readouterr().out.splitlines() == ["chair held by chair-svc (chair loop)"]
    assert chair.read(tmp_path / "workspace" / "runs") == record
    assert execs and execs[0][0] == "claude"


@pytest.mark.parametrize(("takes", "taken"), [(None, True), ("when_free", True), ("always", True), ("never", False)])
def test_a_free_lease_is_taken_unless_the_policy_is_never(tmp_path, monkeypatch, capsys, takes, taken):
    execs = _bare(tmp_path, monkeypatch, takes=takes)
    assert main([]) == 0
    out = capsys.readouterr().out
    assert ("chair taken:" in out) is taken
    assert ("chair: beating from pid 424242" in out) is taken
    assert (chair.read(tmp_path / "workspace" / "runs") is not None) is taken
    assert execs and execs[0][0] == "claude"


def test_a_live_non_loop_holder_is_taken_over_without_the_offer_prompt_under_always(tmp_path, monkeypatch, capsys):
    execs = _bare(tmp_path, monkeypatch, takes="always")
    _hold_record(tmp_path, "chair-other", 1)
    asked = []
    monkeypatch.setattr("agent_tools.cli._read_key", lambda: asked.append(1) or "c")
    monkeypatch.setattr("agent_tools.chair_resolve.takeover", lambda *a, **k: (True, "chair taken over"))
    assert main([]) == 0
    assert asked == []
    assert "chair taken over" in capsys.readouterr().out
    assert execs and execs[0][0] == "claude"


def test_a_live_non_loop_holder_is_left_alone_with_no_prompt_under_never(tmp_path, monkeypatch, capsys):
    execs = _bare(tmp_path, monkeypatch, takes="never")
    _hold_record(tmp_path, "chair-other", 1)
    monkeypatch.setattr("agent_tools.cli._read_key", lambda: pytest.fail("no offer under never"))
    assert main([]) == 0
    assert "chair: held by chair-other" in capsys.readouterr().out
    assert chair.read(tmp_path / "workspace" / "runs")["session"] == "chair-other"
    assert execs and execs[0][0] == "claude"


def test_a_failed_beater_start_releases_the_lease_it_just_took(tmp_path, monkeypatch, capsys):
    def _no_beater(argv):
        raise OSError("no fork")

    execs = _bare(tmp_path, monkeypatch, spawn=_no_beater)
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "chair taken:" in out
    assert "chair: beater failed to start: no fork" in out
    assert "chair: released" in out
    assert chair.read(tmp_path / "workspace" / "runs") is None
    assert execs and execs[0][0] == "claude"
