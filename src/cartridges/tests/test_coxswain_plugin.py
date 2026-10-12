import calendar
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

BASH = shutil.which("bash")

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "skills-plugins" / "coxswain"
PLUGIN_JSON = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
HOOKS_JSON = PLUGIN_DIR / "hooks" / "hooks.json"
DOCKET_SH = PLUGIN_DIR / "hooks" / "docket.sh"
STATUSLINE_SH = PLUGIN_DIR / "statusline" / "statusline.sh"
SUMMARIZE_PY = PLUGIN_DIR / "statusline" / "summarize.py"
SETTINGS_JSON = PLUGIN_DIR / "settings.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
COMMANDS_DIR = PLUGIN_DIR / "commands"
COMMAND_NAMES = ("launch", "land", "runs", "intake", "regatta")


def _load_summarize():
    spec = importlib.util.spec_from_file_location("coxswain_summarize", SUMMARIZE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SUMMARIZE = _load_summarize()


def _is_executable(path):
    return bool(os.stat(path).st_mode & stat.S_IXUSR)


def _tracked_as_executable(path):
    # A checkout tool that doesn't preserve the filesystem executable bit
    # (some sandboxes, some archive-based deploys) still leaves the mode
    # git actually committed readable from the index itself.
    result = subprocess.run(
        ["git", "ls-files", "-s", str(path.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.split()
    return bool(fields) and fields[0] == "100755"


def _command_frontmatter_and_body(name):
    path = COMMANDS_DIR / f"{name}.md"
    text = path.read_text()
    assert text.startswith("---\n"), f"{name}.md must open with a --- frontmatter fence"
    _, rest = text.split("---\n", 1)
    front, body = rest.split("---\n", 1)
    return yaml.safe_load(front), body.strip()


def _run_via_shebang(script, **kwargs):
    # Invokes the script by its own path, so the OS picks the interpreter
    # from the shebang and refuses outright if the executable bit is off.
    try:
        return subprocess.run([str(script)], **kwargs)
    except PermissionError:
        pytest.skip(
            f"{script.name} is not chmod +x on disk in this sandbox; "
            "git tracks it as 100755, see "
            "test_docket_and_statusline_are_executable_with_a_shebang"
        )


def test_plugin_json_parses_with_the_coxswain_name():
    data = json.loads(PLUGIN_JSON.read_text())
    assert data["name"] == "coxswain"


def test_hooks_json_parses_and_the_session_start_command_names_docket():
    data = json.loads(HOOKS_JSON.read_text())
    session_start = data["hooks"]["SessionStart"]
    commands = [
        hook["command"] for entry in session_start for hook in entry["hooks"]
    ]
    assert any("docket.sh" in command for command in commands)


def test_settings_json_wires_the_statusline_script():
    data = json.loads(SETTINGS_JSON.read_text())
    assert data["statusLine"]["command"].endswith("statusline/statusline.sh")


def test_docket_and_statusline_are_executable_with_a_shebang():
    for script in (DOCKET_SH, STATUSLINE_SH):
        assert _is_executable(script) or _tracked_as_executable(script)
        first_line = script.read_text().splitlines()[0]
        assert first_line.startswith("#!")


def test_docket_with_no_profile_prints_nothing_and_exits_clean(tmp_path):
    missing_profile = tmp_path / "profile.yaml"
    result = subprocess.run(
        [BASH, str(DOCKET_SH)],
        env={**os.environ, "AGENT_TOOLS_PROFILE": str(missing_profile)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_docket_invoked_by_its_own_shebang_also_prints_nothing(tmp_path):
    missing_profile = tmp_path / "profile.yaml"
    result = _run_via_shebang(
        DOCKET_SH,
        env={**os.environ, "AGENT_TOOLS_PROFILE": str(missing_profile)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_docket_prints_the_leader_line_alongside_context_when_profile_exists(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("team: local\n")
    fake_cox = tmp_path / "cox"
    fake_cox.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  "route context") echo "context: local" ;;\n'
        '  "route leader status") echo "leader: none" ;;\n'
        "esac\n"
    )
    fake_cox.chmod(0o755)
    result = subprocess.run(
        [BASH, str(DOCKET_SH)],
        env={
            **os.environ,
            "AGENT_TOOLS_PROFILE": str(profile),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "context: local" in result.stdout
    assert "leader: none" in result.stdout


def test_docket_tolerates_a_profile_with_neither_cox_nor_agent_tools_on_path(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text("team: local\n")
    result = subprocess.run(
        [BASH, str(DOCKET_SH)],
        env={"AGENT_TOOLS_PROFILE": str(profile), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_statusline_with_no_cox_on_path_prints_just_the_model():
    result = subprocess.run(
        [BASH, str(STATUSLINE_SH)],
        input='{"model":{"display_name":"Opus"}}',
        env={"PATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "Opus"


def test_statusline_invoked_by_its_own_shebang_also_prints_just_the_model():
    result = _run_via_shebang(
        STATUSLINE_SH,
        input='{"model":{"display_name":"Opus"}}',
        env={"PATH": ""},
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "Opus"


def test_marketplace_lists_both_plugins_at_real_paths_with_matching_names():
    data = json.loads(MARKETPLACE_JSON.read_text())
    by_source = {plugin["source"]: plugin for plugin in data["plugins"]}
    for source in ("./skills-plugins/local-skills", "./skills-plugins/coxswain"):
        assert source in by_source
        assert (ROOT / source).resolve().is_dir()
    own_plugin = json.loads(PLUGIN_JSON.read_text())
    assert by_source["./skills-plugins/coxswain"]["name"] == own_plugin["name"]


def test_summarize_counts_running_and_reports_the_slot_cap():
    runs = [{"state": "running"}, {"state": "running"}, {"state": "done"}]
    summary = SUMMARIZE.summarize(runs, slots=3, now=0)
    assert summary.running == 2
    assert summary.slots == 3


def test_summarize_omits_spend_when_no_run_has_reported_cost():
    summary = SUMMARIZE.summarize([{"state": "running"}], slots=3, now=0)
    assert summary.spend is None


def test_summarize_sums_spend_for_runs_inside_the_five_hour_window():
    now = calendar.timegm(time.strptime("2024-01-01T12:00:00", "%Y-%m-%dT%H:%M:%S"))
    runs = [
        {"state": "done", "usage": {"cost_usd": 1.5}, "started_at": "2024-01-01T10:00:00"},
        {"state": "done", "usage": {"cost_usd": 2.0}, "started_at": "2024-01-01T05:00:00"},
    ]
    summary = SUMMARIZE.summarize(runs, slots=3, now=now)
    assert summary.spend == pytest.approx(1.5)


def test_summarize_excludes_rather_than_guesses_at_an_unparsable_timestamp():
    runs = [{"state": "done", "usage": {"cost_usd": 9.0}, "started_at": "not-a-timestamp"}]
    summary = SUMMARIZE.summarize(runs, slots=3, now=0)
    assert summary.spend is None
    assert summary.unparsed == ("not-a-timestamp",)


@pytest.mark.parametrize("name", COMMAND_NAMES)
def test_each_command_file_exists_with_frontmatter_and_a_body(name):
    front, body = _command_frontmatter_and_body(name)
    assert isinstance(front, dict)
    assert "description" in front
    assert "argument-hint" in front
    assert body


@pytest.mark.parametrize("name", ("launch", "intake"))
def test_launch_and_intake_bodies_carry_the_arguments_placeholder(name):
    _, body = _command_frontmatter_and_body(name)
    assert "$ARGUMENTS" in body


@pytest.mark.parametrize("name", COMMAND_NAMES)
def test_each_command_falls_back_to_agent_tools_when_cox_is_absent(name):
    _, body = _command_frontmatter_and_body(name)
    assert "agent-tools" in body


def test_launch_names_the_decompose_step_before_launching_an_epic_at_threshold():
    body = " ".join(_command_frontmatter_and_body("launch")[1].split())
    assert "route launch decompose" in body
    assert "never launch the epic over an unscoped intake item" in body


def test_land_waits_for_the_operator_to_say_apply_rather_than_deciding_itself():
    body = " ".join(_command_frontmatter_and_body("land")[1].split())
    assert "if the operator explicitly says to apply" in body.lower()
    assert "never decide to apply it on your own reading of the plan" in body.lower()


def test_runs_does_not_invent_a_runs_directory():
    body = " ".join(_command_frontmatter_and_body("runs")[1].split())
    assert "ask rather than guess at one" in body


def test_runs_checks_leader_status_but_still_shows_the_table_read_only():
    body = " ".join(_command_frontmatter_and_body("runs")[1].split())
    assert "do not re-arm" in body
    assert "errors or is not yet installed" in body
    leader_at = body.index("cox route leader status")
    status_at = body.index("cox route status")
    assert leader_at < status_at
    assert "show the table anyway" in body
