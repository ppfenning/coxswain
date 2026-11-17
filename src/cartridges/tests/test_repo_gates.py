"""The repo's own overlay leaves the full suite to CI: task checks run narrowed, and no local level or ticket gate repeats the suite."""

import tomllib
from pathlib import Path

import pytest
import yaml

from core.cartridge import overlay_errors

ROOT = Path(__file__).resolve().parents[3]
COMPONENTS = ["cartridges", "graphs", "tools"]
CLEAN_LINE = "python .github/check_clean.py . --require-names"
# The smoke suite (docs/smoke.md): real cox against real git and Postgres, run whole from the root.
SMOKE_LINE = "pytest -q -rs smoke"
# H10 item 5: a public symbol the patch adds under src/ with no reference outside tests fails the lane.
UNWIRED_LINE = "python .github/check_unwired.py"
# H2: pyright, strict on the typed core and basic elsewhere, configured in pyproject.toml's [tool.pyright].
TYPES_LINE = "python .github/check_types.py"


def overlay() -> dict:
    return yaml.safe_load((ROOT / ".agent" / "cartridge.yaml").read_text())


def test_overlay_is_accepted_and_ci_lands():
    loaded = overlay()
    assert overlay_errors(loaded) == []
    assert loaded["landing_areas"]["land_checks"] == "ci"


@pytest.mark.parametrize("key", ["level_checks", "ticket_checks"])
def test_no_local_gate_repeats_the_full_suite(key):
    # Pat, 2026-11-10: test only what a task touches, and leave the full suite to CI on the PR.
    assert key not in overlay()["landing_areas"]


