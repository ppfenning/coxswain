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
CHAIR, LANDER = "coxswain-chair.service", "coxswain-lander.service"


def _doctor_output(*failing: str, without: tuple[str, ...] = ()) -> str:
    checks = ["profile", "workspace", "harness venv", "core importable", "install bins", "chair unit PATH", "store",
              "schema", "claude token", "plugins"]
    rows = [{"check": c, "ok": c not in failing, "detail": f"{c} detail"} for c in checks if c not in without]
    return json.dumps({"rows": rows})


# A hung gh is killed and reported.

def test_default_timeouts_by_program_and_git_subcommand() -> None:
    assert bounded_run.default_timeout(["gh", "run", "list"]) == bounded_run.GH_TIMEOUT_SECONDS == 120
    assert bounded_run.default_timeout(["gh", "pr", "view"], {bounded_run.GH_TIMEOUT_ENV: "2"}) == 2
    assert bounded_run.default_timeout(["gh", "pr", "view"], {bounded_run.GH_TIMEOUT_ENV: "soon"}) == 120
    assert bounded_run.default_timeout(["git", "-C", "/r", "rev-parse", "HEAD"]) == bounded_run.GIT_TIMEOUT_SECONDS
    assert bounded_run.default_timeout(["git", "-c", "a=b", "-C", "/r", "fetch", "origin"]) == 600
    assert bounded_run.default_timeout(["/usr/bin/git", "commit", "-m", "x"]) == 600
    assert bounded_run.default_timeout(["uv", "sync", "--frozen"]) == bounded_run.STEP_TIMEOUT_SECONDS == 1800
    assert bounded_run.git_subcommand(["git", "--no-pager", "-C", "/r", "log"]) == "log"


