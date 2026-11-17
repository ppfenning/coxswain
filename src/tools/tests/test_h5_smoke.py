"""H5 end to end: the real `cox` in a subprocess, against real git repos and a real hung child, with a throwaway home.
The lane-sync proof runs the real read script against real clones in test_lane_checkout_sync.py."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_tools import chair_service, upgrade_edge

TOOLS = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = os.pathsep.join(str(TOOLS.parent / name) for name in ("tools", "graphs", "cartridges"))  # this tree, never an older editable install
SLUG = "example-org/example-repo"
MANIFEST = f'[coxswain]\nversion = "2026.11.8"\nrepo = "{SLUG}"\n\n[providers.claude-code]\nstatus = "supported"\n'

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells and process groups")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd,
                          capture_output=True, text=True, check=True, timeout=60)
    return done.stdout.strip()


def _script(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def machine(tmp_path: Path) -> dict:
    """A home with `cox` (this tree's) first on PATH, a profile, and nothing else of the real machine."""
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    (home / ".config" / "agent-tools").mkdir(parents=True)
    bin_dir.mkdir()
    _script(bin_dir / "cox", f'#!/bin/sh\nexec "{sys.executable}" -m agent_tools.cli "$@"\n')
    profile = home / ".config" / "agent-tools" / "profile.yaml"
    (home / "ws").mkdir()
    profile.write_text(f"workspace_dir: {home / 'ws'}\nupgrade_channel: edge\n", encoding="utf-8")
    env = {"HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}", "PYTHONPATH": SOURCE_ROOTS,
           "AGENT_TOOLS_PROFILE": str(profile), "LANG": "C.UTF-8"}
    return {"home": home, "bin": bin_dir, "profile": profile, "env": env, "tmp": tmp_path}


def _cox(machine: dict, *args: str, extra_env: dict | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["cox", *args], env={**machine["env"], **(extra_env or {})}, cwd=machine["tmp"],
                          capture_output=True, text=True, timeout=timeout)


def test_a_hung_gh_is_killed_and_reported_by_the_scheduled_edge_upgrade(machine: dict) -> None:
    """Done when: a hung `gh` is killed and reported. The scheduled upgrade fetches a real origin, then asks a `gh`
    that never answers for the CI runs; the bounded runner kills it and the oneshot exits with the failure line."""
    tmp = machine["tmp"]
    origin, installs = tmp / "origin.git", tmp / "installs"
    _git(tmp, "init", "--bare", "-b", "main", str(origin))
    _git(tmp, "clone", str(origin), str(installs / "coxswain"))
    checkout = installs / "coxswain"
    (checkout / "manifest.toml").write_text(MANIFEST, encoding="utf-8")
    _git(checkout, "checkout", "-b", "main")
    _git(checkout, "add", "manifest.toml")
    _git(checkout, "commit", "-m", "seed")
    _git(checkout, "push", "origin", "main")
    pid_file = tmp / "gh.pid"
    _script(machine["bin"] / "gh", f'#!/bin/sh\necho $$ > "{pid_file}"\nexec sleep 300\n')
    started = time.monotonic()
    done = _cox(machine, "upgrade", "--scheduled", "--root", str(installs), "--profile", str(machine["profile"]),
                "--provider", "claude-code", extra_env={"COXSWAIN_GH_TIMEOUT_SECONDS": "1"})
    assert time.monotonic() - started < 60
    assert done.returncode == 2, done.stdout + done.stderr
    assert f"refusing: cannot list the ci.yml runs on main of {SLUG}: gh run timed out after 1 seconds" in done.stdout
    hung = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(hung, 0)  # killed with its process group, not left sleeping


def test_all_three_units_read_the_one_env_file_that_setup_unit_env_writes(machine: dict) -> None:
    """Done when: all three units read one env file. `cox setup unit-env` writes it with the stored token; the chair,
    lander and upgrade installs then each load that same file; nothing printed carries the token."""
    home = machine["home"]
    (home / ".config" / "agent-tools" / "claude-token").write_text("tok-smoke-1\n", encoding="utf-8")
    env_file = home / ".config" / "agent-tools" / "garage.env"
    env_file.write_text("STORE_HOST=db.example\n", encoding="utf-8")
    wrote = _cox(machine, "setup", "unit-env")
    assert wrote.returncode == 0, wrote.stdout + wrote.stderr
    assert wrote.stdout.splitlines() == [str(env_file), "names: CLAUDE_CODE_OAUTH_TOKEN, STORE_HOST"]
    assert env_file.read_text() == "CLAUDE_CODE_OAUTH_TOKEN=tok-smoke-1\nSTORE_HOST=db.example\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    for args in (("chair", "service", "--install"), ("lander", "service", "--install"), ("upgrade", "--install-timer")):
        # The suites' store stand-in: installing a unit file reads no store, but the chair and lander groups are gated.
        done = _cox(machine, *args, extra_env={"COXSWAIN_STORE_DOUBLE": "sqlite"})
        assert done.returncode == 0, (args, done.stdout + done.stderr)
        assert "tok-smoke-1" not in done.stdout + done.stderr
    unit_dir = home / ".config" / "systemd" / "user"
    line = f"EnvironmentFile=-{env_file}"
    for name in ("coxswain-chair.service", "coxswain-lander.service", "coxswain-upgrade.service"):
        assert (unit_dir / name).read_text().splitlines().count(line) == 1, name
        assert stat.S_IMODE((unit_dir / name).stat().st_mode) in (0o600, 0o644), name


def test_the_launchd_agents_carry_no_secret_and_read_the_env_file_at_launch(machine: dict) -> None:
    """Regression: the plists embedded CLAUDE_CODE_OAUTH_TOKEN in clear at mode 0644 and went stale on rotation. After
    a real `cox setup unit-env`, the three agents' plists hold no value from the file and name it instead; running an
    agent's ProgramArguments for real (with `env` standing in for cox) shows the sourced token, and a rotated token
    reaches the next launch with no reinstall."""
    home = machine["home"]
    token_file = home / ".config" / "agent-tools" / "claude-token"
    token_file.write_text("tok-smoke-2\n", encoding="utf-8")
    assert _cox(machine, "setup", "unit-env").returncode == 0
    env_file = str(home / ".config" / "agent-tools" / "garage.env")
    plists = {
        "chair": chair_service.chair_plist_text("/opt/cox", str(home / "ws"), "chair", 60, "/usr/bin", "/p.yaml", env_file),
        "watchdog": chair_service.watchdog_plist_text("/opt/cox", "/p.yaml", "/usr/bin", 5, env_file),
        "upgrade": upgrade_edge.plist_text("/opt/cox", "/p.yaml", "/usr/bin", 30, env_file),
    }
    for name, text in plists.items():
        assert "tok-smoke-2" not in text, name
        assert plistlib.loads(text.encode())["ProgramArguments"][4] == env_file, name
    arguments = plistlib.loads(plists["upgrade"].encode())["ProgramArguments"]
    launched = subprocess.run([*arguments[:5], "env"], env={"PATH": os.environ["PATH"]}, capture_output=True,
                              text=True, timeout=30)
    assert "CLAUDE_CODE_OAUTH_TOKEN=tok-smoke-2" in launched.stdout.splitlines()
    token_file.write_text("tok-smoke-3\n", encoding="utf-8")
    assert _cox(machine, "setup", "unit-env").returncode == 0
    again = subprocess.run([*arguments[:5], "env"], env={"PATH": os.environ["PATH"]}, capture_output=True,
                           text=True, timeout=30)
    assert "CLAUDE_CODE_OAUTH_TOKEN=tok-smoke-3" in again.stdout.splitlines()


def test_setup_unit_env_refuses_an_export_style_file_and_leaves_it_as_it_was(machine: dict) -> None:
    env_file = machine["home"] / ".config" / "agent-tools" / "garage.env"
    env_file.write_text("export STORE_HOST=db.example\n", encoding="utf-8")
    done = _cox(machine, "setup", "unit-env")
    assert done.returncode == 2
    assert done.stdout.startswith(f"refusing: {env_file}: export lines (1)")
    assert env_file.read_text() == "export STORE_HOST=db.example\n"
