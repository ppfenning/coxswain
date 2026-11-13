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


def test_lint_reads_only_the_files_it_is_given_and_runs_without_a_store() -> None:
    assert cli.store_refusal(["route", "lint", "work/demo"], None, {}) is None
    assert cli.store_refusal(["route", "lint", "work/demo"], "sqlite:///runs/cox.db", {}) is None
    assert cli.store_refusal(["route", "status"], None, {}) is not None


def test_a_machine_with_no_postgres_store_migrates_unleased_and_says_so(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("COXSWAIN_STORE_DOUBLE")
    monkeypatch.setattr(cli.land_repo_lease, "acquire", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no lease is taken")))
    taken = cli._migrate_acquire(tmp_path / "runs", ["checkout-a", "checkout-b"], "migrate-test")
    assert [(o.path, o.granted, epoch) for o, epoch in taken] == [("checkout-a", True, None), ("checkout-b", True, None)]
    assert "migrating this machine unleased" in capsys.readouterr().err


def test_the_doctor_warns_about_a_local_sqlite_store_on_a_real_machine(monkeypatch) -> None:
    from agent_tools import doctor

    facts = {"store": {"kind": "sqlite", "reachable": True, "runs": 12, "fresh": False}}
    assert doctor._store_row(facts, False) == {"check": "store", "ok": True, "detail": "sqlite, 12 runs"}, "the suites' stand-in"
    monkeypatch.delenv("COXSWAIN_STORE_DOUBLE")
    row = doctor._store_row(facts, False)
    assert row["ok"] is True and row["warn"] is True and "cox setup store --store shared" in row["detail"]
    reachable = doctor._store_row({"store": {"kind": "postgresql", "reachable": True, "runs": 12}}, False)
    assert reachable == {"check": "store", "ok": True, "detail": "postgresql, 12 runs"}
