"""The work setup's landing contract, part one: settings.

A team that lands one pull request per initiative keeps a cartridge that says `land_at: ticket` or says nothing.
This file pins what such a cartridge resolves to, so no change to the landing model can alter the work setup
without editing a test named for it. The second half of the contract (what reaches the forge) lives beside the
lander in the tools suite.
"""
from __future__ import annotations

import pytest

from core.landing import (
    TemplateError,
    initiative_branch_name,
    initiative_pr_reference,
    initiative_pr_title,
    landing_config_problems,
    landing_settings,
)

# A work-shaped cartridge as it stands today: none of the new keys.
WORK_TODAY = {"landing_areas": {"land_at": "ticket", "target_branch": "develop"}}
WORK_SILENT = {"landing_areas": {"target_branch": "develop"}}

# The same team once it adopts its branch convention: changes to develop are features, changes to main are fixes.
WORK_TEMPLATED = {
    "landing_areas": {
        "land_at": "ticket",
        "target_branch": "develop",
        "initiative_pr": {
            "branch": "{kind}/{issue}-{name}",
            "kinds": {"develop": "feature", "main": "fix"},
            "title": "{title}",
            "reference": "Closes #{issue}",
        },
    }
}
INITIATIVE = {"id": "export-reports-as-csv", "title": "Export reports as CSV", "issue": "412"}


@pytest.mark.parametrize("cartridge", [WORK_TODAY, WORK_SILENT])
def test_a_work_cartridge_with_no_new_keys_resolves_to_the_work_behaviour(cartridge: dict) -> None:
    assert landing_settings(cartridge) == {
        "land_at": "initiative",  # one pull request per initiative; phases complete locally
        "on_blocked": "hold",  # a partial pull request against a ticket is a person's call
        "push_initiative": "at_pr",  # nothing of the initiative reaches origin before its pull request
        "epic_stack": False,  # no stacked pull requests unless the team opts in
        "phase_cap": 2,
        "land_cap": 1,
    }
    assert landing_config_problems(cartridge["landing_areas"]) == []


def test_the_branch_title_and_issue_line_come_from_one_block() -> None:
    assert initiative_branch_name(WORK_TEMPLATED, INITIATIVE, "develop") == "feature/412-export-reports-as-csv"
    assert initiative_branch_name(WORK_TEMPLATED, INITIATIVE, "main") == "fix/412-export-reports-as-csv"
    assert initiative_pr_title(WORK_TEMPLATED, INITIATIVE, "develop") == "Export reports as CSV"
    assert initiative_pr_reference(WORK_TEMPLATED, INITIATIVE, "develop") == "Closes #412"
    assert landing_config_problems(WORK_TEMPLATED["landing_areas"]) == []


def test_an_initiative_names_its_own_branch_kind() -> None:
    bug = {**INITIATIVE, "branch_kind": "bug"}
    assert initiative_branch_name(WORK_TEMPLATED, bug, "main") == "bug/412-export-reports-as-csv"


def test_a_work_branch_is_never_invented_without_its_issue() -> None:
    with pytest.raises(TemplateError, match="needs issue"):
        initiative_branch_name(WORK_TEMPLATED, {"id": "export-reports-as-csv", "title": "Export"}, "develop")


def test_the_default_branch_is_init_and_the_default_issue_line_is_the_existing_rule() -> None:
    assert initiative_branch_name({}, INITIATIVE, None) == "init/export-reports-as-csv"
    assert initiative_pr_reference({}, INITIATIVE, None) == "Closes #412"
    assert initiative_pr_reference({}, {"id": "x", "title": "X"}, None, epic_issue="#77") == "Part of #77"
    assert initiative_pr_reference({}, {"id": "x", "title": "X"}, None) is None


def test_an_initiative_may_carry_its_own_branch() -> None:
    assert initiative_branch_name(WORK_TEMPLATED, {**INITIATIVE, "branch": "feature/412-csv"}, "develop") == "feature/412-csv"


def test_a_personal_cartridge_opts_into_split_and_early_push() -> None:
    personal = {"landing_areas": {"on_blocked": "split", "push_initiative": "early"}}
    resolved = landing_settings(personal)
    assert (resolved["land_at"], resolved["on_blocked"], resolved["push_initiative"]) == ("initiative", "split", "early")
    assert landing_settings(personal, {"on_blocked": "hold"})["on_blocked"] == "hold"


def test_bad_landing_keys_are_named() -> None:
    problems = landing_config_problems({
        "on_blocked": "drop", "phase_cap": 0, "epic_stack": "yes",
        "initiative_pr": {"branch": "{kind}/{ticket_key}", "kinds": {"main": "Fix It"}},
    })
    assert len(problems) == 5
    assert any("on_blocked" in p for p in problems) and any("unknown field(s) ticket_key" in p for p in problems)


def test_an_initiative_file_carries_its_issue_branch_kind_and_blocked_policy(tmp_path) -> None:
    from core import workstore

    root = tmp_path / "export-reports-as-csv"
    (root / "p1").mkdir(parents=True)
    (root / "initiative.md").write_text(
        "---\nid: export-reports-as-csv\ntitle: Export reports as CSV\nissue: 412\nbranch_kind: bug\non_blocked: hold\n---\nBody.\n"
    )
    (root / "p1" / "t1.md").write_text("---\nid: t1\ntitle: T1\nphase: p1\nstate: ready\nneeds: []\n---\nDo it.\n")
    read = workstore.read_initiative(root)
    assert (read["issue"], read["branch_kind"], read["on_blocked"]) == ("412", "bug", "hold")
    assert "ticket" not in read and "branch" not in read
    assert initiative_branch_name(WORK_TEMPLATED, read, "main") == "bug/412-export-reports-as-csv"
    (root / "initiative.md").write_text("---\nid: export-reports-as-csv\ntitle: T\nbranch: 'bad..name'\n---\n")
    with pytest.raises(workstore.WorkStoreError, match="branch must be a valid branch name"):
        workstore.read_initiative(root)
