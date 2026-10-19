"""The rehearsal workflow matches docs/release-rehearsal.md, and its scenario script plans and judges from plain data."""

import importlib.util
import json
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release-rehearsal.yml"

# docs/release-rehearsal.md section 1: the dispatch inputs.
INPUT_NAMES = ["sha", "dash_sha", "previous_tag", "migrations"]
# docs/release-rehearsal.md section 3: the run title, with `pinned` for no dash commit and `none` for no migrations.
RUN_TITLE = (
    "rehearsal sha=${{ inputs.sha }} dash=${{ inputs.dash_sha || 'pinned' }}"
    " previous=${{ inputs.previous_tag }} migrations=${{ inputs.migrations || 'none' }}"
)

_spec = importlib.util.spec_from_file_location("release_rehearsal", ROOT / "scripts" / "release_rehearsal.py")
rehearsal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rehearsal)

SHA = "a" * 40
DASH = "d" * 40
INPUTS = {"sha": SHA, "dash_sha": "", "previous_tag": "v0.47.0", "migrations": "split-lake-store,rename-slot-keys"}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_is_dispatch_only_with_the_designed_inputs():
    triggers = _workflow()[True]
    assert list(triggers) == ["workflow_dispatch"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert list(inputs) == INPUT_NAMES
    assert [n for n, spec in inputs.items() if not spec["required"]] == ["dash_sha", "migrations"]


def test_run_title_is_the_designed_format_and_the_gate_builds_the_same_string():
    assert " ".join(_workflow()["run-name"].split()) == RUN_TITLE
    from devtools.release_rehearsal_gate import run_title
    assert run_title(SHA, "pinned", "v0.47.0", []) == f"rehearsal sha={SHA} dash=pinned previous=v0.47.0 migrations=none"


def test_each_scenario_is_a_job_in_a_clean_debian_container_on_linux():
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["fresh", "upgrade", "migrate"]
    assert all(job["runs-on"].startswith("ubuntu") and job["container"].startswith("debian:") for job in jobs.values())


def test_workflow_imports_no_release_tooling():
    text = WORKFLOW.read_text()
    assert "devtools" not in text
    assert "manifest.toml" not in text


def test_plan_gives_three_scenarios_with_migrations_in_declared_order():
    scenarios = rehearsal.plan(INPUTS)
    assert [s.name for s in scenarios] == ["fresh", "upgrade", "migrate"]
    migrate = next(s for s in scenarios if s.name == "migrate")
    migrations = [step[2] for step in migrate.steps if step[:2] == ("cox", "migrate")]
    assert migrations == ["split-lake-store", "rename-slot-keys"]


def test_plan_with_no_migrations_has_a_migrate_scenario_with_no_steps():
    scenarios = rehearsal.plan({**INPUTS, "migrations": ""})
    assert [s.steps for s in scenarios if s.name == "migrate"] == [()]


def _kinds(scenario):
    return [step[0] for step in scenario.steps]


def _scenario(name, **inputs):
    return next(s for s in rehearsal.plan({**INPUTS, **inputs}) if s.name == name)


def _joined(scenario):
    return [" ".join(step) for step in scenario.steps]


def test_upgrade_installs_0_47_0_populates_moves_to_the_candidate_migrates_then_verifies():
    upgrade = _scenario("upgrade")
    assert _kinds(upgrade) == ["uv", "git", "cox", "mkdir", "git", "cartridge", "@populate", "@candidate", "cox", "cox",
                               "@verify", "@doctor"]
    assert upgrade.steps[7] == ("@candidate", SHA)
    assert upgrade.steps[8] == ("cox", "setup", "migrate", "--apply")
    assert upgrade.steps[9] == ("cox", "route", "lint", "{workspace}/work/rehearsal")
    assert upgrade.steps[10] == ("@verify", SHA)


def test_upgrade_from_0_47_0_is_the_multi_repo_coxswain_tools_install():
    lines = _joined(_scenario("upgrade"))
    assert "uv tool install --python 3.14 coxswain-tools==0.47.0" in lines
    assert "git clone --branch v0.47.0 https://github.com/ppfenning/coxswain {root}/coxswain" in lines
    assert "cox install --root {root} --team rehearse --workspace {workspace} --with crew --with dash" in lines
    assert not any("--version" in line for line in lines)


def test_upgrade_from_a_calver_release_installs_the_coxswain_distribution():
    lines = _joined(_scenario("upgrade", previous_tag="v2026.10.0"))
    assert "uv tool install --python 3.14 coxswain==2026.10.0" in lines
    assert "cox install --root {root} --team rehearse --workspace {workspace} --with dash" in lines


def test_previous_package_maps_the_semver_line_to_coxswain_tools():
    assert rehearsal.previous_package("v0.47.0") == "coxswain-tools==0.47.0"
    assert rehearsal.previous_package("v2026.11.3") == "coxswain==2026.11.3"


def test_migrate_starts_from_the_same_install_and_runs_each_migration_after_the_layout_migration():
    migrate = _scenario("migrate")
    upgrade = _scenario("upgrade")
    assert migrate.steps[:10] == upgrade.steps[:10]
    assert _kinds(migrate)[-2:] == ["@verify", "@doctor"]


def _lint_step(scenario):
    return next(step for step in scenario.steps if step[:3] == ("cox", "route", "lint"))


def test_upgrade_lint_directory_is_the_initiative_directory_of_the_work_items():
    phases = {PurePosixPath(rel).parent for rel in rehearsal.WORK_ITEMS}
    assert all(p.parent.parent == PurePosixPath("work") and p.name.startswith("p") for p in phases)
    assert all(PurePosixPath(rel).name.startswith("t") for rel in rehearsal.WORK_ITEMS)
    initiatives = {p.parent for p in phases}
    assert len(initiatives) == 1
    assert _lint_step(_scenario("upgrade"))[-1] == f"{{workspace}}/{initiatives.pop()}"


def test_fresh_clones_the_candidate_where_the_install_lives_then_runs_its_own_cox_install():
    fresh = _scenario("fresh")
    assert _kinds(fresh) == ["git", "@candidate", "cox", "mkdir", "git", "cartridge", "@verify", "@doctor"]
    lines = _joined(fresh)
    assert lines[0] == "git clone https://github.com/ppfenning/coxswain {root}/coxswain"
    assert "cox install --team rehearse --with dash" in lines
    assert not any("--root" in line or "--workspace" in line or "curl" in line for line in lines)
    assert "mkdir -p {workspace}/work {workspace}/runs {workspace}/intake {workspace}/cartridges" in lines
    assert "cartridge init rehearse --cartridges-dir {workspace}/cartridges" in lines


def test_a_dash_commit_moves_dash_after_the_candidate_and_is_verified():
    for name in ("fresh", "upgrade"):
        steps = _scenario(name, dash_sha=DASH).steps
        assert ("@dash", DASH) in steps and ("@verify", SHA, DASH) in steps
        assert steps.index(("@dash", DASH)) > _kinds(_scenario(name, dash_sha=DASH)).index("@candidate")


def test_candidate_install_argv_is_the_editable_force_install_of_the_checkout():
    assert " ".join(rehearsal.candidate_install_argv("/i/coxswain")) == (
        "uv tool install --force --python 3.14 --editable /i/coxswain")


def test_only_the_fresh_scenario_fills_under_the_default_coxswain_home(tmp_path):
    assert rehearsal.fill_paths("fresh", tmp_path) == {
        "{home}": str(tmp_path),
        "{root}": f"{tmp_path}/data/coxswain/installs",
        "{workspace}": f"{tmp_path}/data/coxswain/workspaces/main",
    }
    for name in ("upgrade", "migrate"):
        fill = rehearsal.fill_paths(name, tmp_path)
        assert (fill["{root}"], fill["{workspace}"]) == (f"{tmp_path}/root", f"{tmp_path}/workspace")


def test_env_points_uv_tool_bin_dir_at_the_dir_path_searches(tmp_path):
    env = rehearsal._env(tmp_path)
    assert env["UV_TOOL_BIN_DIR"] == f"{tmp_path}/.local/bin"
    assert env["PATH"].startswith(f"{tmp_path}/.local/bin:")


def test_refusals_name_each_bad_input():
    assert rehearsal.refusals(INPUTS) == []
    assert rehearsal.refusals({**INPUTS, "dash_sha": DASH, "previous_tag": "v2026.10.0"}) == []
    bad = {**INPUTS, "sha": "main", "dash_sha": DASH[:7], "previous_tag": "0.47.0"}
    assert [r.split(":")[0] for r in rehearsal.refusals(bad)] == ["sha", "dash_sha", "previous_tag"]


def test_verify_accepts_the_checkout_at_the_sha_with_an_editable_receipt_naming_it():
    receipt = 'requirements = [{ name = "coxswain", editable = "/i/coxswain" }]'
    assert rehearsal.verify_problems(SHA, SHA, receipt, "/i/coxswain") == []
    assert rehearsal.verify_problems(SHA, SHA, receipt, "/other/coxswain") == [
        "cox: the installed coxswain is not the candidate checkout"]


def test_verify_names_a_moved_or_missing_checkout_and_a_dash_off_its_sha():
    receipt = f'rev = "{SHA}"'
    assert rehearsal.verify_problems("f" * 40, SHA, receipt, "/i/coxswain", DASH, DASH) == [
        "coxswain: at ffffffffffff, expected aaaaaaaaaaaa"]
    assert rehearsal.verify_problems(None, SHA, None, "/i/coxswain", None, DASH) == [
        "coxswain: no checkout at /i/coxswain", "cox: the installed coxswain is not the candidate checkout",
        "dash: no checkout under the install root"]
    assert rehearsal.verify_problems(SHA, SHA, receipt, "/i/coxswain", "e" * 40, DASH) == [
        "dash: at eeeeeeeeeeee, expected dddddddddddd"]


def test_doctor_problem_accepts_passing_rows_and_rows_that_await_setup():
    rows = [{"check": "git", "ok": True}, {"check": "profile", "ok": False, "detail": "missing: profile"}]
    assert rehearsal.doctor_problem(json.dumps({"rows": rows})) is None


def test_doctor_problem_names_the_first_failed_row_and_rejects_empty_or_invalid_output():
    failed = json.dumps({"rows": [{"check": "git", "ok": True}, {"check": "uv", "ok": False, "detail": "not found"}]})
    assert rehearsal.doctor_problem(failed) == "doctor: uv failed: not found"
    assert rehearsal.doctor_problem(json.dumps({"rows": []})) == "doctor: no rows"
    assert rehearsal.doctor_problem("not json").startswith("doctor: invalid JSON")
    only_waiting = json.dumps({"rows": [{"check": "store", "ok": False, "detail": "skipped: no profile"}]})
    assert rehearsal.doctor_problem(only_waiting) == "doctor: no row passed"


def test_a_step_whose_command_is_missing_fails_the_scenario_instead_of_raising(tmp_path):
    scenario = rehearsal.Scenario("fresh", (("no-such-command-for-rehearsal-test",),))
    assert rehearsal.execute(scenario, tmp_path) == "fail"


def test_main_prints_one_fail_line_per_scenario_when_a_sha_is_refused(capsys):
    flags = [f"--{n.replace('_', '-')}={v}" for n, v in {**INPUTS, "sha": "main"}.items()]
    assert rehearsal.main(flags) == 1
    assert capsys.readouterr().out.splitlines() == ["fresh: fail", "upgrade: fail", "migrate: fail"]


def test_main_reports_skip_and_exits_zero_for_migrate_with_none_declared(capsys):
    flags = [f"--{n.replace('_', '-')}={v}" for n, v in {**INPUTS, "migrations": ""}.items()]
    assert rehearsal.main([*flags, "--scenario", "migrate"]) == 0
    assert capsys.readouterr().out == "migrate: skip\n"


def test_parse_migrations_trims_and_drops_empties():
    assert rehearsal.parse_migrations(" a, b ,,c") == ["a", "b", "c"]
    assert rehearsal.parse_migrations("") == []


def test_verdict_all_pass_exits_zero_with_one_line_each():
    results = {"fresh": "pass", "upgrade": "pass", "migrate": "pass"}
    assert rehearsal.verdict(results, ["a"]) == (0, ["fresh: pass", "upgrade: pass", "migrate: pass"])


def test_verdict_any_fail_exits_nonzero():
    code, lines = rehearsal.verdict({"fresh": "pass", "upgrade": "fail", "migrate": "skip"}, [])
    assert code == 1
    assert lines == ["fresh: pass", "upgrade: fail", "migrate: skip"]


def test_verdict_migrate_skip_with_migrations_declared_fails():
    assert rehearsal.verdict({"fresh": "pass", "upgrade": "pass", "migrate": "skip"}, ["a"])[0] == 1


def test_verdict_migrate_skip_with_none_declared_is_not_a_failure():
    assert rehearsal.verdict({"fresh": "pass", "upgrade": "pass", "migrate": "skip"}, [])[0] == 0
