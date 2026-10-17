"""The rehearsal workflow matches docs/release-rehearsal.md, and its scenario script plans and judges from plain data."""

import importlib.util
import json
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release-rehearsal.yml"

# docs/release-rehearsal.md section 1: the dispatch inputs.
INPUT_NAMES = ["cartridges_sha", "graphs_sha", "tools_sha", "crew_sha", "dash_sha", "previous_tag", "migrations"]
# docs/release-rehearsal.md section 3: the run title, with `none` for an empty migration list.
RUN_TITLE = (
    "rehearsal cartridges=${{ inputs.cartridges_sha }} graphs=${{ inputs.graphs_sha }}"
    " tools=${{ inputs.tools_sha }} crew=${{ inputs.crew_sha }} dash=${{ inputs.dash_sha }}"
    " previous=${{ inputs.previous_tag }} migrations=${{ inputs.migrations || 'none' }}"
)

_spec = importlib.util.spec_from_file_location("release_rehearsal", ROOT / "scripts" / "release_rehearsal.py")
rehearsal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rehearsal)

SHA = "a" * 40
INPUTS = {
    "cartridges_sha": SHA,
    "graphs_sha": SHA,
    "tools_sha": SHA,
    "crew_sha": SHA,
    "dash_sha": SHA,
    "previous_tag": "v0.40.0",
    "migrations": "split-lake-store,rename-slot-keys",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_is_dispatch_only_with_the_designed_inputs():
    triggers = _workflow()[True]
    assert list(triggers) == ["workflow_dispatch"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert list(inputs) == INPUT_NAMES
    assert [n for n, spec in inputs.items() if not spec["required"]] == ["migrations"]


def test_run_title_is_the_designed_format():
    assert " ".join(_workflow()["run-name"].split()) == RUN_TITLE


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


def _scenario(name):
    return next(s for s in rehearsal.plan(INPUTS) if s.name == name)


def _joined(scenario):
    return [" ".join(step) for step in scenario.steps]


def test_upgrade_installs_the_previous_release_populates_then_pins_the_candidate_shas():
    upgrade = _scenario("upgrade")
    assert _kinds(upgrade) == ["uv", "git", "cox", "mkdir", "git", "cartridge", "@populate", "@pin", "cox", "cox", "@doctor"]
    assert upgrade.steps[7][1:] == tuple(f"{c}={SHA}" for c in ("cartridges", "graphs", "tools", "crew", "dash"))


def test_upgrade_applies_migrations_right_after_the_pin_and_before_the_lint():
    steps = _scenario("upgrade").steps
    pinned = next(i for i, step in enumerate(steps) if step[0] == "@pin")
    assert steps[pinned + 1] == ("cox", "setup", "migrate", "--apply")
    assert steps[pinned + 2][:3] == ("cox", "route", "lint")


def _lint_step(scenario):
    return next(step for step in scenario.steps if step[:3] == ("cox", "route", "lint"))


def test_upgrade_lint_step_is_route_lint_on_the_rehearsal_initiative_directory():
    assert _lint_step(_scenario("upgrade")) == ("cox", "route", "lint", "{workspace}/work/rehearsal")


def test_upgrade_lint_directory_is_the_initiative_directory_of_the_work_items():
    phases = {PurePosixPath(rel).parent for rel in rehearsal.WORK_ITEMS}
    assert all(p.parent.parent == PurePosixPath("work") and p.name.startswith("p") for p in phases)
    assert all(PurePosixPath(rel).name.startswith("t") for rel in rehearsal.WORK_ITEMS)
    initiatives = {p.parent for p in phases}
    assert len(initiatives) == 1
    assert _lint_step(_scenario("upgrade"))[-1] == f"{{workspace}}/{initiatives.pop()}"


def test_every_scenario_that_runs_pins_the_candidate_after_installing_and_before_judging():
    for s in (s for s in rehearsal.plan(INPUTS) if s.steps):
        installed = next(i for i, step in enumerate(s.steps) if step[:2] == ("cox", "install"))
        kinds = _kinds(s)
        assert installed < kinds.index("@pin") < len(kinds) - 1


def test_fresh_plan_is_the_documented_sequence_with_no_script_download_or_version_flag():
    lines = _joined(_scenario("fresh"))
    assert not any(w in line for line in lines for w in ("curl", "install.sh", "--version"))
    assert "uv tool install --python 3.14 coxswain-tools" in lines
    assert {"--python", "3.14"} <= set(_scenario("fresh").steps[0])
    assert "git clone https://github.com/ppfenning/coxswain {root}/coxswain" in lines
    assert "cox install --team rehearse --with crew --with dash" in lines
    assert not any("--root" in line or "--workspace" in line for line in lines)
    assert "mkdir -p {workspace}/work {workspace}/runs {workspace}/intake {workspace}/cartridges" in lines
    assert "git init {workspace}" in lines
    assert "cartridge init rehearse --cartridges-dir {workspace}/cartridges" in lines


def test_fresh_plan_installs_the_candidate_tools_before_cox_install():
    fresh = _scenario("fresh")
    assert _kinds(fresh) == ["uv", "@cox", "git", "cox", "mkdir", "git", "cartridge", "@pin", "@doctor"]
    assert fresh.steps[1] == ("@cox", SHA)


def test_upgrade_and_migrate_keep_the_root_and_workspace_flags():
    for name in ("upgrade", "migrate"):
        assert "cox install --root {root} --team rehearse --workspace {workspace} --with crew --with dash" in _joined(_scenario(name))


def test_tools_install_argv_is_the_force_install_of_the_candidate_tools_sha():
    expected = f"uv tool install --force --python 3.14 git+https://github.com/ppfenning/coxswain-tools@{SHA}"
    assert " ".join(rehearsal.tools_install_argv(SHA)) == expected


def test_only_the_fresh_scenario_fills_under_the_default_coxswain_home(tmp_path):
    assert rehearsal.fill_paths("fresh", tmp_path) == {
        "{home}": str(tmp_path),
        "{root}": f"{tmp_path}/data/coxswain/installs",
        "{workspace}": f"{tmp_path}/data/coxswain/workspaces/rehearse",
    }
    for name in ("upgrade", "migrate"):
        fill = rehearsal.fill_paths(name, tmp_path)
        assert (fill["{root}"], fill["{workspace}"]) == (f"{tmp_path}/root", f"{tmp_path}/workspace")


def test_the_fresh_pin_lookup_reads_checkouts_under_the_filled_root(tmp_path):
    root = Path(rehearsal.fill_paths("fresh", tmp_path)["{root}"])
    (root / "coxswain-tools" / ".git").mkdir(parents=True)
    assert rehearsal._checkouts(root) == {"tools": root / "coxswain-tools"}


def test_upgrade_plan_pins_the_tools_package_and_clones_the_previous_tag():
    lines = _joined(_scenario("upgrade"))
    assert "uv tool install --python 3.14 coxswain-tools==0.40.0" in lines
    assert {"--python", "3.14", "coxswain-tools==0.40.0"} <= set(_scenario("upgrade").steps[0])
    assert "git clone --branch v0.40.0 https://github.com/ppfenning/coxswain {root}/coxswain" in lines
    assert not any("--version" in line for line in lines)


def test_env_points_uv_tool_bin_dir_at_the_dir_path_searches(tmp_path):
    env = rehearsal._env(tmp_path)
    assert env["UV_TOOL_BIN_DIR"] == f"{tmp_path}/.local/bin"
    assert env["PATH"].startswith(f"{tmp_path}/.local/bin:")


def test_checkouts_are_read_at_the_directory_cox_install_clones_into(tmp_path):
    for name in ("tools", "dash"):
        (tmp_path / f"coxswain-{name}" / ".git").mkdir(parents=True)
    (tmp_path / "coxswain" / ".git").mkdir(parents=True)
    assert rehearsal._checkouts(tmp_path) == {"tools": tmp_path / "coxswain-tools", "dash": tmp_path / "coxswain-dash"}


def test_refusals_name_each_input_that_is_not_a_full_sha():
    assert rehearsal.refusals(INPUTS) == []
    bad = {**INPUTS, "graphs_sha": "main", "dash_sha": SHA[:7], "crew_sha": SHA.upper()}
    assert [r.split(":")[0] for r in rehearsal.refusals(bad)] == ["graphs_sha", "crew_sha", "dash_sha"]


def test_pin_problems_is_empty_only_when_every_head_and_the_tools_receipt_hold_the_sha():
    shas = {c: str(i) * 40 for i, c in enumerate(("cartridges", "graphs", "tools", "crew", "dash"))}
    assert rehearsal.pin_problems(dict(shas), shas, f'rev = "{shas["tools"]}"') == []
    problems = rehearsal.pin_problems({**shas, "graphs": "f" * 40, "crew": None}, shas, None)
    assert problems == [
        "graphs: at ffffffffffff, expected 111111111111",
        "crew: no checkout under the install root",
        "tools: installed cox is not pinned to the candidate SHA",
    ]


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
    flags = [f"--{n.replace('_', '-')}={v}" for n, v in {**INPUTS, "graphs_sha": "main"}.items()]
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
