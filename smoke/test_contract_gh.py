"""Contract: coxswain -> `gh` -> GitHub's API, with the real `gh` binary and answers recorded from api.github.com.

Each test runs coxswain's own code (an entry point, or the installed module the entry point calls, in process) with
the real `gh`. `gh` is pointed at `smoke/gh_replay.py` through its own mechanisms (`GH_HOST=github.localhost` and
`HTTP_PROXY`), which answers from `smoke/gh_recordings/<test>.json`. Nothing in coxswain or `gh` is replaced. A
request with no recorded answer fails the test, so a `gh` that asks something new, or a coxswain that runs a new `gh`
command, shows here. A recorded answer is what GitHub said when it was recorded: run with `COX_SMOKE_GH_RECORD=1` and
`COX_SMOKE_GH_TOKEN` set and the same tests run against api.github.com, read only (the recorder refuses every write
but the deletion of a branch it first saw is gone), and rewrite their recordings. docs/smoke.md says why this and
not a sandbox repository.

The subject is ppfenning/coxswain itself, which is public: pull request #1061 (merged, carrying one task), its head
commit, and the push runs of ci.yml on main.
"""

from __future__ import annotations

from smoke import plan

SLUG = plan.GH_SLUG
MERGED_PR = 1061
MERGED_TASK = "chair-breaks-a-steer-deadlock-between-facts-approval-holds"
MERGED_HEAD = "20029843c19b460ce224eacd337fc97d7e6b255a"
MERGED_URL = f"https://github.com/{SLUG}/pull/{MERGED_PR}"


def _in_clone(github, code: str):
    """Run `code` under the install's interpreter from the clone, where `gh` finds the repository by its origin."""
    return github.sandbox.python_json(code, cwd=github.clone)


def test_pr_view_reads_every_field_a_land_needs(github):
    pr = _in_clone(github, f"""
import json
from pathlib import Path
from agent_tools import forge_github, pr_land
pr = forge_github.read_pr(Path("."), {MERGED_PR})
print(json.dumps(pr and {{**pr, "body": None, "tasks": list(pr_land.carried_tasks(pr["body"]))}}))
""")
    assert pr == {"number": MERGED_PR, "state": "MERGED", "url": MERGED_URL, "headRefOid": MERGED_HEAD,
                  "headRefName": f"pr/{MERGED_TASK}", "baseRefName": "main", "body": None, "tasks": [MERGED_TASK]}


def test_cox_pr_land_of_a_merged_pr_marks_the_task_it_carries_done(github):
    sandbox = github.sandbox
    sandbox.write(f"work/ship/p1/{MERGED_TASK}.md",
                  f"---\nid: {MERGED_TASK}\ntitle: T\nstate: approved\nphase: p1\nrepo: app\n---\n\nBody.\n")
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    landed = sandbox.cox("pr", "land", str(MERGED_PR), "--repo", str(github.clone))
    assert landed.returncode == 0, landed.stdout + landed.stderr
    assert landed.stdout.strip() == f"level_merged: merged {MERGED_URL}; marked 1 task(s) done"
    row = sandbox.store_row("ship", MERGED_TASK)
    assert row["state"] == "done", row


def test_check_rows_of_a_commit_read_its_check_runs_and_statuses(github):
    rows = _in_clone(github, f"""
import json
from pathlib import Path
from agent_tools import forge_github
rows = forge_github.pr_check_rows(Path("."), "{MERGED_HEAD}")
print(json.dumps(None if rows is None else sorted({{(r["name"], r["state"]) for r in rows}})))
""")
    assert rows is not None and rows, rows
    assert ["test", "success"] in rows, rows


def test_required_checks_of_main_are_read(github):
    required = _in_clone(github, """
import json
from pathlib import Path
from agent_tools import forge_github
required = forge_github.read_required_for_base(Path("."), "main")
print(json.dumps(None if required is None else list(required)))
""")
    assert required is not None, "the rulesets or the classic protection of main could not be read"


def test_run_list_finds_the_green_ci_push_runs_on_main(github):
    green = _in_clone(github, f"""
import json
from agent_tools import cli_install, forge_github, upgrade_edge
code, out = cli_install._edge_run(forge_github.ci_runs_argv("{SLUG}", upgrade_edge.WORKFLOW, "main", limit=5))
print(json.dumps([code, list(forge_github.parse_green_shas(out)), out[:300]]))
""")
    code, shas, head = green
    assert code == 0, head
    assert shas and all(len(sha) == 40 for sha in shas), head


def test_pr_checks_watch_of_a_finished_pr_lists_no_failing_check(github):
    failing = _in_clone(github, f"""
import json
from agent_tools import chair_exec, chair_revert_ports
port = chair_revert_ports.GhForgePort(chair_revert_ports.bounded(chair_exec.run_argv))
print(json.dumps(port.wait_checks("{MERGED_PR}")))
""")
    assert failing == []


def test_pr_merge_of_a_merged_pr_reads_as_merged(github):
    merged = _in_clone(github, f"""
import json
from pathlib import Path
from agent_tools import forge_github
print(json.dumps(list(forge_github.merge_pr(Path("."), {MERGED_PR}))))
""")
    assert merged[0] is True, merged
