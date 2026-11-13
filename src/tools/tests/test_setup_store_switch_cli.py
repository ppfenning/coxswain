import json

import pytest

from agent_tools import cli, store_switch
from agent_tools.store_probe import ProbeResult

PASSWORD = "pw-test-SECRET-9f3"
ACCESS = "GKaccess123"
SECRET = "sekrit-secret-key"
ENV = {"PG_PW": PASSWORD, "S3_ID": ACCESS, "S3_SEC": SECRET}
FLAGS = [
    "--host", "db.example", "--port", "5432", "--database", "graphs", "--user", "cox", "--bucket", "traces",
    "--endpoint", "http://garage.example:3900", "--region", "garage",
    "--password-env", "PG_PW", "--access-key-env", "S3_ID", "--secret-key-env", "S3_SEC",
]
GOOD = [ProbeResult("postgres", True, "accepted"), ProbeResult("object_store", True, "reachable")]
BAD = [ProbeResult("postgres", False, "refused"), ProbeResult("object_store", True, "reachable")]
OK_COPY = lambda s, t: {"runs": 1}  # noqa: E731
OK_TRACES = lambda s, t: {"copied": 1}  # noqa: E731
SECRETS = (PASSWORD, ACCESS, SECRET, f"cox:{PASSWORD}@")


