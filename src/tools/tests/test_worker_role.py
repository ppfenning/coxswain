"""The worker store role's tools side: the doctor row's judgement, the env file text, and `cox setup store worker-role`
refusing what it must before it writes. Against a real Postgres: smoke/test_worker_role.py."""

import argparse
import os

import pytest

from agent_tools import doctor, unit_env
from agent_tools.cli_doctor import _doctor_worker_role_rows
from agent_tools.cli_setup_store import _setup_worker_role

CHECK = doctor.WORKER_ROLE_CHECK


def test_a_machine_with_no_worker_role_gets_no_row():
    assert doctor.worker_role_rows(False, False, True, None, None) == []


def test_half_a_worker_role_in_the_env_file_fails_and_says_to_run_setup_again():
    for role_set, password_set in ((True, False), (False, True)):
        (row,) = doctor.worker_role_rows(role_set, password_set, True, None, None)
        assert row["check"] == CHECK and row["ok"] is False and "worker-role again" in row["detail"]


def test_a_worker_role_without_the_postgres_store_fails():
    (row,) = doctor.worker_role_rows(True, True, False, None, None)
    assert row["ok"] is False and "Postgres" in row["detail"]


def test_the_row_carries_the_probe_verdict_or_why_it_could_not_run():
    assert doctor.worker_role_rows(True, True, True, (True, "connects"), None) == [
        {"check": CHECK, "ok": True, "detail": "connects"}]
    assert doctor.worker_role_rows(True, True, True, (False, "update work_items: allowed"), None)[0]["ok"] is False
    (row,) = doctor.worker_role_rows(True, True, True, None, "the store did not answer (OperationalError)")
    assert row == {"check": CHECK, "ok": False, "detail": "cannot use the role: the store did not answer (OperationalError)"}


def test_the_doctor_edge_adds_no_row_where_the_environment_names_no_worker_role():
    assert unit_env.WORKER_ROLE_ENV not in os.environ and unit_env.WORKER_PASSWORD_ENV not in os.environ
    assert _doctor_worker_role_rows({"provider_profile": "/nonexistent/provider.yaml"}) == []


def test_with_values_sets_names_over_the_lines_a_file_has_and_keeps_the_rest():
    existing = "CLAUDE_CODE_OAUTH_TOKEN=tok\nOTHER=1\n"
    text = unit_env.with_values(existing, {unit_env.WORKER_ROLE_ENV: "cox_w_a", "OTHER": "2"})
    assert unit_env.parse_env(text) == {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "OTHER": "2", unit_env.WORKER_ROLE_ENV: "cox_w_a"}
    assert unit_env.with_values(text, {}) == text
    assert unit_env.file_text(existing, None) == unit_env.with_values(existing, {})


def _profiles(tmp_path, storage_url: str | None):
    provider = tmp_path / "provider.yaml"
    provider.write_text(f"storage_url: {storage_url}\n" if storage_url else "{}\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {tmp_path}\nprovider_profile: {provider}\n", encoding="utf-8")
    return profile


def _args(tmp_path, name: str, profile) -> argparse.Namespace:
    return argparse.Namespace(name=name, environment_file=str(tmp_path / "units.env"), profile=str(profile))


def test_setup_refuses_a_name_that_makes_no_role(tmp_path, capsys):
    assert _setup_worker_role(_args(tmp_path, "---", _profiles(tmp_path, None))) == 2
    assert "worker name is empty" in capsys.readouterr().err
    assert not (tmp_path / "units.env").exists()


def test_setup_refuses_a_profile_with_no_store(tmp_path, capsys):
    assert _setup_worker_role(_args(tmp_path, "w1", _profiles(tmp_path, None))) == 2
    assert "names no storage_url" in capsys.readouterr().err
    assert not (tmp_path / "units.env").exists()


def test_setup_refuses_a_store_with_no_roles_and_writes_no_env_file(tmp_path, capsys):
    store = f"sqlite:///{tmp_path / 'store.db'}"
    assert _setup_worker_role(_args(tmp_path, "w1", _profiles(tmp_path, store))) == 2
    assert "needs the Postgres store" in capsys.readouterr().err
    assert not (tmp_path / "units.env").exists()


def test_setup_refuses_an_env_file_it_would_rewrite_differently_before_touching_the_store(tmp_path, capsys):
    env_file = tmp_path / "units.env"
    env_file.write_text("export A=1\n", encoding="utf-8")
    assert _setup_worker_role(_args(tmp_path, "w1", _profiles(tmp_path, "postgresql://u:p@127.0.0.1:1/none"))) == 2
    assert "export lines" in capsys.readouterr().err
    assert env_file.read_text(encoding="utf-8") == "export A=1\n"


def test_the_env_file_is_replaced_atomically_and_private(tmp_path):
    from agent_tools.cli_setup_store import _write_env_file

    path = tmp_path / "units.env"
    path.write_text("OLD=1\n", encoding="utf-8")
    path.chmod(0o644)
    _write_env_file(path, "NEW=2\n")
    assert path.read_text(encoding="utf-8") == "NEW=2\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["units.env"]
    _write_env_file(tmp_path / "fresh" / "units.env", "A=1\n")
    assert (tmp_path / "fresh" / "units.env").stat().st_mode & 0o777 == 0o600


def test_a_failed_write_leaves_the_old_file_and_no_temporary(tmp_path):
    from agent_tools.cli_setup_store import _write_env_file

    path = tmp_path / "units.env"
    path.write_text("OLD=1\n", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        _write_env_file(path, "BAD=\udcff\n")  # a lone surrogate cannot be encoded, so the write fails midway
    assert path.read_text(encoding="utf-8") == "OLD=1\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["units.env"]
