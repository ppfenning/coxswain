"""Task-mode `runs land` feeds the planner a done-phase fact and per-branch ahead/behind counts.

The phase manifest the harness writes (`harness/epic.py`, `graphs/delivery/phase_validate.py`) carries
`phase_verdict.goal_met`; that is the passing-validation key read here. Every manifest below sets it true.
"""

import argparse
import json
import subprocess as sp

from agent_tools import cli

TASK = "seams-task"


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"add {name}")


def _repo(tmp_path, *, equivalent):
    """`develop` holds one commit and `epic/x/seams` forks from it with three task commits.
    With `equivalent`, develop then cherry-picks the same three, so git counts 3 ahead and 3 behind."""
    repo = tmp_path / "repo"
    sp.run(["git", "init", "-q", "-b", "develop", str(repo)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        _git(repo, *args)
    _commit(repo, "base")
    _git(repo, "checkout", "-qb", "epic/x/seams")
    for n in (1, 2, 3):
        _commit(repo, f"t{n}")
    _git(repo, "checkout", "-q", "develop")
    if equivalent:
        _git(repo, "cherry-pick", "-x", "epic/x/seams~3..epic/x/seams")
    _git(repo, "checkout", "-qb", "feature/268-ai-activity-type", "develop")
    _commit(repo, "feature")
    _git(repo, "checkout", "-q", "develop")
    (repo / ".agent").mkdir()
    (repo / ".agent" / "cartridge.yaml").write_text("base: develop\n")
    return repo


def _runs_dir(tmp_path, state="done"):
    runs_dir = tmp_path / "runs"
    tasks_dir = runs_dir / "epic-x-5" / "tasks" / "seams"
    tasks_dir.mkdir(parents=True)
    (runs_dir / "epic-x-5:seams.json").write_text(json.dumps({
        "phase": "seams", "initiative": "x", "phase_verdict": {"goal_met": True, "reasoning": "ok"},
    }), encoding="utf-8")
    (tasks_dir / f"{TASK}.json").write_text(json.dumps({
        "status": "done", "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "change_facts": {"fix_loop_attempts": 0, "files_touched": ["t1"]}, "title": "Add seams", "initiative": "x",
        "proposals": [{"kind": "draft_pr_create", "title": "Add seams"}],
    }), encoding="utf-8")
    items = tmp_path / "work" / "x" / "seams"
    items.mkdir(parents=True)
    (items / f"{TASK}.md").write_text(f"---\nid: {TASK}\nphase: seams\nstate: {state}\nneeds: []\ntitle: Add seams\n---\n\nbody\n",
                                      encoding="utf-8")
    (runs_dir / "policy.gate.json").write_text(json.dumps({"level": "full"}), encoding="utf-8")
    return runs_dir


def _land(repo, runs_dir, capsys, base=None):
    ns = argparse.Namespace(run_id="epic-x-5", repo=str(repo), task=TASK, phase=None, label=None, force=False,
                            worktree_root="~/worktrees", apply=False, no_merge=False, runs_dir=str(runs_dir),
                            profile=None, gate=None, base=base)
    rc = cli._runs_land(ns)
    out = capsys.readouterr().out
    return rc, json.loads(out[out.index("["):]), out


def test_a_done_phase_with_three_commits_dry_runs_to_a_plan_against_develop(tmp_path, capsys):
    rc, steps, _ = _land(_repo(tmp_path, equivalent=False), _runs_dir(tmp_path), capsys)
    assert rc == 0
    assert not [s for s in steps if s["kind"] == "refuse"]
    assert next(s for s in steps if s["kind"] == "cherry_pick")["commits"] == 3
    assert next(s for s in steps if s["kind"] == "pr_create")["base"] == "develop"


def test_an_explicit_pr_branch_base_is_the_branch_the_plan_targets(tmp_path, capsys):
    base = "feature/268-ai-activity-type"
    rc, steps, _ = _land(_repo(tmp_path, equivalent=False), _runs_dir(tmp_path), capsys, base=base)
    assert rc == 0
    assert not [s for s in steps if s["kind"] == "refuse"]
    assert next(s for s in steps if s["kind"] == "cherry_pick")["from"] == base
    assert next(s for s in steps if s["kind"] == "pr_create")["base"] == base


def test_a_branch_three_ahead_and_three_behind_but_patch_equivalent_refuses_with_both_counts(tmp_path, capsys):
    repo = _repo(tmp_path, equivalent=True)
    assert _git(repo, "rev-list", "--left-right", "--count", "develop...epic/x/seams") == "3\t3"
    rc, steps, out = _land(repo, _runs_dir(tmp_path), capsys)
    assert rc == 2
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "epic/x/seams: ahead 3, behind 3, 0 not already on develop" in steps[0]["reason"]
    assert "ahead 3, behind 3, 0 not already on" in out
