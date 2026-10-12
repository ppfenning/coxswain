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


def test_triggers_are_dispatch_and_call_with_three_inputs():
    triggers = _workflow()[True]
    assert "workflow_dispatch" in triggers
    assert set(triggers["workflow_call"]["inputs"]) == {"tap_ref", "formula_path", "version"}
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
