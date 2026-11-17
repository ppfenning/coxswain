"""H5, the pure core: example inputs and outputs, no fakes. The end-to-end proofs are in test_h5_smoke.py."""

from __future__ import annotations

import ast
import json
import plistlib
import re
import subprocess
import time
from pathlib import Path, PurePosixPath

import pytest

from agent_tools import bounded_run, chair_service, doctor, lander_cli, unit_env, upgrade_edge

SRC = Path(__file__).resolve().parents[2]
HOME = PurePosixPath("/home/user")
ENV_FILE = "/home/user/.config/agent-tools/garage.env"
COX = "/home/user/.local/bin/cox"
PROFILE = "/home/user/.config/agent-tools/profile.yaml"
PATH = "/home/user/.local/bin:/usr/bin"
DOCTOR = ["cox", "setup", "doctor", "--json"]


def _doctor_output(*failing: str) -> str:
    checks = ["profile", "workspace", "harness venv", "core importable", "install bins", "store", "schema",
              "claude token", "plugins"]
    return json.dumps({"rows": [{"check": c, "ok": c not in failing, "detail": f"{c} detail"} for c in checks]})


# A hung gh is killed and reported.

def test_default_timeouts_by_program_and_git_subcommand() -> None:
    assert bounded_run.default_timeout(["gh", "run", "list"]) == bounded_run.GH_TIMEOUT_SECONDS == 120
    assert bounded_run.default_timeout(["gh", "pr", "view"], {bounded_run.GH_TIMEOUT_ENV: "2"}) == 2
    assert bounded_run.default_timeout(["gh", "pr", "view"], {bounded_run.GH_TIMEOUT_ENV: "soon"}) == 120
    assert bounded_run.default_timeout(["git", "-C", "/r", "rev-parse", "HEAD"]) == bounded_run.GIT_TIMEOUT_SECONDS
    assert bounded_run.default_timeout(["git", "-c", "a=b", "-C", "/r", "fetch", "origin"]) == 600
    assert bounded_run.default_timeout(["/usr/bin/git", "commit", "-m", "x"]) == 600
    assert bounded_run.git_subcommand(["git", "--no-pager", "-C", "/r", "log"]) == "log"


