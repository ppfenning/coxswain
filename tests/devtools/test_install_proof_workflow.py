"""install-proof.yml: its structure, and its step scripts run against stub brew, git and cox."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "install-proof.yml"
BASH = shutil.which("bash")
PREVIOUS, UPGRADE, CLEAN = "Install previous release", "Stage candidate", "Upgrade, then clean install"

BREW = """#!/bin/bash
echo "brew $*" >> "$STUB_DIR/calls"
case "$1" in
  list) echo "cox $(cat "$STUB_DIR/installed")" ;;
  install) if [ -e "$STUB_DIR/uninstalled" ]; then cp "$STUB_DIR/candidate" "$STUB_DIR/installed"; else cp "$STUB_DIR/previous" "$STUB_DIR/installed"; fi ;;
  upgrade) if [ "$STUB_UPGRADE" = 1 ]; then cp "$STUB_DIR/candidate" "$STUB_DIR/installed"; fi ;;
  uninstall) rm "$STUB_DIR/installed"; touch "$STUB_DIR/uninstalled" ;;
  info) echo "{\\"formulae\\": [{\\"versions\\": {\\"stable\\": \\"$(cat "$STUB_DIR/candidate")\\"}}]}" ;;
  --repository) echo "$STUB_DIR/tap" ;;
esac
"""
GIT = """#!/bin/bash
echo "git $*" >> "$STUB_DIR/calls"
case " $* " in *" ls-remote "*) for t in $STUB_TAGS; do printf 'deadbeef\\trefs/tags/%s\\n' "$t"; done ;; esac
"""
COX = """#!/bin/bash
echo "cox $*" >> "$STUB_DIR/calls"
case "$1" in
  --version) echo "cox $(cat "$STUB_DIR/installed")" ;;
  setup) echo '{"ok": false, "rows": [{"check": "git", "ok": true, "detail": "git version 2.56.0"}, {"check": "profile", "ok": false, "detail": "missing: x/profile.yaml"}, {"check": "store", "ok": false, "detail": "skipped: no profile"}]}'; exit 1 ;;