def boom(source, target):
    raise RuntimeError(f"cannot reach {target}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    settings = tmp_path / ".config" / "agent-tools" / "profile.yaml"
    settings.parent.mkdir(parents=True)
    settings.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    runs = tmp_path / "ws" / "runs"
    (runs / "traces").mkdir(parents=True)
    (runs / "cox.db").write_bytes(b"SQLite format 3\x00 source rows")
    (runs / "traces" / "2026-10-16.parquet").write_bytes(b"PAR1 source traces")
    return tmp_path


def settings(home) -> str:
    return (home / ".config" / "agent-tools" / "profile.yaml").read_text(encoding="utf-8")


def profile_file(home):
    return home / ".config" / "agent-tools" / "shared-store.yaml"


def runs_snapshot(home) -> dict:
    root = home / "ws" / "runs"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def switch(argv, **hooks):
    args = cli.build_parser().parse_args(["setup", "store", *argv])
    hooks.setdefault("env", ENV)
    hooks.setdefault("copy_store", OK_COPY)
    hooks.setdefault("copy_traces", OK_TRACES)
    hooks.setdefault("probes", lambda profile, overlay: GOOD)
    return args.fn(args, **hooks)


def assert_no_secret(captured) -> None:
    text = captured.out + captured.err
    assert not [s for s in SECRETS if s in text]


def test_to_shared_records_shared_and_hides_the_password(home, capsys):
    before = runs_snapshot(home)
    assert switch(["switch", "shared", *FLAGS]) == 0
    assert "mode: shared" in settings(home)
    assert profile_file(home).exists()
    assert runs_snapshot(home) == before
    assert_no_secret(capsys.readouterr())


def test_failing_copy_leaves_the_mode_the_source_and_the_output_clean(home, capsys):
    before = runs_snapshot(home)
    assert switch(["switch", "shared", *FLAGS], copy_store=boom) == 1
    assert "mode: shared" not in settings(home)
    assert runs_snapshot(home) == before
    out = capsys.readouterr()
    assert "copy_store" in out.err
    assert_no_secret(out)


def test_failing_traces_copy_leaves_the_mode_and_the_source(home, capsys):
    before = runs_snapshot(home)
    assert switch(["switch", "shared", *FLAGS], copy_traces=boom) == 1
    assert "mode: shared" not in settings(home)
    assert runs_snapshot(home) == before
    out = capsys.readouterr()
    assert "copy_traces" in out.err
    assert_no_secret(out)


@pytest.mark.parametrize("argv", [["--dry-run", "switch", "shared", *FLAGS], ["switch", "shared", *FLAGS, "--dry-run"]])
def test_dry_run_changes_nothing_in_both_positions(home, argv, capsys):
    before = runs_snapshot(home)
    assert switch(argv, copy_store=boom, copy_traces=boom) == 0
    assert "mode:" not in settings(home)
    assert not profile_file(home).exists()
    assert runs_snapshot(home) == before
    out = capsys.readouterr()
    assert "dry run" in out.out
    assert_no_secret(out)


def test_a_flag_before_the_verb_is_kept(home):
    args = cli.build_parser().parse_args(["setup", "store", "--host", "early", "--dry-run", "switch", "shared"])
    assert args.host == "early"
    assert args.dry_run is True


def test_current_mode_is_a_no_op_and_a_repeat_is_safe(home, capsys):
    assert switch(["switch", "shared", *FLAGS]) == 0
    capsys.readouterr()
    assert switch(["switch", "shared", *FLAGS], copy_store=boom, copy_traces=boom) == 0
    out = capsys.readouterr()
    assert "already shared" in out.out
    assert_no_secret(out)
    assert "mode: shared" in settings(home)


def test_a_second_switch_after_a_failure_completes(home):
    assert switch(["switch", "shared", *FLAGS], copy_store=boom) == 1
    assert switch(["switch", "shared", *FLAGS]) == 0
    assert "mode: shared" in settings(home)


def test_default_copiers_resolve_on_store_switch_public_names(home, monkeypatch):
    calls = []

    def fake_store(source, target):
        calls.append(("store", source, target))
        return {"runs": 1}

    def fake_traces_factory(object_store=None, env=None):
        calls.append(("factory", object_store, env))
        return lambda source, target: calls.append(("traces", source, target)) or {"copied": 1}

    monkeypatch.setattr(store_switch, "default_copy_store", fake_store)
    monkeypatch.setattr(store_switch, "traces_copier", fake_traces_factory)
    assert switch(["switch", "shared", *FLAGS], copy_store=None, copy_traces=None) == 0
    kinds = [c[0] for c in calls]
    assert kinds == ["factory", "store", "traces"]
    assert calls[1][1].startswith("sqlite:///") and calls[1][2].startswith("postgresql://")
    assert calls[0][1]["endpoint"] == "http://garage.example:3900"
    assert calls[0][2][cli.setup_store.ACCESS_KEY_ENV] == ACCESS


def _doctor(home, capsys, *extra):
    args = cli.build_parser().parse_args(["setup", "doctor", "--profile", str(home / ".config" / "agent-tools" / "profile.yaml"), *extra])
    rc = cli._setup_doctor(args)
    return rc, capsys.readouterr()


@pytest.fixture
def quiet_doctor(monkeypatch):
    monkeypatch.setattr(cli, "_parquet_traces_line", lambda profile: None)
    monkeypatch.setattr(cli, "_store_facts", lambda provider, workspace: {
        "kind": "sqlite", "reachable": False, "runs": None, "fresh": True, "error": "no store yet"})


def test_doctor_prints_a_warning_row_for_the_retired_files_setting(home, capsys, quiet_doctor):
    _rc, out = _doctor(home, capsys, "--json")
    rows = [r for r in json.loads(out.out)["rows"] if r["check"] == "shared store"]
    assert [r["ok"] for r in rows] == [True]
    assert "files, which is retired" in rows[0]["detail"]
    _rc, out = _doctor(home, capsys)
    line = next(row for row in out.out.splitlines() if row.startswith("shared store"))
    assert " warn " in line and "files, which is retired" in line


def test_doctor_prints_a_fail_row_naming_postgres_for_an_unreachable_shared_store(home, capsys, monkeypatch, quiet_doctor):
    assert switch(["switch", "shared", *FLAGS]) == 0
    capsys.readouterr()
    path = home / ".config" / "agent-tools" / "profile.yaml"
    path.write_text(path.read_text(encoding="utf-8") + f"provider_profile: {profile_file(home)}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_probe_store", lambda profile, overlay: BAD)
    rc, out = _doctor(home, capsys)
    line = next(row for row in out.out.splitlines() if row.startswith("shared store postgres"))
    assert "FAIL" in line and "refused" in line
    assert rc == 1
    assert_no_secret(out)
    rc, out = _doctor(home, capsys, "--json")
    rows = json.loads(out.out)["rows"]
    assert any(r["check"] == "shared store postgres" and not r["ok"] for r in rows)
    assert_no_secret(out)


def test_switching_to_files_is_refused_and_changes_nothing(home, capsys):
    before = settings(home)
    assert switch(["switch", "files"]) == 2
    assert "the files store is retired" in capsys.readouterr().err
    assert settings(home) == before