def test_completed_turns_a_real_hung_child_into_exit_124_with_the_failure_line() -> None:
    started = time.monotonic()
    done = bounded_run.completed(["sleep", "30"], 0.5, capture_output=True, text=True)
    assert time.monotonic() - started < 10
    assert (done.returncode, done.stdout, done.stderr) == (124, "", "sleep 30 timed out after 0.5 seconds")
    merged = bounded_run.completed(["sleep", "30"], 0.5, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert (merged.returncode, merged.stdout) == (124, b"\nsleep 30 timed out after 0.5 seconds")


def _untimed_gh_and_git_calls(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
                continue
            first = node.args[0] if node.args else None
            literal = (isinstance(first, ast.List) and first.elts and isinstance(first.elts[0], ast.Constant)
                       and first.elts[0].value in ("gh", "git"))
            if literal and not any(k.arg == "timeout" for k in node.keywords):
                found.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return found


def test_no_gh_or_git_subprocess_call_in_the_tools_or_the_harness_runs_without_a_timeout() -> None:
    """Regression: the edge timer hung on a `gh` with no timeout; every literal gh or git argv now has one."""
    assert _untimed_gh_and_git_calls(SRC / "tools" / "agent_tools") == []
    assert _untimed_gh_and_git_calls(SRC / "graphs" / "harness") == []


def test_the_tools_and_the_harness_share_one_gh_timeout() -> None:
    """Regression: bounded_run said 30 seconds and epic.py 120; the harness twin and epic now carry the one value."""
    twin = (SRC / "graphs" / "harness" / "bounded_proc.py").read_text(encoding="utf-8")
    numbers = {name: int(value) for name, value in re.findall(r"^(\w+_SECONDS) = (\d+)$", twin, re.MULTILINE)}
    assert numbers == {"GH_TIMEOUT_SECONDS": bounded_run.GH_TIMEOUT_SECONDS,
                       "GIT_TIMEOUT_SECONDS": bounded_run.GIT_TIMEOUT_SECONDS,
                       "GIT_SLOW_TIMEOUT_SECONDS": bounded_run.GIT_SLOW_TIMEOUT_SECONDS}
    epic = (SRC / "graphs" / "harness" / "epic.py").read_text(encoding="utf-8")
    assert "_GH_TIMEOUT_SECONDS = bounded_proc.GH_TIMEOUT_SECONDS" in epic


# An advisory doctor failure does not roll back.

def test_an_advisory_doctor_failure_does_not_roll_the_edge_upgrade_back() -> None:
    """Regression: the edge read the doctor's whole exit code, so a token file rolled an upgrade back."""
    failure, warning = doctor.edge_verdict(DOCTOR, 1, _doctor_output("claude token", "plugins"), {}, {})
    assert failure is None
    assert warning == ("warning: doctor advisory rows failed (not rolled back): "
                       "claude token: claude token detail; plugins: plugins detail")


@pytest.mark.parametrize("check", ["schema", "install bins", "workspace", "profile"])
def test_a_required_row_the_first_tier_left_out_rolls_the_upgrade_back(check: str) -> None:
    """Regression: schema (a pending migration), install bins, workspace and profile were advisory."""
    failure, _warning = doctor.edge_verdict(DOCTOR, 1, _doctor_output(check), {}, {})
    assert failure == f"doctor required rows failed: {check}: {check} detail"


def test_a_unit_running_before_the_upgrade_and_down_after_it_rolls_back() -> None:
    before = {"coxswain-chair.service": "active", "coxswain-lander.service": "inactive"}
    after = {"coxswain-chair.service": "failed", "coxswain-lander.service": "inactive"}
    failure, _ = doctor.edge_verdict(DOCTOR, 0, _doctor_output(), before, after)
    assert failure == "doctor required rows failed: units start: coxswain-chair.service is failed"
    assert doctor.edge_verdict(DOCTOR, 0, _doctor_output(), before, {**after, "coxswain-chair.service": "active"}) == (
        None, None)


def test_doctor_output_with_no_rows_is_a_failure_naming_the_command() -> None:
    assert doctor.edge_verdict(DOCTOR, 124, "cox setup timed out after 600 seconds", {}, {}) == (
        "cox setup doctor --json exited 124 with no rows to judge: cox setup timed out after 600 seconds", None)
    assert doctor.rows_from_json("noise first\n" + _doctor_output())[0]["check"] == "profile"


# All three units read one env file.

def test_the_chair_lander_and_upgrade_units_carry_the_same_environment_file_line() -> None:
    line = unit_env.environment_file_line(ENV_FILE)
    chair = chair_service.unit_text(COX, "/home/user/ws", "chair", 60, ENV_FILE, PROFILE, PATH)
    lander = lander_cli.unit_text(COX, "/home/user/ws", 30, PROFILE, PATH, ENV_FILE)
    upgrade = upgrade_edge.service_text(COX, PROFILE, PATH, ENV_FILE)
    assert [text.splitlines().count(line) for text in (chair, lander, upgrade)] == [1, 1, 1]
    assert unit_env.env_file_path(str(HOME)) == ENV_FILE


def test_the_env_file_keeps_its_lines_sets_the_token_and_refuses_what_a_unit_would_misread() -> None:
    assert unit_env.file_text("STORE_HOST=db.example\n", "tok-1") == "CLAUDE_CODE_OAUTH_TOKEN=tok-1\nSTORE_HOST=db.example\n"
    assert unit_env.file_text(None, None) == ""
    with pytest.raises(ValueError, match=r"export lines \(1\)"):
        unit_env.file_text("export STORE_HOST=db.example\n", "tok-1")
    with pytest.raises(ValueError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        unit_env.file_text(None, "tok $1")


# The macOS lane host upgrades on a schedule.

def test_the_macos_schedule_is_a_launchd_agent_running_the_scheduled_upgrade_with_the_env_file_values() -> None:
    files = upgrade_edge.schedule_files("darwin", HOME, COX, PROFILE, PATH, 30, ENV_FILE,
                                        {"CLAUDE_CODE_OAUTH_TOKEN": "tok-1", "NOTE": "a b & c"})
    assert [path for path, _text in files] == [HOME / "Library/LaunchAgents/coxswain-upgrade.plist"]
    plist = plistlib.loads(files[0][1].encode())
    assert plist == {
        "Label": "coxswain-upgrade",
        "ProgramArguments": [COX, "upgrade", "--scheduled", "--profile", PROFILE],
        "StartInterval": 1800,
        # launchd takes any string: a value with a space reaches the agent as written (the review's plist bug).
        "EnvironmentVariables": {"CLAUDE_CODE_OAUTH_TOKEN": "tok-1", "NOTE": "a b & c", "PATH": PATH},
    }
    assert upgrade_edge.activation_lines("darwin", files[0][0]) == [
        "launchctl bootstrap gui/$(id -u) /home/user/Library/LaunchAgents/coxswain-upgrade.plist"]


def test_the_linux_schedule_is_a_bounded_oneshot_and_its_timer() -> None:
    files = upgrade_edge.schedule_files("linux", HOME, COX, PROFILE, PATH, 20, ENV_FILE, {})
    assert [str(path) for path, _ in files] == ["/home/user/.config/systemd/user/coxswain-upgrade.service",
                                                "/home/user/.config/systemd/user/coxswain-upgrade.timer"]
    service = files[0][1].splitlines()
    assert "TimeoutStartSec=30min" in service and f"EnvironmentFile=-{ENV_FILE}" in service
    assert "OnUnitActiveSec=20min" in files[1][1].splitlines()
