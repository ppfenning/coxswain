"""`cox upgrade` on the edge channel: resolve the newest green commit on main, move to it by the `--to` path, restart
the units the machine has, run the doctor, roll back on a failure, record the outcome; and the timer that runs it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_tools import cli, doctor, forge_github, upgrade_edge

A, B, C = ("a" * 40, "b" * 40, "c" * 40)  # newest first on main: A is the tip, B the newest green, C installed
SLUG = "example-org/example-repo"
MANIFEST = f'[coxswain]\nversion = "2026.11.8"\nrepo = "{SLUG}"\n\n[providers.claude-code]\nstatus = "supported"\n'
BASE_LISTING = "src/graphs/harness/store_ddl_0001.py\nsrc/graphs/harness/store_ddl_0002.py\nsrc/graphs/harness/cli.py\n"
COX = "/opt/example/bin/cox"
DOCTOR = ["cox", "setup", "doctor", "--json"]
CURRENT_UNIT = "[Service]\nExecStart=/c chair run\nKillMode=process\n"
LEGACY_UNIT = "[Service]\nExecStart=/c chair run\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n"
RELOAD = ["systemctl", "--user", "daemon-reload"]


class World:
    """The checkout, the forge and the service manager as the edge flow's commands see them."""

    def __init__(self, root: Path) -> None:
        self.checkout = str(root / "coxswain")
        self.head = C
        self.history = [A, B, C]
        self.green = {B, C}
        self.listings = {A: BASE_LISTING, B: BASE_LISTING, C: BASE_LISTING}
        self.files: dict[tuple[str, str], str] = {}  # (sha, path) -> text, for `git show <sha>:<path>`
        self.doctor_fails_at: set[str] = set()  # a required row (the store) fails at these commits
        self.advisory_fails_at: set[str] = set()  # only an advisory row (the token file) fails at these commits
        self.down_after_restart: set[str] = set()  # units that are not active once restarted
        self.restarted = False
        self.calls: list[list[str]] = []
        self.moves: list[tuple[str | None, bool]] = []
        self.move_exits: dict[str, int] = {}  # a failing move still moved the checkout unless `stuck`
        self.stuck = False
        self.reload_exit = 0
        self.stale = False  # systemd's loaded units are older than their files (a failed reload)

    def run(self, argv: list, cwd: str | None = None) -> tuple[int, str]:
        self.calls.append(list(argv))
        git = ["git", "-C", self.checkout]
        if argv[:3] == git and argv[3:5] == ["fetch", "origin"]:
            return 0, ""
        if argv[:3] == git and argv[3] == "rev-list":
            return 0, "".join(f"{sha}\n" for sha in self.history)
        if argv[:3] == git and argv[3:] == ["rev-parse", "HEAD"]:
            return 0, f"{self.head}\n"
        if argv[:3] == git and argv[3] == "ls-tree":
            return 0, self.listings[argv[5]]
        if argv[:3] == git and argv[3] == "show":
            sha, _, path = argv[4].partition(":")
            text = self.files.get((sha, path))
            return (0, text) if text is not None else (128, f"fatal: path '{path}' does not exist in '{sha}'")
        if argv[:3] == ["gh", "run", "list"]:
            runs = [{"headSha": sha, "status": "completed", "conclusion": "success" if sha in self.green else "failure"}
                    for sha in self.history]
            return 0, json.dumps(runs)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            states = ["inactive" if self.restarted and unit in self.down_after_restart else "active" for unit in argv[3:]]
            return (0 if set(states) == {"active"} else 3), "".join(f"{state}\n" for state in states)
        if argv[:3] == ["uv", "sync", "--check"]:
            return 0, ""
        if argv[:3] == ["systemctl", "--user", "try-restart"]:
            self.restarted = True
            return 0, ""
        if argv == ["systemctl", "--user", "daemon-reload"]:
            self.stale = self.reload_exit != 0
            return self.reload_exit, ""
        if argv[:5] == ["systemctl", "--user", "show", "-p", "NeedDaemonReload"]:
            return 0, "".join(f"NeedDaemonReload={'yes' if self.stale else 'no'}\n" for _ in argv[5:])
        if argv == DOCTOR:
            rows = [{"check": "store", "ok": self.head not in self.doctor_fails_at, "detail": "store FAIL"},
                    {"check": "claude token", "ok": self.head not in self.advisory_fails_at, "detail": "no token file"},
                    *({"check": c, "ok": True} for c in doctor.UPGRADE_EXPECTED_CHECKS if c != "store")]
            return int(not all(r["ok"] for r in rows)), json.dumps({"rows": rows, "ok": all(r["ok"] for r in rows)})
        raise AssertionError(f"unexpected command: {argv}")

    def move(self, a) -> int:
        self.moves.append((a.to, a.dry_run))
        if not a.dry_run and not self.stuck:
            self.head = a.to
        return self.move_exits.get(a.to, 0)

    def restarts(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[:3] == ["systemctl", "--user", "try-restart"]]

    def doctors(self) -> int:
        return sum(argv == DOCTOR for argv in self.calls)


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> World:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("coxswain-chair.service", "coxswain-lander.service"):
        (unit_dir / name).write_text(CURRENT_UNIT)  # a unit written by this version: nothing to refresh
    root = tmp_path / "installs"
    (root / "coxswain" / ".git").mkdir(parents=True)
    (root / "coxswain" / "manifest.toml").write_text(MANIFEST)
    (tmp_path / "profile.yaml").write_text("upgrade_channel: edge\n")
    w = World(root)
    monkeypatch.setattr(cli, "_edge_run", w.run)
    monkeypatch.setattr(cli, "_upgrade_move", w.move)
    return w


