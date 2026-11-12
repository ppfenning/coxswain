import json
import subprocess

from agent_tools import cli
from agent_tools.chair_facts import (
    approval_holds_from_actions,
    approved_facts,
    resolve_approval_hold_ticks,
    running_initiatives,
)

ROW = {
    "id": "t1", "initiative": "i", "repo": "r", "phase": "p1", "phase_done": True, "needs": [], "run": "i-1",
    "needs_fetch": False,
}
LANDS = [
    {"ts": f"2026-11-09T0{n}:00:00Z", "kind": "land", "task_id": "t1", "initiative": "i", "status": "refused"}
    for n in range(1, 5)
]


def test_a_branch_merged_approved_row_carries_the_fact_and_has_no_hold_however_many_lands():
    [fact] = approved_facts([ROW], {"i": "initiative"}, lambda row: True)
    assert (fact["branch_merged"], fact["land_at"]) == (True, "initiative")
    assert approval_holds_from_actions([fact], LANDS) == []


def test_a_row_the_stub_says_is_not_branch_merged_holds_as_before():
    [fact] = approved_facts([ROW], {}, lambda row: False)
    assert (fact["branch_merged"], fact["land_at"]) == (False, "initiative")
    assert [h["ticks"] for h in approval_holds_from_actions([fact], LANDS)] == [4]


def test_task_landing_never_consults_the_branch_merged_callable():
    asked = []
    [fact] = approved_facts([ROW], {"i": "task"}, lambda row: asked.append(row) or True)
    assert (fact["branch_merged"], fact["land_at"], asked) == (False, "task", [])


def test_no_callable_reads_branch_merged_false():
    assert [f["branch_merged"] for f in approved_facts([ROW])] == [False]


RECORD = {"id": "t1", "phase": "p1", "branch": "agents/i-1/t1"}


def _cli_runner(calls):
    def run(argv):
        calls.append(argv)
        return (0, "+x\n") if "show" in argv else (0, "c0ffee\n")
    return run


def _binding(tmp_path, calls):
    record = tmp_path / "i-1" / "tasks" / "p1" / "t1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(RECORD))
    return cli._approved_branch_merged(_cli_runner(calls), lambda row: "/repo", tmp_path)


def test_the_cli_binding_resolves_the_recorded_branch_and_asks_the_epic_phase_branch(tmp_path):
    calls = []
    assert _binding(tmp_path, calls)({**ROW, "land_at": "ticket"}) is True
    listed = [a[-1] for a in calls if "rev-list" in a]
    assert ["git", "-C", "/repo", "rev-parse", "--verify", "--quiet", "agents/i-1/t1^{commit}"] in calls
    assert listed[0] == "main..epic/i/p1"


def test_the_cli_binding_reads_false_when_the_recorded_branch_no_longer_resolves(tmp_path):
    asked = _binding(tmp_path, [])
    gone = cli._approved_branch_merged(lambda argv: (1, ""), lambda row: "/repo", tmp_path)
    assert (asked({**ROW, "land_at": "initiative"}), gone({**ROW, "land_at": "initiative"})) == (True, False)


def test_the_cli_binding_finds_a_commit_on_the_epic_phase_branch_in_a_real_repo(tmp_path):
    def git(*args):
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
        return done.returncode, done.stdout

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "a@b"], ["config", "user.name", "n"],
                 ["commit", "-q", "--allow-empty", "-m", "base"], ["checkout", "-q", "-b", "agents/i-1/t1"],
                 ["commit", "-q", "--allow-empty", "-m", "work"], ["checkout", "-q", "-b", "epic/i/p1", "main"],
                 ["merge", "-q", "--no-ff", "agents/i-1/t1", "-m", "merge"], ["checkout", "-q", "main"]):
        assert git(*args)[0] == 0
    record = tmp_path / "runs" / "i-1" / "tasks" / "p1" / "t1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(RECORD))
    run = lambda argv: git(*argv[3:]) if argv[:2] == ["git", "-C"] else (1, "")  # noqa: E731
    merged = cli._approved_branch_merged(run, lambda row: str(repo), tmp_path / "runs")
    assert (merged({**ROW, "land_at": "ticket"}), merged({**ROW, "phase": "p2", "land_at": "ticket"})) == (True, False)


def test_the_cli_binding_under_task_landing_never_runs_git(tmp_path):
    calls = []
    assert _binding(tmp_path, calls)({**ROW, "land_at": "task"}) is False
    assert calls == []


def test_a_stale_approval_only_entry_is_not_live_and_a_live_entry_and_a_live_phase_are():
    items = [
        {"initiative": "a", "id": "a1", "state": "ready", "surfaces": ["a.rs"]},
        {"initiative": "b", "id": "b1", "state": "approved", "surfaces": ["b.rs"]},
        {"initiative": "c", "id": "c1", "state": "in_progress", "phase": "p1", "surfaces": ["c.rs"]},
    ]
    running = running_initiatives({"a"}, items, {}, unlanded={"b"}, live_phases=[("c", "p1")])
    assert [(r["id"], r.get("phase"), r["live"]) for r in running] == [("a", None, True), ("b", None, False), ("c", "p1", True)]


def test_hold_age_counts_distinct_ticks_since_the_last_landed_row_and_skips_merged_tasks():
    approved = [
        {"id": "t1", "initiative": "i", "repo": "r", "run": "i-1", "merged": False},
        {"id": "t2", "initiative": "i", "repo": "r", "run": "i-1", "merged": True},
    ]
    actions = [
        {"ts": "2026-11-09T01:00:00Z", "kind": "land", "task_id": "t1", "initiative": "i", "status": "refused"},
        {"ts": "2026-11-09T02:00:00Z", "kind": "land_phase", "task_id": "t1", "initiative": "i", "status": "landed"},
        {"ts": "2026-11-09T03:00:00Z", "kind": "land", "task_id": "t1", "initiative": "i", "status": "refused"},
        {"ts": "2026-11-09T04:00:00Z", "kind": "steer_clear", "initiative": "i", "other": "j"},
        {"ts": "2026-11-09T04:00:00Z", "kind": "steer_clear", "initiative": "i", "other": "k"},
        {"ts": "2026-11-09T05:00:00Z", "kind": "pull"},
        {"ts": "2026-11-09T06:00:00Z", "kind": "land", "task_id": "t9", "initiative": "i", "status": "refused"},
    ]
    assert approval_holds_from_actions(approved, actions) == [
        {"initiative": "i", "repo": "r", "task_id": "t1", "run": "i-1", "ticks": 2}
    ]


def test_the_approval_hold_limit_defaults_to_6_when_missing_non_numeric_or_not_positive():
    assert [resolve_approval_hold_ticks(v) for v in (None, "x", 0, -3, "4")] == [6, 6, 6, 6, 4]