def test_completed_turns_a_real_hung_child_into_exit_124_with_the_failure_line() -> None:
    started = time.monotonic()
    done = bounded_run.completed(["sleep", "30"], 0.5, capture_output=True, text=True)
    assert time.monotonic() - started < 10
    assert (done.returncode, done.stdout, done.stderr) == (124, "", "sleep 30 timed out after 0.5 seconds")
    merged = bounded_run.completed(["sleep", "30"], 0.5, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert (merged.returncode, merged.stdout) == (124, b"\nsleep 30 timed out after 0.5 seconds")
    shell = bounded_run.completed("sleep 30", 0.5, shell=True, capture_output=True, text=True)
    assert (shell.returncode, shell.stderr) == (124, "sleep 30 timed out after 0.5 seconds")


_SPAWNERS = frozenset({"run", "Popen", "check_output", "call", "check_call"})

# Each call that may run without `timeout=`, by (file, enclosing function), and why.
ALLOWED_UNTIMED = {
    ("tools/agent_tools/bounded_run.py", "popen_runner"): "the Popen whose communicate() carries the timeout",
    ("tools/agent_tools/chair_land_proc.py", "_run"): "the land's Popen; communicate(timeout=spec.timeout_s) bounds it",
    ("tools/agent_tools/cli.py", "_route_launch"): "a detached background run, never waited on here",
    ("tools/agent_tools/cli.py", "_run_decompose_for_result"): "Popen waited with _DECOMPOSE_TIMEOUT_S, killed past it",
    ("tools/agent_tools/cli.py", "_spawn"): "a detached child in its own session, never waited on",
    ("tools/agent_tools/cli.py", "spawn_land"): "a detached land process, which bounds itself",
    ("tools/agent_tools/inbox_cli.py", "subprocess_runner"): "a command a person runs with the terminal attached",
    ("tools/agent_tools/plan.py", "serve"): "a long-lived local server and a browser, both detached",
    ("tools/agent_tools/store_switch.py", "default_copy_store"): "a whole-store copy a person runs; it scales with data",
    ("graphs/harness/checks.py", "_serial_rerun_passes"): "Popen waited with communicate(timeout=limit)",
    ("graphs/harness/serial_tests.py", "pytest_sessionfinish"): "pytest's own serial pass, bounded by the suite",
}


def _untimed_in(tree: ast.Module) -> set[str]:
    """The enclosing function of every subprocess spawn in `tree` with no `timeout=`, aliases of the module or of its
    functions included."""
    modules, names = {"subprocess"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "subprocess"}
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            names |= {a.asname or a.name for a in node.names if a.name in _SPAWNERS}
    found, stack = set(), [(tree, "<module>")]
    while stack:
        node, function = stack.pop()
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
            if isinstance(child, ast.Call):
                f = child.func
                spawn = (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in modules
                         and f.attr in _SPAWNERS) or (isinstance(f, ast.Name) and f.id in names)
                if spawn and not any(k.arg == "timeout" for k in child.keywords):
                    found.add(inner)
            stack.append((child, inner))
    return found


def _untimed_calls(root: Path) -> set[tuple[str, str]]:
    """(file, enclosing function) of every untimed subprocess spawn under `root`."""
    return {(str(path.relative_to(SRC)), function)
            for path in sorted(root.rglob("*.py"))
            for function in _untimed_in(ast.parse(path.read_text(encoding="utf-8")))}


def test_the_guard_sees_an_aliased_spawn_and_a_timed_one_passes() -> None:
    source = ("from subprocess import run as go\nimport subprocess as sp\n"
              "def a():\n    go(['gh'])\ndef b():\n    sp.Popen(['x'])\ndef c():\n    sp.run(['git'], timeout=5)\n")
    assert _untimed_in(ast.parse(source)) == {"a", "b"}


def test_no_subprocess_in_the_tools_or_the_harness_runs_without_a_timeout_outside_the_allowlist() -> None:
    """Regression: the edge timer hung on a `gh` with no timeout, and an adapter's `gh pr list` (`_run_argv`) had none
    either. Every spawn passes `timeout=` (the bounded helpers do) or is named in ALLOWED_UNTIMED with its reason."""
    found = _untimed_calls(SRC / "tools" / "agent_tools") | _untimed_calls(SRC / "graphs" / "harness")
    assert sorted(found - ALLOWED_UNTIMED.keys()) == []
    assert sorted(ALLOWED_UNTIMED.keys() - found) == []  # an entry that no longer matches anything is removed


def test_the_tools_and_the_harness_share_one_set_of_bounds() -> None:
    """Regression: bounded_run said 30 seconds and epic.py 120; the harness twin and epic now carry the one value."""
    twin = (SRC / "graphs" / "harness" / "bounded_proc.py").read_text(encoding="utf-8")
    numbers = {name: int(value) for name, value in re.findall(r"^(\w+_SECONDS) = (\d+)$", twin, re.MULTILINE)}
    assert numbers == {name: getattr(bounded_run, name) for name in numbers}
    assert set(numbers) == {"GH_TIMEOUT_SECONDS", "GIT_TIMEOUT_SECONDS", "GIT_SLOW_TIMEOUT_SECONDS", "STEP_TIMEOUT_SECONDS"}
    epic = (SRC / "graphs" / "harness" / "epic.py").read_text(encoding="utf-8")
    assert "_GH_TIMEOUT_SECONDS = bounded_proc.GH_TIMEOUT_SECONDS" in epic


def test_a_move_whose_venv_sync_was_killed_is_resynced_on_the_next_run() -> None:
    """Regression: a timed-out sync left the checkout at the target, and the next run saw nothing to move."""
    assert upgrade_edge.needs_resync(upgrade_edge.UNCHANGED, 1) is True
    assert upgrade_edge.needs_resync(upgrade_edge.HELD, 1) is True
    assert upgrade_edge.needs_resync(upgrade_edge.UNCHANGED, 0) is False
    assert upgrade_edge.needs_resync(upgrade_edge.UNCHANGED, 124) is False  # a check that timed out says nothing
    assert upgrade_edge.needs_resync(upgrade_edge.MOVE, 1) is False  # the move syncs anyway


# An advisory doctor failure does not roll back.

def test_an_advisory_doctor_failure_does_not_roll_the_edge_upgrade_back() -> None:
    """Regression: the edge read the doctor's whole exit code, so a token file rolled an upgrade back."""
    failure, warning = doctor.edge_verdict(DOCTOR, 1, _doctor_output("claude token", "plugins"), [], {}, {})
    assert failure is None
    assert warning == ("warning: doctor advisory rows failed (not rolled back): "
                       "claude token: claude token detail; plugins: plugins detail")


@pytest.mark.parametrize("check", ["schema", "install bins", "workspace", "profile"])
def test_a_required_row_the_first_tier_left_out_rolls_the_upgrade_back(check: str) -> None:
    """Regression: schema (a pending migration), install bins, workspace and profile were advisory."""
    failure, _warning = doctor.edge_verdict(DOCTOR, 1, _doctor_output(check), [], {}, {})
    assert failure == f"doctor required rows failed: {check}: {check} detail"


def test_a_required_row_missing_from_the_doctor_output_fails_rather_than_passes() -> None:
    failure, _ = doctor.edge_verdict(DOCTOR, 0, _doctor_output(without=("schema", "workspace")), [], {}, {})
    assert failure == ("doctor required rows failed: workspace: missing from the doctor output; "
                       "schema: missing from the doctor output")


def test_a_unit_running_before_the_upgrade_and_down_after_it_rolls_back() -> None:
    before = {CHAIR: "active", LANDER: "inactive"}
    failure, _ = doctor.edge_verdict(DOCTOR, 0, _doctor_output(), [CHAIR, LANDER], before, {CHAIR: "failed", LANDER: "inactive"})
    assert failure == f"doctor required rows failed: units start: {CHAIR} is failed"
    assert doctor.edge_verdict(DOCTOR, 0, _doctor_output(), [CHAIR, LANDER], before, before) == (None, None)


def test_a_unit_whose_state_before_could_not_be_read_fails_and_refuses_the_move() -> None:
    assert doctor.unreadable_units([CHAIR, LANDER], {CHAIR: "active"}) == [LANDER]
    row = doctor.units_start_row([CHAIR, LANDER], {CHAIR: "active"}, {CHAIR: "active", LANDER: "active"})
    assert row == {"check": "units start", "ok": False, "detail": f"{LANDER} had no state before the upgrade"}


def test_doctor_output_with_no_rows_is_a_failure_naming_the_command() -> None:
    assert doctor.edge_verdict(DOCTOR, 124, "cox setup timed out after 1800 seconds", [], {}, {}) == (
        "cox setup doctor --json exited 124 with no rows to judge: cox setup timed out after 1800 seconds", None)
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

def test_the_macos_schedule_is_a_launchd_agent_that_sources_the_one_env_file_at_launch() -> None:
    files = upgrade_edge.schedule_files("darwin", HOME, COX, PROFILE, PATH, 30, None)
    assert [path for path, _text in files] == [HOME / "Library/LaunchAgents/coxswain-upgrade.plist"]
    assert plistlib.loads(files[0][1].encode()) == {
        "Label": "coxswain-upgrade",
        "ProgramArguments": ["/bin/sh", "-c", unit_env.SOURCE_SCRIPT, "coxswain-env", ENV_FILE,
                             COX, "upgrade", "--scheduled", "--profile", PROFILE],
        "StartInterval": 1800,
        "EnvironmentVariables": {"PATH": PATH},  # no value from the env file: it is read at launch, never copied
    }
    assert upgrade_edge.activation_lines("darwin", files[0][0]) == [
        "launchctl bootstrap gui/$(id -u) /home/user/Library/LaunchAgents/coxswain-upgrade.plist"]


def test_the_linux_schedule_is_a_bounded_oneshot_and_its_timer() -> None:
    files = upgrade_edge.schedule_files("linux", HOME, COX, PROFILE, PATH, 20, ENV_FILE)
    assert [str(path) for path, _ in files] == ["/home/user/.config/systemd/user/coxswain-upgrade.service",
                                                "/home/user/.config/systemd/user/coxswain-upgrade.timer"]
    service = files[0][1].splitlines()
    assert "TimeoutStartSec=30min" in service and f"EnvironmentFile=-{ENV_FILE}" in service
    assert "OnUnitActiveSec=20min" in files[1][1].splitlines()