def test_agent_checks_keeps_task_checks():
    lines = [
        line
        for line in (ROOT / ".agent-checks").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    pytest_lines = [f"cd src/{name} && pytest -q -n auto" for name in ("tools", "graphs", "cartridges")]
    assert lines == pytest_lines + [SMOKE_LINE, CLEAN_LINE, UNWIRED_LINE, TYPES_LINE]


def test_ci_runs_the_unwired_check_against_full_history():
    """The required test job runs the same line as the lane, and fetches the history its merge base needs."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["test"]["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    assert any(UNWIRED_LINE in str(step.get("run", "")) for step in steps)


def test_ci_runs_the_type_check_in_the_required_job():
    """The required test job runs pyright over the project's own config, as the lane's line does, with the extras synced."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    step = next(s for s in ci["jobs"]["test"]["steps"] if s.get("run") == f"uv run --all-extras {TYPES_LINE}")
    assert step["env"]["COX_TYPES_REQUIRED"] == "1"  # in CI a missing pyright or extra fails, never skips


def test_the_typed_core_is_strict():
    """H2's strict set: the core, the store modules and the chair's action contract, none of them excluded."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pyright"]
    assert config["typeCheckingMode"] == "basic"
    strict = {"src/cartridges/core", "src/graphs/harness/store*.py", "src/tools/agent_tools/chair_types.py"}
    assert strict <= set(config["strict"])
    excluded = set(config.get("exclude", []))
    assert not any(path.startswith(("src/cartridges/core", "src/graphs/harness/store")) for path in excluded)
    # the executor's dispatch is checked exhaustive over ActionKind only while its module is checked
    assert not excluded & {"src/tools/agent_tools/chair_types.py", "src/tools/agent_tools/chair_exec.py"}


# H2's ratchet: the modules not yet at pyright's basic level when the check went in. The exclude list may only lose
# entries; a module leaves it in the change that makes it pass, and this baseline loses the line with it.
EXCLUDE_BASELINE = frozenset({
    "src/graphs/graphs/delivery/decompose_order.py",
    "src/graphs/graphs/delivery/initiative_decompose.py",
    "src/graphs/graphs/delivery/lifecycle_propose.py",
    "src/graphs/graphs/delivery/phase_validate.py",
    "src/graphs/graphs/delivery/rescue_review.py",
    "src/graphs/graphs/ops/triage_propose.py",
    "src/graphs/graphs/ops/triage_quarantine.py",
    "src/graphs/harness/bounded_proc.py",
    "src/graphs/harness/check_scope.py",
    "src/graphs/harness/checks.py",
    "src/graphs/harness/cli.py",
    "src/graphs/harness/cos.py",
    "src/graphs/harness/escalate.py",
    "src/graphs/harness/landing_ops.py",
    "src/graphs/harness/landing_store.py",
    "src/graphs/harness/surface_heal.py",
    "src/graphs/harness/system_one_eval.py",
    "src/graphs/harness/ticket_claims.py",
    "src/graphs/harness/usage.py",
    "src/graphs/runner/anthropic_runner.py",
    "src/graphs/runner/claude_code_runner.py",
    "src/graphs/runner/decision_log.py",
    "src/graphs/runner/openai_compatible_runner.py",
    "src/graphs/runner/protocol.py",
    "src/graphs/runner/shadow_tier_runner.py",
    "src/graphs/runner/system_one.py",
    "src/tools/agent_tools/analytics_snapshot.py",
    "src/tools/agent_tools/bounded_run.py",
    "src/tools/agent_tools/chair.py",
    "src/tools/agent_tools/cleanup.py",
    "src/tools/agent_tools/cli.py",
    "src/tools/agent_tools/console_screen.py",
    "src/tools/agent_tools/courier.py",
    "src/tools/agent_tools/dash_detail_initiative.py",
    "src/tools/agent_tools/dash_detail_release.py",
    "src/tools/agent_tools/dash_detail_run.py",
    "src/tools/agent_tools/dash_detail_spend.py",
    "src/tools/agent_tools/dash_feed.py",
    "src/tools/agent_tools/dash_feed_tasks.py",
    "src/tools/agent_tools/dash_live_turns_session.py",
    "src/tools/agent_tools/draft_state.py",
    "src/tools/agent_tools/editor_model.py",
    "src/tools/agent_tools/forge_auto.py",
    "src/tools/agent_tools/forge_forgejo.py",
    "src/tools/agent_tools/forge_github.py",
    "src/tools/agent_tools/home_layout.py",
    "src/tools/agent_tools/home_model.py",
    "src/tools/agent_tools/lake_sync.py",
    "src/tools/agent_tools/lake_traces.py",
    "src/tools/agent_tools/land.py",
    "src/tools/agent_tools/land_batch_effects.py",
    "src/tools/agent_tools/land_batch_settings.py",
    "src/tools/agent_tools/land_limit.py",
    "src/tools/agent_tools/land_repo_lease.py",
    "src/tools/agent_tools/lane_checkout_sync.py",
    "src/tools/agent_tools/leader_chat.py",
    "src/tools/agent_tools/mcp_http.py",
    "src/tools/agent_tools/mcp_ports.py",
    "src/tools/agent_tools/mcp_protocol.py",
    "src/tools/agent_tools/operator_sessions.py",
    "src/tools/agent_tools/pacing.py",
    "src/tools/agent_tools/partial_land.py",
    "src/tools/agent_tools/queue_export.py",
    "src/tools/agent_tools/readme_commands.py",
    "src/tools/agent_tools/remote_launch.py",
    "src/tools/agent_tools/route.py",
    "src/tools/agent_tools/route_drift.py",
    "src/tools/agent_tools/route_edit.py",
    "src/tools/agent_tools/route_sync.py",
    "src/tools/agent_tools/route_sync_gh.py",
    "src/tools/agent_tools/run_logs.py",
    "src/tools/agent_tools/run_store.py",
    "src/tools/agent_tools/runs_stranded.py",
    "src/tools/agent_tools/runs_top.py",
    "src/tools/agent_tools/schema.py",
    "src/tools/agent_tools/settings_hosts.py",
    "src/tools/agent_tools/settings_housekeeping.py",
    "src/tools/agent_tools/settings_plan.py",
    "src/tools/agent_tools/setup_migrate.py",
    "src/tools/agent_tools/stale_draft.py",
    "src/tools/agent_tools/stats_chair.py",
    "src/tools/agent_tools/stats_derive.py",
    "src/tools/agent_tools/stats_gates.py",
    "src/tools/agent_tools/stats_ingest.py",
    "src/tools/agent_tools/stats_models_cmd.py",
    "src/tools/agent_tools/stats_query.py",
    "src/tools/agent_tools/stats_tiers_cmd.py",
    "src/tools/agent_tools/steward_draft.py",
    "src/tools/agent_tools/store_connect.py",
    "src/tools/agent_tools/store_dialect.py",
})


# H9 (docs/hardening.md): a module split out of an excluded one takes its place on the list, and only that: each part
# named here may be excluded because the code it holds was excluded before the move, never as a net new entry. The old
# module stays on the exclude list only while it still holds code that fails; it stays in the baseline above as the
# record of where its parts' exclusion came from, and a key that is not in the baseline allows nothing.
SPLIT_FROM: dict[str, frozenset[str]] = {
    "src/tools/agent_tools/cli.py": frozenset({
        "src/tools/agent_tools/cli_chair.py",
        "src/tools/agent_tools/cli_chair_service.py",
        "src/tools/agent_tools/cli_doctor.py",
        "src/tools/agent_tools/cli_host.py",
        "src/tools/agent_tools/cli_install.py",
        "src/tools/agent_tools/cli_leader.py",
        "src/tools/agent_tools/cli_migrate.py",
        "src/tools/agent_tools/cli_setup_store.py",
        "src/tools/agent_tools/cli_views.py",
    }),
}


def allowed_excludes(baseline: frozenset[str], split_from: dict[str, frozenset[str]]) -> frozenset[str]:
    """Pure. The baseline, plus each part of a module that is itself in the baseline; a split of any other module adds
    nothing, so a part cannot enter the list through a module that was never excluded."""
    return baseline.union(*(parts for old, parts in split_from.items() if old in baseline))


def test_allowed_excludes_admits_only_the_parts_of_a_baseline_module():
    split = {"old.py": frozenset({"old_a.py"}), "checked.py": frozenset({"checked_a.py"})}
    assert allowed_excludes(frozenset({"old.py", "other.py"}), split) == {"old.py", "other.py", "old_a.py"}
    assert allowed_excludes(frozenset(), split) == frozenset()


def test_every_split_comes_from_a_baseline_module():
    assert sorted(set(SPLIT_FROM) - EXCLUDE_BASELINE) == []


def test_the_exclude_list_only_shrinks():
    excluded = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pyright"].get("exclude", [])
    allowed = allowed_excludes(EXCLUDE_BASELINE, SPLIT_FROM)
    assert set(excluded) <= allowed, sorted(set(excluded) - allowed)
    assert [path for path in excluded if not (ROOT / path).is_file()] == []  # a moved or deleted module leaves the list
