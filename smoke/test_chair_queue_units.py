"""C6, end to end: `cox chair service --install --queues`, again, then `--uninstall --queues`, in a sandboxed home.

The installed `cox` writes the queue workers' units (the systemd template on Linux, one launchd agent per kind on
macOS) and runs the steps that start them and disable the tick's unit. The service manager is a recording stand-in on
the sandbox's PATH, never this machine's: `launchctl` keeps which labels are loaded in a file, so `launchctl print`
answers as launchd would, and `systemctl` answers `active`. Install twice writes and restarts nothing the second time;
the rollback refuses while `chair.queues` lists a kind and, once it lists none, removes every file and starts the tick's
unit again. The files are read back with the platform's own parser where there is one (plistlib).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

KINDS = ("housekeeping", "watch", "quarantine", "decompose", "launch")

FAKE = """#!/bin/sh
# A stand-in for {name}: records each call; launchctl keeps its loaded labels in a file.
echo "{name} $*" >> "$COX_FAKE_LOG"
state="$COX_FAKE_STATE"
touch "$state"
case "{name} $1" in
  "launchctl print") label="${{2##*/}}"; grep -qx "$label" "$state" && exit 0; exit 113 ;;
  "launchctl bootstrap") label="$(basename "$3" .plist)"; grep -qx "$label" "$state" && exit 37; echo "$label" >> "$state" ;;
  "launchctl bootout") label="${{2##*/}}"; grep -qx "$label" "$state" || exit 3
    grep -vx "$label" "$state" > "$state.new"; mv "$state.new" "$state" ;;
  "systemctl is-active"|"systemctl --user") [ "$2" = "is-active" ] && for u in "$@"; do case "$u" in *.service) echo active;; esac; done ;;
esac
exit 0
"""


@pytest.fixture
def machine(sandbox, tmp_path: Path):
    """The sandbox's home, its profile and store, with the stand-ins first on PATH."""
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name in ("systemctl", "launchctl"):
        (fakes / name).write_text(FAKE.format(name=name), encoding="utf-8")
        (fakes / name).chmod(0o755)
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    base = profile.read_text(encoding="utf-8")
    env = {**sandbox.env, "PATH": f"{fakes}:{sandbox.env['PATH']}",
           "COX_FAKE_LOG": str(tmp_path / "calls.log"), "COX_FAKE_STATE": str(tmp_path / "loaded")}
    return sandbox.home, base, profile, env, tmp_path


def _profile(profile: Path, base: str, queues: str) -> None:
    profile.write_text(f"{base}chair:\n  queues: [{queues}]\n", encoding="utf-8")


def _cox(install, env: dict[str, str], home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(install.cox), *args], env=env, cwd=home, capture_output=True, text=True, timeout=120)


def _calls(root: Path) -> list[str]:
    log = root / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _files(home: Path) -> list[Path]:
    if sys.platform == "darwin":
        return [home / "Library/LaunchAgents" / f"coxswain-chair-{k}.plist" for k in KINDS]
    return [home / ".config/systemd/user/coxswain-chair@.service"]


def _tick_unit(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library/LaunchAgents/coxswain-chair.plist"
    return home / ".config/systemd/user/coxswain-chair.service"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="the queue units are systemd or launchd only")
def test_install_twice_then_roll_back_in_a_sandboxed_home(machine, sandbox):
    install = sandbox.install
    home, base, profile, env, root = machine
    tick = _tick_unit(home)
    tick.parent.mkdir(parents=True, exist_ok=True)
    tick.write_text("the tick's unit, as `cox chair service --install` left it\n", encoding="utf-8")
    if sys.platform == "darwin":
        (root / "loaded").write_text("coxswain-chair\n", encoding="utf-8")  # the tick's agent is running

    _profile(profile, base, "housekeeping, watch")
    refused = _cox(install, env, home, "chair", "service", "--install", "--queues", "--label", "chair")
    assert refused.returncode == 2 and "quarantine, decompose, launch" in refused.stdout, refused.stdout + refused.stderr
    assert not any(p.exists() for p in _files(home)) and _calls(root) == []

    _profile(profile, base, ", ".join(KINDS))
    first = _cox(install, env, home, "chair", "service", "--install", "--queues", "--label", "chair")
    assert first.returncode == 0, first.stdout + first.stderr
    assert all(p.is_file() and p.stat().st_mode & 0o777 == 0o600 for p in _files(home)), first.stdout
    calls = _calls(root)
    uid = os.getuid()
    if sys.platform == "darwin":
        for kind in KINDS:
            agent = plistlib.loads(_files(home)[KINDS.index(kind)].read_bytes())
            assert agent["Label"] == f"coxswain-chair-{kind}"
            assert agent["ProgramArguments"][-7:] == ["worker", "--kind", kind, "--label", "chair", "--profile", str(profile)]
        assert f"launchctl bootout gui/{uid}/coxswain-chair" in calls and f"launchctl disable gui/{uid}/coxswain-chair" in calls
        assert sorted((root / "loaded").read_text().split()) == sorted(f"coxswain-chair-{k}" for k in KINDS)
    else:
        text = _files(home)[0].read_text(encoding="utf-8")
        assert f"ExecStart={install.cox} chair worker --kind %i --label chair --profile {profile}" in text
        instances = " ".join(f"coxswain-chair@{k}.service" for k in KINDS)
        assert calls == ["systemctl --user disable --now coxswain-chair.service", "systemctl --user daemon-reload",
                         f"systemctl --user enable --now {instances}", f"systemctl --user restart {instances}"]

    status = _cox(install, env, home, "chair", "service", "--install", "--queues", "--label", "chair")
    assert status.returncode == 0 and status.stdout.count("unchanged ") == len(_files(home)), status.stdout
    again = _calls(root)[len(calls):]
    assert not any(" restart " in f" {c} " or " bootstrap " in f" {c} " or " bootout " in f" {c} " for c in again), again

    rollback = _cox(install, env, home, "chair", "service", "--uninstall", "--queues")
    assert rollback.returncode == 2 and "Set chair.queues to [] first" in rollback.stdout, rollback.stdout
    assert all(p.is_file() for p in _files(home))

    _profile(profile, base, "")
    mark = len(_calls(root))
    back = _cox(install, env, home, "chair", "service", "--uninstall", "--queues")
    assert back.returncode == 0, back.stdout + back.stderr
    assert not any(p.exists() for p in _files(home)) and tick.is_file()
    after = _calls(root)[mark:]
    if sys.platform == "darwin":
        assert after[-1] == f"launchctl bootstrap gui/{uid} {tick}"
        assert (root / "loaded").read_text().split() == ["coxswain-chair"]
    else:
        assert after[-1] == "systemctl --user enable --now coxswain-chair.service"
    # The rollback again finds nothing left to stop, and still ends with the tick's unit up.
    twice = _cox(install, env, home, "chair", "service", "--uninstall", "--queues")
    assert twice.returncode == 0, twice.stdout + twice.stderr