def _upgrade(tmp_path: Path, *flags: str) -> int:
    return cli.main(["upgrade", "--root", str(tmp_path / "installs"), "--profile", str(tmp_path / "profile.yaml"),
                     "--provider", "claude-code", *flags])


def _record(tmp_path: Path) -> dict | None:
    path = tmp_path / "installs" / upgrade_edge.RECORD_FILE
    return json.loads(path.read_text()) if path.exists() else None


def test_edge_moves_to_the_newest_green_commit_not_the_tip_then_restarts_both_units_and_runs_the_doctor(
    world: World, tmp_path: Path, capsys
):
    assert _upgrade(tmp_path) == 0
    out = capsys.readouterr().out
    assert forge_github.ci_runs_argv(SLUG, "ci.yml", "main") in world.calls
    assert world.moves == [(B, False)]
    assert world.restarts() == [["systemctl", "--user", "try-restart", "coxswain-chair.service", "coxswain-lander.service"]]
    assert world.doctors() == 1
    assert world.calls.index(world.restarts()[0]) < world.calls.index(DOCTOR)
    assert _record(tmp_path) | {"at": "t"} == {"at": "t", "previous": C, "chosen": B, "outcome": "upgraded", "detail": ""}
    assert f"upgraded {C[:12]} -> {B[:12]}" in out