esac
"""


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _script(job: str, name: str) -> str:
    return next(step["run"] for step in _workflow()["jobs"][job]["steps"] if step["name"] == name)


class Rig:
    def __init__(self, root: Path):
        self.state, self.bin = root / "state", root / "bin"
        (self.state / "tap" / "Formula").mkdir(parents=True)
        self.bin.mkdir()
        for name, body in (("brew", BREW), ("git", GIT), ("cox", COX)):
            self.add_tool(name, body)
        (self.bin / "python3").symlink_to(sys.executable)

    def add_tool(self, name: str, body: str) -> None:
        (self.bin / name).write_text(body)
        (self.bin / name).chmod(0o755)

    def write_formula(self, text: str) -> None:
        (self.state / "tap" / "Formula" / "cox.rb").write_text(text)

    def seed(self, previous: str, candidate: str) -> None:
        (self.state / "previous").write_text(previous)
        (self.state / "candidate").write_text(candidate)
        (self.state / "installed").write_text(previous)

    def run(self, step: str, **env: str) -> subprocess.CompletedProcess:
        full = {"PATH": f"{self.bin}:/usr/bin:/bin", "STUB_DIR": str(self.state), "SRC": str(ROOT), "HOME": str(self.state)}
        full |= {"VERSION": "1.2.0", "TAP_REF": "", "FORMULA_PATH": "", "STUB_TAGS": "", "STUB_UPGRADE": "1"} | env
        return subprocess.run([BASH, "-eo", "pipefail", "-c", _script("proof-macos", step)], env=full, capture_output=True, text=True)

    def calls(self) -> list[str]:
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_both_jobs_run_on_arm64_macos_and_the_homebrew_container():
    jobs = _workflow()["jobs"]
    assert jobs["proof-macos"]["runs-on"] == "macos-14"
    assert jobs["proof-linux"]["runs-on"] == "ubuntu-latest"
    assert jobs["proof-linux"]["container"] == "homebrew/brew"
    assert jobs["proof-macos"]["steps"] == jobs["proof-linux"]["steps"]


def test_fetch_sources_provides_python3_for_the_checker_only_when_it_is_missing():
    script = _script("proof-linux", "Fetch sources")
    assert "if ! command -v python3 >/dev/null; then" in script
    assert "\n  sudo apt-get update -qq\n" in script  # not inside an || list, so -e fails the step if apt fails
    assert "apt-get install -y -qq --no-install-recommends python3" in script


def test_triggers_are_dispatch_and_call_with_four_inputs():
    triggers = _workflow()[True]
    assert "workflow_dispatch" in triggers
    assert set(triggers["workflow_call"]["inputs"]) == {"tap_ref", "formula_path", "version", "expect_failure"}
    assert triggers["workflow_call"]["inputs"]["version"]["required"] is True


def test_workflow_is_read_only_and_uses_no_action():
    workflow = _workflow()
    text = WORKFLOW.read_text()
    assert workflow["permissions"] == {"contents": "read"}
    assert not any("uses" in step for job in workflow["jobs"].values() for step in job["steps"])
    assert not re.search(r"git push|git tag|\bgh \w+|secrets\.", text)


def test_the_formula_installs_cox_only_so_coxtop_is_skipped_absent_and_towpath_follows_the_formula():
    text = WORKFLOW.read_text()
    assert not re.search(r"brew (install|upgrade)\S*[^\n]*(coxtop|towpath)", text)
    assert '! command -v coxtop >/dev/null || coxtop --version' in text
    assert "for b in towpath coxtop" not in text


WITH_TOWPATH = 'class Cox < Formula\n  resource "towpath" do\n    url "u"\n  end\nend\n'
WITHOUT_TOWPATH = 'class Cox < Formula\n  resource "pyyaml" do\n    url "u"\n  end\nend\n'


@pytest.mark.parametrize("job", ["proof-macos", "proof-linux"])
def test_towpath_assertion_is_guarded_by_the_formula_declaring_a_towpath_resource(job):
    script = _script(job, CLEAN)
    guard = script.index('grep -Eq \'^[[:space:]]*resource "towpath" do\' "$formula"')
    assert guard < script.index("command -v towpath") < script.index("towpath --version") < script.index("fi\n")
    assert 'formula="$(brew --repository ppfenning/coxswain)/Formula/cox.rb"' in script


def test_a_formula_with_a_towpath_resource_fails_when_towpath_is_not_on_path(rig):
    rig.seed("1.1.0", "1.2.0")
    rig.write_formula(WITH_TOWPATH)
    done = rig.run(CLEAN)
    assert done.returncode != 0
    assert "towpath is not on PATH" in done.stdout


def test_a_formula_with_a_towpath_resource_passes_when_towpath_is_on_path(rig):
    rig.seed("1.1.0", "1.2.0")
    rig.write_formula(WITH_TOWPATH)
    rig.add_tool("towpath", "#!/bin/bash\necho towpath 1.2.0\n")
    assert rig.run(CLEAN).returncode == 0


def test_a_formula_without_a_towpath_resource_skips_the_towpath_assertion(rig):
    rig.seed("1.1.0", "1.2.0")
    rig.write_formula(WITHOUT_TOWPATH)
    done = rig.run(CLEAN)
    assert done.returncode == 0, done.stdout
    assert "towpath" not in done.stdout


def test_allow_installed_candidate_is_a_dispatch_boolean_defaulting_to_false_and_reaches_both_jobs():
    workflow = _workflow()
    spec = workflow[True]["workflow_dispatch"]["inputs"]["allow_installed_candidate"]
    assert spec["type"] == "boolean"
    assert spec["default"] is False
    assert workflow["env"]["ALLOW_INSTALLED_CANDIDATE"] == "${{ inputs.allow_installed_candidate }}"
    assert all("ALLOW_INSTALLED_CANDIDATE" in _script(job, PREVIOUS) for job in ("proof-macos", "proof-linux"))


TAGS = "v1.1.0 v1.2.0"


def test_previous_release_assertion_fails_when_the_candidate_is_already_installed_and_the_flag_is_off(rig):
    rig.seed("1.2.0", "1.2.0")
    for flag in ({}, {"ALLOW_INSTALLED_CANDIDATE": "false"}):
        done = rig.run(PREVIOUS, STUB_TAGS=TAGS, **flag)
        assert done.returncode != 0
        assert "installed 'cox 1.2.0', expected 'cox 1.1.0'" in done.stdout


def test_previous_release_assertion_accepts_the_installed_candidate_when_the_flag_is_on(rig):
    rig.seed("1.2.0", "1.2.0")
    assert rig.run(PREVIOUS, STUB_TAGS=TAGS, ALLOW_INSTALLED_CANDIDATE="true").returncode == 0


def test_the_flag_does_not_excuse_a_version_that_is_neither_previous_nor_candidate(rig):
    rig.seed("1.0.5", "1.2.0")
    assert rig.run(PREVIOUS, STUB_TAGS=TAGS, ALLOW_INSTALLED_CANDIDATE="true").returncode != 0


def test_upgrade_that_changes_nothing_fails_before_any_cox_call(rig):
    rig.seed("1.1.0", "1.2.0")
    done = rig.run(CLEAN, STUB_UPGRADE="0")
    assert done.returncode != 0
    assert "changed nothing" in done.stdout
    assert not any(call.startswith("cox ") for call in rig.calls())


def test_upgrade_landing_on_another_version_fails_before_any_cox_call(rig):
    rig.seed("1.1.0", "1.3.0")
    done = rig.run(CLEAN)
    assert done.returncode != 0
    assert "expected 'cox 1.2.0'" in done.stdout
    assert not any(call.startswith("cox ") for call in rig.calls())


@pytest.mark.parametrize("candidate", ["1.2.0", "1.1.0"])
def test_staged_version_not_above_installed_fails_before_upgrading(rig, candidate):
    rig.seed("1.2.0", candidate)
    done = rig.run(UPGRADE)
    assert done.returncode != 0
    assert f"staged {candidate} is not above installed 1.2.0" in done.stdout
    assert "brew upgrade cox" not in rig.calls()


def test_empty_tag_list_for_a_real_version_fails_rather_than_skips(rig):
    rig.seed("1.1.0", "1.2.0")
    done = rig.run(PREVIOUS, STUB_TAGS="")
    assert done.returncode != 0
    assert "no release tag below 1.2.0" in done.stdout


def test_installed_version_other_than_the_previous_tag_fails(rig):
    rig.seed("1.1.0", "1.2.0")
    done = rig.run(PREVIOUS, STUB_TAGS="v1.0.0 v1.1.0 v1.2.0 v1.3.0")
    assert done.returncode == 0
    rig.seed("1.0.5", "1.2.0")
    assert rig.run(PREVIOUS, STUB_TAGS="v1.0.0 v1.1.0 v1.2.0").returncode != 0


def test_non_release_version_skips_the_starting_version_assertion(rig):
    rig.seed("1.1.0", "999.0.0-negative-control")
    done = rig.run(PREVIOUS, VERSION="999.0.0-negative-control", STUB_TAGS="")
    assert done.returncode == 0
    assert "skip previous-version assertion" in done.stdout


def test_a_release_runs_install_stage_upgrade_and_clean_install_in_order(rig):
    rig.seed("1.1.0", "1.2.0")
    tags = "v1.0.0 v1.1.0 v1.2.0"
    assert rig.run(PREVIOUS, STUB_TAGS=tags).returncode == 0
    assert rig.run(UPGRADE).returncode == 0
    assert rig.run(CLEAN).returncode == 0
    mutations = [call for call in rig.calls() if call.startswith(("brew install", "brew upgrade", "brew uninstall"))]
    assert mutations == [
        "brew install ppfenning/coxswain/cox",
        "brew upgrade cox",
        "brew uninstall cox",
        "brew install ppfenning/coxswain/cox",
    ]
    assert rig.calls().count("cox --version") == 2


def test_a_coxtop_on_path_at_another_version_fails_the_checks(rig):
    rig.seed("1.1.0", "1.2.0")
    rig.add_tool("coxtop", "#!/bin/bash\necho coxtop 1.1.0\n")
    done = rig.run(CLEAN)
    assert done.returncode != 0
    assert "version: expected '1.2.0'" in done.stderr


def test_a_towpath_on_path_at_the_candidate_version_passes(rig):
    rig.seed("1.1.0", "1.2.0")
    rig.add_tool("towpath", "#!/bin/bash\necho towpath 1.2.0\n")
    done = rig.run(CLEAN)
    assert done.returncode == 0, done.stdout


def test_staging_copies_the_formula_path_over_the_tap_and_checks_out_the_tap_ref(rig):
    rig.seed("1.1.0", "1.2.0")
    done = rig.run(UPGRADE, FORMULA_PATH="tests/devtools/broken_cox.rb", TAP_REF="topic")
    assert done.returncode == 0
    assert (rig.state / "tap" / "Formula" / "cox.rb").read_text() == (ROOT / "tests/devtools/broken_cox.rb").read_text()
    assert f"git -C {rig.state}/tap checkout -q FETCH_HEAD" in rig.calls()


NEGATIVE = ROOT / ".github" / "workflows" / "install-proof-negative.yml"
EXPECTED_CONTINUE = "${{ inputs.expect_failure == true }}"
PLATFORM_OUTPUTS = {"proof-macos": "macos_outcome", "proof-linux": "linux_outcome"}


def _negative() -> dict:
    return yaml.safe_load(NEGATIVE.read_text())


def _clean_step(job: str) -> dict:
    return next(step for step in _workflow()["jobs"][job]["steps"] if step["name"] == CLEAN)


def test_expect_failure_is_a_boolean_input_defaulting_to_false_on_workflow_call_only():
    triggers = _workflow()[True]
    assert triggers["workflow_call"]["inputs"]["expect_failure"]["type"] == "boolean"
    assert triggers["workflow_call"]["inputs"]["expect_failure"]["default"] is False
    assert "expect_failure" not in triggers["workflow_dispatch"]["inputs"]


@pytest.mark.parametrize("job", PLATFORM_OUTPUTS)
def test_the_clean_install_step_has_an_id_and_the_exact_continue_on_error_expression(job):
    step = _clean_step(job)
    assert step["id"]
    assert step["continue-on-error"] == EXPECTED_CONTINUE


@pytest.mark.parametrize("job", PLATFORM_OUTPUTS)
def test_each_platform_job_exports_the_clean_install_outcome_as_a_job_output(job):
    outputs = _workflow()["jobs"][job]["outputs"]
    assert outputs == {PLATFORM_OUTPUTS[job]: "${{ steps." + _clean_step(job)["id"] + ".outcome }}"}


def test_workflow_call_outputs_expose_both_platform_outputs_under_distinct_names():
    outputs = _workflow()[True]["workflow_call"]["outputs"]
    assert {name: out["value"] for name, out in outputs.items()} == {
        name: "${{ jobs." + job + ".outputs." + name + " }}" for job, name in PLATFORM_OUTPUTS.items()
    }


def test_continue_on_error_reads_the_input_alone_so_false_keeps_a_failed_install_failing():
    expression = _clean_step("proof-macos")["continue-on-error"]
    refs = re.findall(r"[A-Za-z_][\w.]*", expression.removeprefix("${{").removesuffix("}}"))
    assert refs == ["inputs.expect_failure", "true"]
    assert _clean_step("proof-linux")["continue-on-error"] == expression


def test_the_negative_workflow_passes_expect_failure_true():
    assert _negative()["jobs"]["proof"]["with"]["expect_failure"] is True


def test_the_expect_failure_job_reads_both_platform_outputs_from_the_proof_call():
    steps = _negative()["jobs"]["expect-failure"]["steps"]
    env = {k: v for step in steps for k, v in step.get("env", {}).items()}
    assert env == {
        "MACOS_OUTCOME": "${{ needs.proof.outputs.macos_outcome }}",
        "LINUX_OUTCOME": "${{ needs.proof.outputs.linux_outcome }}",
        "MACOS_FAILED_STEP": "${{ needs.failed-step.outputs.macos_failed_step }}",
        "LINUX_FAILED_STEP": "${{ needs.failed-step.outputs.linux_failed_step }}",
    }


def _verdict(macos: str, linux: str, macos_step: str = "check-version", linux_step: str = "check-version") -> int:
    job = _negative()["jobs"]["expect-failure"]
    env = {"PATH": "/usr/bin:/bin", "MACOS_OUTCOME": macos, "LINUX_OUTCOME": linux,
           "MACOS_FAILED_STEP": macos_step, "LINUX_FAILED_STEP": linux_step}
    script = "\n".join(step["run"] for step in job["steps"])
    return subprocess.run([BASH, "-eo", "pipefail", "-c", script], env=env, capture_output=True, text=True).returncode


@pytest.mark.parametrize(
    ("macos", "linux", "passes"),
    [
        ("failure", "failure", True),
        ("success", "failure", False),
        ("failure", "success", False),
        ("failure", "skipped", False),
        ("cancelled", "failure", False),
        ("failure", "", False),
        ("", "", False),
    ],
)
def test_the_expect_failure_job_succeeds_only_when_both_outcomes_are_failure(macos, linux, passes):
    assert (_verdict(macos, linux) == 0) is passes


# The negative control proves the checks run: the broken formula is the real previous-release sdist declared as
# 999.0.0, so the upgrade succeeds and the proof must reject it at check-version, not anywhere earlier.

BROKEN = ROOT / "tests" / "devtools" / "broken_cox.rb"
CHECK_VERSION = "check-version"


def test_the_broken_formula_declares_999_and_points_at_no_example_invalid_url():
    text = BROKEN.read_text()
    assert re.search(r'^  version "999\.0\.0"$', text, re.MULTILINE)
    assert "example.invalid" not in text
    assert _negative()["jobs"]["proof"]["with"]["version"] == "999.0.0"


def test_the_failed_step_job_exposes_each_platforms_failed_step_as_a_job_output():
    job = _negative()["jobs"]["failed-step"]
    assert job["needs"] == "proof"
    assert "always()" in job["if"]
    step_id = job["steps"][0]["id"]
    assert job["outputs"] == {
        f"{platform}_failed_step": "${{ steps." + step_id + ".outputs." + platform + "_failed_step }}"
        for platform in ("macos", "linux")
    }


def test_expect_failure_needs_the_failed_step_job_and_compares_it_to_check_version():
    job = _negative()["jobs"]["expect-failure"]
    assert job["needs"] == ["proof", "failed-step"]
    script = "\n".join(step["run"] for step in job["steps"])
    assert f'test "${{MACOS_FAILED_STEP}}" = "{CHECK_VERSION}"' in script
    assert f'test "${{LINUX_FAILED_STEP}}" = "{CHECK_VERSION}"' in script


@pytest.mark.parametrize(
    ("macos_step", "linux_step", "passes"),
    [
        ("check-version", "check-version", True),
        ("other", "check-version", False),
        ("check-version", "other", False),
        ("missing", "check-version", False),
        ("", "", False),
    ],
)
def test_a_failure_outside_check_version_fails_the_expect_failure_step(macos_step, linux_step, passes):
    assert (_verdict("failure", "failure", macos_step, linux_step) == 0) is passes


GH = """#!/bin/bash
case "$2" in
  */jobs) printf '%s\\t%s\\n' 11 "proof / proof-macos" 12 "proof / proof-linux" 13 "failed-step" ;;
  *) exit 1 ;;
