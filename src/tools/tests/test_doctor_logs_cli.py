import json

import pytest

from agent_tools import cli

GOOD = '{"id": "a1", "to": "chair"}\n'
NULLS = "\x00" * 24 + "\n"
LOGS = ("courier.jsonl", "land.jsonl", "chair.actions.jsonl")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    profile = tmp_path / ".config" / "agent-tools" / "profile.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    (tmp_path / "ws" / "runs").mkdir(parents=True)
    monkeypatch.setattr(cli, "_parquet_traces_line", lambda profile: None)
    monkeypatch.setattr(cli, "_store_facts", lambda provider, workspace: {
        "kind": "sqlite", "reachable": False, "runs": None, "fresh": True, "error": "no store yet"})
    return tmp_path


def _doctor(home, capsys, *extra):
    args = cli.build_parser().parse_args(
        ["setup", "doctor", "--profile", str(home / ".config" / "agent-tools" / "profile.yaml"), "--json", *extra]
    )
    rc = cli._setup_doctor(args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _log_rows(out: str) -> dict:
    rows = json.loads(out)["rows"]
    return {r["check"]: r for r in rows if r["check"] in LOGS}


def test_a_damaged_courier_line_is_a_warning_row_pointing_at_fix(home, capsys):
    (home / "ws" / "courier.jsonl").write_text(GOOD + NULLS + GOOD, encoding="utf-8")
    _rc, out, _err = _doctor(home, capsys)
    rows = _log_rows(out)
    assert rows["courier.jsonl"]["warn"] is True
    assert "--fix" in rows["courier.jsonl"]["detail"]
    assert (home / "ws" / "courier.jsonl").read_text(encoding="utf-8") == GOOD + NULLS + GOOD
    assert not (home / "ws" / "courier.jsonl.damaged").exists()


def test_fix_moves_the_damaged_line_aside_and_checks_again(home, capsys):
    log = home / "ws" / "courier.jsonl"
    log.write_text(GOOD + NULLS + GOOD, encoding="utf-8")
    _rc, out, err = _doctor(home, capsys, "--fix")
    assert log.read_text(encoding="utf-8") == GOOD + GOOD
    assert (home / "ws" / "courier.jsonl.damaged").read_text(encoding="utf-8") == NULLS
    rows = _log_rows(out)
    assert rows["courier.jsonl"] == {"check": "courier.jsonl", "ok": True, "detail": "ok"}
    assert err == "courier.jsonl: moved 1 damaged line(s) to courier.jsonl.damaged\n"


def test_a_clean_workspace_adds_only_ok_rows(home, capsys):
    (home / "ws" / "courier.jsonl").write_text(GOOD, encoding="utf-8")
    (home / "ws" / "runs" / "land.jsonl").write_text(GOOD, encoding="utf-8")
    _rc, out, err = _doctor(home, capsys, "--fix")
    rows = _log_rows(out)
    assert set(rows) == set(LOGS)
    assert all(r == {"check": r["check"], "ok": True, "detail": "ok"} for r in rows.values())
    assert err == ""
    assert not (home / "ws" / "courier.jsonl.damaged").exists()


def test_the_host_path_ignores_fix(home, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_setup_doctor_host", lambda a, run: seen.append(a.host) or 0)
    monkeypatch.setattr(cli.log_doctor, "fix_logs", lambda *_: pytest.fail("fix ran for --host"))
    args = cli.build_parser().parse_args(["setup", "doctor", "--host", "lane-a", "--fix"])
    assert cli._setup_doctor(args) == 0
    assert seen == ["lane-a"]


def test_without_json_the_moved_line_is_printed_before_the_rows(home, capsys):
    (home / "ws" / "courier.jsonl").write_text(GOOD + NULLS, encoding="utf-8")
    args = cli.build_parser().parse_args(
        ["setup", "doctor", "--profile", str(home / ".config" / "agent-tools" / "profile.yaml"), "--fix"]
    )
    cli._setup_doctor(args)
    out = capsys.readouterr().out
    assert out.startswith("courier.jsonl: moved 1 damaged line(s) to courier.jsonl.damaged\n")
