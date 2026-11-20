"""The edge upgrade channel's pure core: the setting, the newest green commit, the move verdict, the rollback guard,
the record `cox versions` shows, and the timer unit text."""

from __future__ import annotations

import json

import pytest

from agent_tools import forge_github, route, upgrade_edge, upgrade_plan
from agent_tools.settings_model import setting_for
from agent_tools.settings_plan import Plan, PlanError, plan_set

A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)


def test_the_channel_is_stable_unless_the_profile_says_edge():
    assert upgrade_edge.DEFAULT_CHANNEL == "stable"
    assert upgrade_edge.channel({}) == "stable"
    assert upgrade_edge.channel({"upgrade_channel": "edge"}) == "edge"


def test_the_profile_keeps_upgrade_channel_lower_case_and_refuses_any_other_value(capsys):
    assert route.parse_profile("upgrade_channel: Edge\n")["upgrade_channel"] == "edge"
    assert route.parse_profile("upgrade_channel: 'stable'\n")["upgrade_channel"] == "stable"
    assert capsys.readouterr().err == ""
    with pytest.raises(route.ProfileError, match="upgrade_channel is one of stable, edge"):
        route.parse_profile("upgrade_channel: nightly\n")


def test_cox_settings_set_writes_the_channel_and_refuses_an_unknown_one():
    setting = setting_for("profile", "upgrade_channel")
    assert setting is not None and setting.source_file == "profile.yaml"
    plan = plan_set("profile", "upgrade_channel", "edge", "team: acme\n")
    assert isinstance(plan, Plan) and plan.new_text == "team: acme\nupgrade_channel: edge\n"
    assert isinstance(plan_set("profile", "upgrade_channel", "nightly", "team: acme\n"), PlanError)


def test_ci_runs_argv_asks_gh_for_the_push_runs_of_one_workflow_on_main():
    assert forge_github.ci_runs_argv("example-org/example-repo", "ci.yml", "main") == [
        "gh", "run", "list", "--repo", "example-org/example-repo", "--workflow", "ci.yml", "--branch", "main",
        "--event", "push", "--json", "headSha,status,conclusion", "--limit", "50",
    ]


def test_green_shas_are_the_completed_successful_runs_only():
    runs = [
        {"headSha": A, "status": "in_progress", "conclusion": ""},
        {"headSha": B, "status": "completed", "conclusion": "failure"},
        {"headSha": C, "status": "completed", "conclusion": "success"},
        {"headSha": D, "status": "completed", "conclusion": "cancelled"},
    ]
    assert forge_github.parse_green_shas(json.dumps(runs)) == (C,)
    assert forge_github.parse_green_shas("not json") == ()
    assert forge_github.parse_green_shas('{"headSha": "x"}') == ()


def test_newest_green_is_the_newest_first_parent_commit_whose_ci_passed_never_the_tip_blindly():
    history = [A, B, C, D]  # newest first, as `git rev-list --first-parent origin/main` prints it
    assert upgrade_edge.newest_green(history, {C, D}) == C
    assert upgrade_edge.newest_green(history, {"e" * 40}) is None
    assert upgrade_edge.newest_green(history, set()) is None


def test_the_verdict_moves_forward_only_and_holds_a_commit_that_was_rolled_back():
    history = [A, B, C, D]
    assert upgrade_edge.decide(B, B, history, None) == upgrade_edge.UNCHANGED
    assert upgrade_edge.decide(C, B, history, None) == upgrade_edge.MOVE
    assert upgrade_edge.decide(A, B, history, None) == upgrade_edge.AHEAD
    assert upgrade_edge.decide("f" * 40, B, history, None) == upgrade_edge.MOVE  # a release commit off main's first parents
    rolled_back = upgrade_edge.record("2026-11-13T00:00:00Z", C, B, upgrade_edge.ROLLED_BACK, "doctor failed")
    assert upgrade_edge.decide(C, B, history, rolled_back) == upgrade_edge.HELD
    assert upgrade_edge.decide(C, A, history, rolled_back) == upgrade_edge.MOVE  # a newer green commit is tried


def test_added_migrations_are_the_store_ddl_modules_only_the_newer_listing_has():
    before = ["src/graphs/harness/store_ddl_0001.py", "src/graphs/harness/store_ddl_0002.py", "src/graphs/harness/cli.py"]
    after = [*before, "src/graphs/harness/store_ddl_0003.py", "src/graphs/harness/store_ddl_0004.py"]
    assert upgrade_edge.added_migrations(before, after) == ("0003", "0004")
    assert upgrade_edge.added_migrations(after, before) == ()


def test_a_rollback_is_refused_only_when_restarted_units_may_have_migrated_the_store():
    assert upgrade_edge.may_roll_back((), restarted=True)
    assert upgrade_edge.may_roll_back(("0003",), restarted=False)
    assert not upgrade_edge.may_roll_back(("0003",), restarted=True)


def test_the_record_line_names_the_outcome_the_previous_and_the_chosen_commit():
    rec = upgrade_edge.record("2026-11-13T00:00:00Z", C, B, upgrade_edge.ROLLED_BACK, "cox setup doctor exited 1")
    assert rec == {"at": "2026-11-13T00:00:00Z", "previous": C, "chosen": B, "outcome": "rolled_back",
                   "detail": "cox setup doctor exited 1"}
    line = upgrade_edge.record_line(rec)
    assert line.startswith("edge upgrade 2026-11-13T00:00:00Z: rolled_back")
    assert C[:12] in line and B[:12] in line and "cox setup doctor exited 1" in line


def test_the_service_runs_the_scheduled_upgrade_against_the_pinned_profile_and_the_timer_repeats():
    service = upgrade_edge.service_text("/opt/bin/cox", "/cfg/profile.yaml", "/opt/bin:/usr/bin", "/cfg/garage.env")
    lines = service.splitlines()
    assert "Type=oneshot" in lines
    assert "ExecStart=/opt/bin/cox upgrade --scheduled --profile /cfg/profile.yaml" in lines
    assert "Environment=PATH=/opt/bin:/usr/bin" in lines and "EnvironmentFile=-/cfg/garage.env" in lines
    assert "EnvironmentFile" not in upgrade_edge.service_text("/opt/bin/cox", "/cfg/p.yaml", "/usr/bin", None)
    timer = upgrade_edge.timer_text(30).splitlines()
    assert "OnUnitActiveSec=30min" in timer and f"Unit={upgrade_edge.SERVICE_NAME}" in timer
    assert "WantedBy=timers.target" in timer


def test_upgrade_to_takes_a_commit_as_given_and_a_version_as_its_release_tag():
    assert upgrade_plan.target_ref(B) == B
    assert upgrade_plan.target_ref("ABCDEF1") == "abcdef1"
    assert upgrade_plan.target_ref("2026.11.8") == "v2026.11.8"
    assert upgrade_plan.target_ref("v0.47.0") == "v0.47.0"