esac
"""
CURL = """#!/bin/bash
case "${@: -1}" in
  https://api.test/repos/o/r/actions/jobs/11/logs) cat "$STUB_DIR/macos.log" ;;
  https://api.test/repos/o/r/actions/jobs/12/logs) cat "$STUB_DIR/linux.log" ;;
  *) exit 22 ;;
esac
"""
VERSION_MISS = "2026-10-12T17:00:00.0Z version: expected '999.0.0', first output line was 'cox 0.38.0'\n"


def _failed_steps(tmp_path: Path, macos_log: str, linux_log: str) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", GH), ("curl", CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "macos.log").write_text(macos_log)
    (tmp_path / "linux.log").write_text(linux_log)
    out = tmp_path / "output"
    out.touch()
    step = _negative()["jobs"]["failed-step"]["steps"][0]
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_DIR": str(tmp_path), "GITHUB_OUTPUT": str(out),
           "RUNNER_TEMP": str(tmp_path), "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "1",
           "GITHUB_RUN_ATTEMPT": "1", "GITHUB_API_URL": "https://api.test", "GH_TOKEN": "t", "EXPECTED": step["env"]["EXPECTED"]}
    done = subprocess.run([BASH, "-eo", "pipefail", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_a_version_miss_on_a_real_release_names_check_version(tmp_path):
    steps = _failed_steps(tmp_path, VERSION_MISS, VERSION_MISS)
    assert steps == {"macos_failed_step": CHECK_VERSION, "linux_failed_step": CHECK_VERSION}


@pytest.mark.parametrize(
    "log",
    [
        "Error: cox: SHA256 mismatch\n",  # the upgrade never ran: the old all-zero digest
        "after upgrade 'cox 0.38.0', expected 'cox 999.0.0'\n",
        "version: expected '999.0.0', first output line was ''\n",  # cox did not run: a bad HOME override
        "doctor: git failed: missing\n",
    ],
)
def test_any_other_failure_is_not_named_check_version(tmp_path, log):
    steps = _failed_steps(tmp_path, log, VERSION_MISS)
    assert steps == {"macos_failed_step": "other", "linux_failed_step": CHECK_VERSION}
