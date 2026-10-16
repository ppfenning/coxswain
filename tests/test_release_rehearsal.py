"""The rehearsal workflow matches docs/release-rehearsal.md, and its scenario script plans and judges from plain data."""

import importlib.util
import json
from pathlib import Path

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


def test_upgrade_installs_the_previous_release_populates_then_pins_the_candidate_shas():
    upgrade = next(s for s in rehearsal.plan(INPUTS) if s.name == "upgrade")
    assert _kinds(upgrade) == ["curl", "sh", "@populate", "@pin", "cox", "@doctor"]
    assert upgrade.steps[1][-2:] == ("--version", "0.40.0")
    assert upgrade.steps[3][1:] == tuple(f"{c}={SHA}" for c in ("cartridges", "graphs", "tools", "crew", "dash"))


def test_every_scenario_that_runs_pins_the_candidate_after_installing_and_before_judging():
    for s in (s for s in rehearsal.plan(INPUTS) if s.steps):
        kinds = _kinds(s)
        assert kinds.index("sh") < kinds.index("@pin") < len(kinds) - 1


def test_install_is_a_download_then_a_run_never_a_pipe():
    fresh = next(s for s in rehearsal.plan(INPUTS) if s.name == "fresh")
    assert fresh.steps[0][:2] == ("curl", "-fsSL")
    assert all("|" not in arg for step in fresh.steps for arg in step)


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


def test_component_for_url_reads_the_component_from_the_origin_url():
    assert rehearsal.component_for_url("https://github.com/ppfenning/coxswain-tools.git\n") == "tools"
    assert rehearsal.component_for_url("git@github.com:ppfenning/coxswain-dash") == "dash"
    assert rehearsal.component_for_url("https://github.com/ppfenning/coxswain") is None


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
