"""The one store is Postgres: a board command refuses any other configured store before it runs."""

from __future__ import annotations

from agent_tools import cli

PG = "postgresql://db.example/cox"


def test_a_postgres_store_runs_every_command() -> None:
    assert cli.store_refusal(["route", "status"], PG, {}) is None


def test_no_store_and_a_sqlite_store_are_refused_for_board_commands() -> None:
    assert "no store is configured" in cli.store_refusal(["route", "status"], None, {})
    assert "no store is configured" in cli.store_refusal(["lander", "run"], "", {})
    refused = cli.store_refusal(["chair", "run"], "sqlite:///runs/cox.db", {})
    assert "not Postgres" in refused and "cox setup store switch shared" in refused
    assert "not Postgres" in cli.store_refusal(["runs", "list"], "not a url", {})


def test_the_commands_that_configure_a_machine_run_without_a_store() -> None:
    for group in ("setup", "install", "upgrade", "versions", "settings"):
        assert cli.store_refusal([group, "anything"], None, {}) is None
    assert cli.store_refusal([], None, {}) is None


def test_the_suites_stand_in_is_allowed_only_by_its_exact_value() -> None:
    assert cli.store_refusal(["route", "status"], None, {"COXSWAIN_STORE_DOUBLE": "sqlite"}) is None
    assert cli.store_refusal(["route", "status"], None, {"COXSWAIN_STORE_DOUBLE": "1"}) is not None


def test_a_machine_with_no_profile_yet_is_not_refused_and_one_with_a_sqlite_store_is(tmp_path, monkeypatch, capsys) -> None:
    import argparse

    monkeypatch.delenv("COXSWAIN_STORE_DOUBLE")
    assert cli._store_refusal(argparse.Namespace(profile=str(tmp_path / "none.yaml")), ["route", "status"]) is None
    provider = tmp_path / "provider.yaml"
    provider.write_text("storage_url: sqlite:///runs/cox.db\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\nprovider_profile: {provider}\n")
    assert cli.main(["route", "status", "--profile", str(profile)]) == 2
    assert "not Postgres" in capsys.readouterr().err
