"""`cox upgrade` on the edge channel: resolve the newest green commit on main, move to it by the `--to` path, restart
the units the machine has, run the doctor, roll back on a failure, record the outcome; and the timer that runs it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_tools import cli, forge_github, upgrade_edge

A, B, C = ("a" * 40, "b" * 40, "c" * 40)  # newest first on main: A is the tip, B the newest green, C installed
SLUG = "example-org/example-repo"
MANIFEST = f'[coxswain]\nversion = "2026.11.8"\nrepo = "{SLUG}"\n\n[providers.claude-code]\nstatus = "supported"\n'
BASE_LISTING = "src/graphs/harness/store_ddl_0001.py\nsrc/graphs/harness/store_ddl_0002.py\nsrc/graphs/harness/cli.py\n"
COX = "/opt/example/bin/cox"


class World:
    """The checkout, the forge and the service manager as the edge flow's commands see them."""

    def __init__(self, root: Path) -> None:
        self.checkout = str(root / "coxswain")
        self.head = C
        self.history = [A, B, C]
        self.green = {B, C}
        self.listings = {A: BASE_LISTING, B: BASE_LISTING, C: BASE_LISTING}
        self.doctor_fails_at: set[str] = set()
        self.calls: list[list[str]] = []
        self.moves: list[tuple[str | None, bool]] = []
        self.move_exits: dict[str, int] = {}  # a failing move still moved the checkout unless `stuck`
        self.stuck = False

    def run(self, argv: list, cwd: str | None) -> tuple[int, str]:
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
        if argv[:3] == ["gh", "run", "list"]:
            runs = [{"headSha": sha, "status": "completed", "conclusion": "success" if sha in self.green else "failure"}
                    for sha in self.history]
            return 0, json.dumps(runs)
        if argv[:2] == ["systemctl", "--user"]:
            return 0, ""
        if argv == ["cox", "setup", "doctor"]:
            return (1, "store  FAIL") if self.head in self.doctor_fails_at else (0, "all ok")
        raise AssertionError(f"unexpected command: {argv}")

    def move(self, a) -> int:
        self.moves.append((a.to, a.dry_run))
        if not a.dry_run and not self.stuck:
            self.head = a.to
        return self.move_exits.get(a.to, 0)

    def restarts(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[:2] == ["systemctl", "--user"]]

    def doctors(self) -> int:
        return sum(argv == ["cox", "setup", "doctor"] for argv in self.calls)


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> World:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name in ("coxswain-chair.service", "coxswain-lander.service"):
        (unit_dir / name).write_text("[Service]\n")
    root = tmp_path / "installs"
    (root / "coxswain" / ".git").mkdir(parents=True)
    (root / "coxswain" / "manifest.toml").write_text(MANIFEST)
    (tmp_path / "profile.yaml").write_text("upgrade_channel: edge\n")
    w = World(root)
    monkeypatch.setattr(cli, "_real_run", w.run)
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
    assert world.calls.index(world.restarts()[0]) < world.calls.index(["cox", "setup", "doctor"])
    assert _record(tmp_path) | {"at": "t"} == {"at": "t", "previous": C, "chosen": B, "outcome": "upgraded", "detail": ""}
    assert f"upgraded {C[:12]} -> {B[:12]}" in out


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
    assert "cox setup doctor exited 1" in record["detail"]
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
    assert world.moves == [(None, False)] and world.calls == []


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
        ["uv", "tool", "install", "--force", "-q", "--editable", checkout],
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


def test_install_timer_refuses_off_linux(unit_home: Path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli.main(["upgrade", "--install-timer", "--profile", str(unit_home / "profile.yaml")]) == 2
    assert not (unit_home / ".config" / "systemd").exists()
    assert "refusing" in capsys.readouterr().out