def _units(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".config" / "systemd" / "user"


def test_an_upgrade_gives_a_unit_installed_before_kill_mode_the_setting_and_reloads_before_it_restarts(
    world: World, tmp_path: Path, capsys
):
    (_units(tmp_path) / "coxswain-chair.service").write_text(LEGACY_UNIT)
    (_units(tmp_path) / "coxswain-chair@.service").write_text(LEGACY_UNIT.replace("chair run", "chair worker --kind %i"))
    assert _upgrade(tmp_path) == 0
    for name in ("coxswain-chair.service", "coxswain-chair@.service"):
        text = (_units(tmp_path) / name).read_text()
        assert text.count("KillMode=process") == 1 and "WantedBy=default.target" in text and "ExecStart=/c chair" in text
    assert (_units(tmp_path) / "coxswain-lander.service").read_text() == CURRENT_UNIT  # not the chair's: untouched
    assert world.calls.count(RELOAD) == 1
    assert world.calls.index(RELOAD) < world.calls.index(world.restarts()[0])
    assert "set KillMode=process in coxswain-chair.service, coxswain-chair@.service" in capsys.readouterr().out


def test_a_failed_reload_restarts_nothing_so_no_land_in_flight_is_killed(world: World, tmp_path: Path, capsys):
    (_units(tmp_path) / "coxswain-chair.service").write_text(LEGACY_UNIT)
    world.reload_exit = 1
    assert _upgrade(tmp_path) != 0
    assert world.restarts() == []
    assert "not restarting" in capsys.readouterr().out


def test_an_upgrade_that_moves_nothing_still_refreshes_the_unit_and_restarts_nothing(world: World, tmp_path: Path):
    (_units(tmp_path) / "coxswain-chair.service").write_text(LEGACY_UNIT)
    world.head = B
    assert _upgrade(tmp_path) == 0
    assert "KillMode=process" in (_units(tmp_path) / "coxswain-chair.service").read_text()
    assert world.calls.count(RELOAD) == 1 and world.restarts() == []


def test_a_release_upgrade_refreshes_the_unit_too(world: World, tmp_path: Path):
    (tmp_path / "profile.yaml").write_text("team: example\n")
    (_units(tmp_path) / "coxswain-chair.service").write_text(LEGACY_UNIT)
    assert _upgrade(tmp_path) == 0
    assert "KillMode=process" in (_units(tmp_path) / "coxswain-chair.service").read_text()
    assert world.calls == [RELOAD]


def test_a_dry_run_refreshes_nothing(world: World, tmp_path: Path):
    (_units(tmp_path) / "coxswain-chair.service").write_text(LEGACY_UNIT)
    _upgrade(tmp_path, "--dry-run")
    assert (_units(tmp_path) / "coxswain-chair.service").read_text() == LEGACY_UNIT
    assert RELOAD not in world.calls


def test_edge_at_the_newest_green_commit_moves_nothing_restarts_nothing_and_writes_no_record(world: World, tmp_path: Path, capsys):
    world.head = B
    assert _upgrade(tmp_path) == 0
    assert world.moves == [] and world.restarts() == [] and world.doctors() == 0
    assert _record(tmp_path) is None
    assert "already at" in capsys.readouterr().out


def test_edge_never_moves_back_from_a_commit_newer_than_the_newest_green_one(world: World, tmp_path: Path, capsys):
    world.head = A
    assert _upgrade(tmp_path) == 0
    assert world.moves == [] and world.restarts() == []
    assert "never moves back" in capsys.readouterr().out


def test_a_failing_doctor_moves_back_restarts_again_and_records_the_rollback(world: World, tmp_path: Path, capsys):
    world.doctor_fails_at = {B}
    assert _upgrade(tmp_path) == 1
    out = capsys.readouterr().out
    assert world.moves == [(B, False), (C, False)]
    assert len(world.restarts()) == 2 and world.head == C
    record = _record(tmp_path)
    assert (record["previous"], record["chosen"], record["outcome"]) == (C, B, "rolled_back")
    assert record["detail"] == "doctor required rows failed: store: store FAIL"
    assert f"rolled back {B[:12]} -> {C[:12]}" in out


def test_a_failed_move_rolls_back_without_restarting_units_that_never_ran_the_new_code(world: World, tmp_path: Path):
    world.move_exits = {B: 2}  # the checkout moved, then a later step (its venv sync, say) failed
    assert _upgrade(tmp_path) == 1
    assert world.moves == [(B, False), (C, False)]
    assert world.restarts() == [] and world.doctors() == 0
    assert _record(tmp_path)["outcome"] == "rolled_back"


def test_a_failed_move_back_is_recorded_as_a_failed_rollback(world: World, tmp_path: Path, capsys):
    world.move_exits = {B: 2, C: 2}
    assert _upgrade(tmp_path) == 1
    assert _record(tmp_path)["outcome"] == "rollback_failed"
    assert "FAILED" in capsys.readouterr().out


def test_a_move_refused_before_the_checkout_moved_rolls_nothing_back_and_holds_nothing(world: World, tmp_path: Path, capsys):
    world.stuck, world.move_exits = True, {B: 2}  # e.g. `cox upgrade` refused a dirty checkout
    assert _upgrade(tmp_path) == 2
    assert world.moves == [(B, False)]
    assert world.restarts() == [] and world.doctors() == 0 and _record(tmp_path) is None
    assert "nothing moved" in capsys.readouterr().out


def test_a_failing_doctor_after_a_store_migration_is_not_rolled_back_past_it(world: World, tmp_path: Path, capsys):
    world.listings[B] = BASE_LISTING + "src/graphs/harness/store_ddl_0003.py\n"
    world.files[(B, "src/graphs/harness/store_ddl_0003.py")] = 'VERSION = 3\nDESCRIPTION = "drops a column"\n'
    world.files[(C, upgrade_edge.MIGRATE_MODULE)] = "def accepts(current, expected, newer):\n    return False\n"
    world.doctor_fails_at = {B}
    assert _upgrade(tmp_path) == 1
    out = capsys.readouterr().out
    assert world.moves == [(B, False)] and len(world.restarts()) == 1
    record = _record(tmp_path)
    assert (record["previous"], record["chosen"], record["outcome"]) == (C, B, "not_rolled_back")
    assert "0003" in record["detail"] and "0003" in out


def test_a_commit_that_was_rolled_back_is_held_until_a_newer_one_goes_green(world: World, tmp_path: Path, capsys):
    held = upgrade_edge.record("2026-11-13T00:00:00Z", C, B, upgrade_edge.ROLLED_BACK, "cox setup doctor exited 1")
    (tmp_path / "installs" / upgrade_edge.RECORD_FILE).write_text(json.dumps(held))
    assert _upgrade(tmp_path) == 0
    assert world.moves == [] and world.restarts() == []
    assert "was rolled back" in capsys.readouterr().out
    assert _record(tmp_path) == held
    world.green = {A, B, C}
    assert _upgrade(tmp_path) == 0
    assert world.moves == [(A, False)]


def test_a_machine_without_chair_or_lander_units_restarts_nothing_but_still_runs_the_doctor(world: World, tmp_path: Path):
    for unit in (tmp_path / "home" / ".config" / "systemd" / "user").iterdir():
        unit.unlink()
    assert _upgrade(tmp_path) == 0
    assert world.restarts() == [] and world.doctors() == 1


def test_an_edge_dry_run_plans_the_move_and_restarts_runs_and_records_nothing(world: World, tmp_path: Path):
    assert _upgrade(tmp_path, "--dry-run") == 0
    assert world.moves == [(B, True)]
    assert world.restarts() == [] and world.doctors() == 0 and _record(tmp_path) is None


def test_no_green_commit_refuses_without_moving(world: World, tmp_path: Path, capsys):
    world.green = set()
    assert _upgrade(tmp_path) == 2
    assert world.moves == [] and "refusing" in capsys.readouterr().out


def test_to_on_an_edge_machine_is_the_explicit_path_with_no_ci_lookup(world: World, tmp_path: Path):
    assert _upgrade(tmp_path, "--to", "2026.11.8") == 0
    assert world.moves == [("2026.11.8", False)]
    assert not any(argv[:2] == ["gh", "run"] for argv in world.calls)


def test_a_stable_machine_upgrades_exactly_as_before(world: World, tmp_path: Path):
    (tmp_path / "profile.yaml").write_text("team: example\n")
    assert _upgrade(tmp_path) == 0
    # The one call is the unit refresh's read-only question; the units are current, so nothing is written or reloaded.
    assert world.moves == [(None, False)]
    assert world.calls == [["systemctl", "--user", "show", "-p", "NeedDaemonReload", "coxswain-chair.service"]]


def test_the_scheduled_upgrade_does_nothing_on_a_stable_machine(world: World, tmp_path: Path, capsys):
    (tmp_path / "profile.yaml").write_text("team: example\n")
    assert _upgrade(tmp_path, "--scheduled") == 0
    assert world.moves == [] and world.calls == []
    assert "stable" in capsys.readouterr().out


def test_the_scheduled_upgrade_refuses_when_the_profile_does_not_parse(world: World, tmp_path: Path, capsys):
    (tmp_path / "profile.yaml").write_text("upgrade_channel: edge\nspend: 5\n")
    assert _upgrade(tmp_path, "--scheduled") == 2
    assert world.moves == [] and world.calls == []
    assert "refusing" in capsys.readouterr().out


def test_versions_shows_the_last_edge_upgrade(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "installs"
    (root / "coxswain").mkdir(parents=True)
    (root / "coxswain" / "manifest.toml").write_text(MANIFEST)
    rec = upgrade_edge.record("2026-11-13T00:00:00Z", C, B, upgrade_edge.UPGRADED)
    (root / upgrade_edge.RECORD_FILE).write_text(json.dumps(rec))
    monkeypatch.setattr(cli, "_checkout_facts", lambda path, fetch=False: {"present": True, "tag": None, "dirty": False})
    assert cli.main(["versions", "--root", str(root)]) == 0
    assert upgrade_edge.record_line(rec) in capsys.readouterr().out.splitlines()


def test_upgrade_to_a_commit_moves_the_checkout_by_the_release_path(tmp_path: Path, monkeypatch):
    root = tmp_path / "installs"
    (root / "coxswain").mkdir(parents=True)
    (root / "coxswain" / "manifest.toml").write_text(MANIFEST)
    monkeypatch.setattr(cli, "_checkout_facts", lambda path: {"present": True, "tag": "v2026.11.8", "dirty": False})
    monkeypatch.setattr(cli, "_load_lazy_modules", lambda: None)
    monkeypatch.setattr(cli.setup_receipts, "read_tool_extras", lambda tool_dir, package: ())
    calls = []
    monkeypatch.setattr(cli, "_real_run", lambda argv, cwd: (calls.append(argv), (0, ""))[1])
    assert cli.main(["upgrade", "--root", str(root), "--provider", "claude-code", "--to", B]) == 0
    checkout = str(root / "coxswain")
    assert calls[:5] == [
        ["git", "-C", checkout, "fetch", "--tags"],
        ["git", "-C", checkout, "checkout", B],
        ["uv", "sync", "--frozen", "--all-extras", "--inexact", "-q"],
        ["uv", "tool", "install", "--force", "-q", "--editable", f"{checkout}[parquet,postgres]"],
        [f"{checkout}/crew/install.sh"],
    ]
    assert calls[-1] == ["cox", "setup", "doctor"]


@pytest.fixture
def unit_home(tmp_path: Path, monkeypatch) -> Path:
    """A HOME with a profile and `cox` on PATH, where no tool may run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "profile.yaml").write_text(f"workspace_dir: {tmp_path}/ws\nupgrade_channel: edge\n")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: COX)

    def refuse(*_args, **_kwargs):
        raise AssertionError("installing the timer runs no tool")

    monkeypatch.setattr(cli.subprocess, "run", refuse)
    monkeypatch.setattr(cli, "_real_run", refuse)
    return tmp_path


def test_install_timer_writes_the_service_and_timer_prints_the_systemctl_lines_and_uninstall_removes_them(
    unit_home: Path, capsys
):
    unit_dir = unit_home / ".config" / "systemd" / "user"
    service, timer = unit_dir / upgrade_edge.SERVICE_NAME, unit_dir / upgrade_edge.TIMER_NAME
    profile = str(unit_home / "profile.yaml")
    assert cli.main(["upgrade", "--install-timer", "--profile", profile, "--interval-minutes", "20"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        str(service), str(timer), "systemctl --user daemon-reload", f"systemctl --user enable --now {upgrade_edge.TIMER_NAME}",
    ]
    assert f"ExecStart={COX} upgrade --scheduled --profile {profile}" in service.read_text().splitlines()
    assert "OnUnitActiveSec=20min" in timer.read_text().splitlines()
    assert cli.main(["upgrade", "--uninstall-timer"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"removed {service}", f"removed {timer}"]
    assert not service.exists() and not timer.exists()


def test_install_timer_on_a_stable_machine_says_the_timer_moves_nothing_yet(unit_home: Path, capsys):
    (unit_home / "profile.yaml").write_text(f"workspace_dir: {unit_home}/ws\n")
    assert cli.main(["upgrade", "--install-timer", "--profile", str(unit_home / "profile.yaml")]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("note: this machine's channel is stable")
    assert "OnUnitActiveSec=30min" in (unit_home / ".config" / "systemd" / "user" / upgrade_edge.TIMER_NAME).read_text()


def _export_garage(home: Path) -> None:
    garage = home / ".config" / "agent-tools" / "garage.env"
    garage.parent.mkdir(parents=True, exist_ok=True)
    garage.write_text("export STORE_URL=postgresql://store.example/x\n", encoding="utf-8")


def test_install_timer_refuses_an_export_style_garage_file_and_names_the_flag(unit_home: Path, capsys):
    _export_garage(unit_home)
    assert cli.main(["upgrade", "--install-timer", "--profile", str(unit_home / "profile.yaml")]) == 2
    assert "--environment-file" in capsys.readouterr().out
    assert not (unit_home / ".config" / "systemd").exists()


def test_install_timer_takes_an_environment_file_in_name_value_form(unit_home: Path, capsys):
    _export_garage(unit_home)
    plain = unit_home / "garage.systemd.env"
    plain.write_text("STORE_URL=postgresql://store.example/x\n", encoding="utf-8")
    profile = str(unit_home / "profile.yaml")
    assert cli.main(["upgrade", "--install-timer", "--profile", profile, "--environment-file", str(plain)]) == 0
    service = unit_home / ".config" / "systemd" / "user" / upgrade_edge.SERVICE_NAME
    assert f"EnvironmentFile=-{plain}" in service.read_text().splitlines()
