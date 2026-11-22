import json
import os
import subprocess as sp

import pytest

from agent_tools import cleanup, cli

# What run `x-5` of initiative `x` recorded: two tasks of its `seams` phase.
RECORDS = [{"phase": "seams", "task": "task"}, {"phase": "seams", "task": "task2"}]


def test_plan_keeps_phase_branches_and_targets_only_this_run(tmp_path):
    plan = cleanup.plan_cleanup(
        run_id="x-5", records=RECORDS,
        worktrees=["/repo", str(tmp_path / "wt/x-5/seams"), str(tmp_path / "wt/x-5/seams/task"), str(tmp_path / "wt/x-4/seams")],
        branches=["main", "epic/x/seams", "epic/x/seams--task", "agents/x-5/task", "agents/x-4/other", "epic/dock/p1--task", "epic/dock/p1"],
        worktree_root=str(tmp_path / "wt"),
    )
    assert plan["worktrees"] == [str(tmp_path / "wt/x-5/seams"), str(tmp_path / "wt/x-5/seams/task")]
    assert plan["branches"] == ["epic/x/seams--task", "agents/x-5/task"]
    assert plan["kept_phase_branches"] == ["epic/x/seams"]


def test_a_run_owns_its_scratch_namespace_and_its_recorded_tasks_merge_targets():
    branches = ["agents/x-5/task", "agents/x-5/unrecorded", "epic/x/seams--task", "epic/x/seams--task2", "epic/x/seams"]
    assert cleanup.owned_branches("x-5", RECORDS, branches) == ["agents/x-5/task", "agents/x-5/unrecorded", "epic/x/seams--task", "epic/x/seams--task2"]


def test_another_initiatives_merge_target_for_the_same_task_is_not_the_runs():
    owned = cleanup.owned_branches("r1", [{"phase": "p1", "task": "t1", "landed": True}], ["agents/r1/t1", "epic/dock/p1--t1"])
    assert owned == ["agents/r1/t1"]


def test_a_run_ids_initiative_is_its_id_without_the_ordinal_as_the_chair_reads_it():
    # `epic-x-5` is a run of initiative `epic-x`, so `epic/x/...` is not its: the mapping `run_initiative` makes.
    branches = ["epic/x/seams--task", "epic/epic-x/seams--task"]
    assert cleanup.owned_branches("epic-x-5", [{"phase": "seams", "task": "task"}], branches) == ["epic/epic-x/seams--task"]


def test_a_records_own_initiative_wins_over_the_run_ids():
    records = [{"phase": "seams", "task": "task", "initiative": "y"}]
    assert cleanup.owned_branches("x-5", records, ["epic/x/seams--task", "epic/y/seams--task"]) == ["epic/y/seams--task"]


def test_a_records_named_draft_is_the_runs():
    records = [{"phase": "seams", "task": "task", "draft": "epic/z/seams--task"}]
    assert cleanup.owned_branches("x-5", records, ["epic/z/seams--task", "epic/z/seams--other"]) == ["epic/z/seams--task"]


def test_a_draft_that_is_not_a_merge_target_is_not_taken_from_the_record():
    records = [{"phase": "seams", "task": "task", "draft": "main"}, {"phase": "seams", "task": "t2", "draft": "epic/x/seams"}]
    assert cleanup.owned_branches("x-5", records, ["main", "epic/x/seams"]) == []


def test_another_runs_scratch_namespace_is_not_the_runs_even_when_its_id_extends_this_one():
    assert cleanup.owned_branches("x-5", [], ["agents/x-5/t", "agents/x-50/t", "agents/x-5:p1/t", "agents/x-4/t"]) == ["agents/x-5/t"]


def test_new_model_phase_and_pr_branches_are_never_a_runs():
    branches = ["epic/x/p1", "pr/x--p1", "agents/x-5"]
    assert cleanup.owned_branches("x-5", [{"phase": "p1", "task": "t1"}], branches) == []


def test_an_empty_run_id_owns_nothing():
    assert cleanup.owned_branches("", [{"phase": "p1", "task": "t1"}], ["agents//t1", "epic//p1--t1", "epic/x/p1--t1"]) == []


def test_run_records_reads_phase_and_task_from_the_path(tmp_path):
    (tmp_path / "p1").mkdir()
    (tmp_path / "p1/t1.json").write_text(json.dumps({"landed": True}), encoding="utf-8")
    (tmp_path / "p1/t2.json").write_text(json.dumps(["not", "a", "record"]), encoding="utf-8")
    assert cleanup.run_records(tmp_path) == [{"phase": "p1", "task": "t1", "landed": True}]
    assert cleanup.run_records(tmp_path / "missing") == []


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f").write_text("x"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    sp.run(["git", "-C", str(root), "branch", "epic/x/seams"], check=True)
    sp.run(["git", "-C", str(root), "branch", "agents/x-5/task"], check=True)
    sp.run(["git", "-C", str(root), "branch", "epic/x/seams--task2"], check=True)
    wt = tmp_path / "wt/x-5/seams"; wt.parent.mkdir(parents=True)
    sp.run(["git", "-C", str(root), "worktree", "add", "-q", str(wt), "agents/x-5/task"], check=True)
    return root, tmp_path / "wt"


def test_apply_is_a_dry_run_unless_asked(repo):
    root, wtroot = repo
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=cleanup.git_worktrees(root), branches=cleanup.git_branches(root), worktree_root=str(wtroot))
    lines = cleanup.apply_cleanup(root, plan)
    assert any(l.startswith("would remove worktree") for l in lines) and "agents/x-5/task" in cleanup.git_branches(root)
    lines = cleanup.apply_cleanup(root, plan, dry_run=False, landed=["task"])
    assert any(l.startswith("removed worktree") for l in lines)
    assert "agents/x-5/task" not in cleanup.git_branches(root) and "epic/x/seams" in cleanup.git_branches(root)
    assert not (wtroot / "x-5").exists()


def test_a_branch_with_no_landed_or_dropped_record_is_kept_as_a_draft(repo):
    root, wtroot = repo
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=cleanup.git_worktrees(root), branches=cleanup.git_branches(root), worktree_root=str(wtroot))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False)
    assert any(l == "kept draft branch agents/x-5/task: unlanded" for l in lines)
    assert any(l == "kept draft branch epic/x/seams--task2: unlanded" for l in lines)
    assert "agents/x-5/task" in cleanup.git_branches(root)
    assert "epic/x/seams--task2" in cleanup.git_branches(root)


@pytest.fixture
def repo_unlanded(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f").write_text("x"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    sp.run(["git", "-C", str(root), "checkout", "-qb", "agents/x-5/task"], check=True, env=env)
    (root / "g").write_text("y"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "task work"], check=True, env=env)
    sp.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True, env=env)
    return root


def test_an_unlanded_branch_is_kept_as_a_draft(repo_unlanded):
    root = repo_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False)
    assert any(l == "kept draft branch agents/x-5/task: unlanded" for l in lines)
    assert "agents/x-5/task" in cleanup.git_branches(root)


def test_a_refused_tasks_branch_survives_a_clean_and_is_named_in_the_output(repo_unlanded):
    root = repo_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False, reasons={"task": "attempts_exhausted"})
    assert any(l == "kept draft branch agents/x-5/task: attempts_exhausted" for l in lines)
    assert "agents/x-5/task" in cleanup.git_branches(root)


def test_a_landed_task_record_lets_the_branch_be_deleted(repo_unlanded):
    root = repo_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False, landed=["task"])
    assert any(l == "deleted branch agents/x-5/task" for l in lines)
    assert "agents/x-5/task" not in cleanup.git_branches(root)


def test_a_dropped_work_items_branch_is_deleted(repo_unlanded):
    root = repo_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False, dropped=["task"])
    assert any(l == "deleted branch agents/x-5/task" for l in lines)
    assert "agents/x-5/task" not in cleanup.git_branches(root)


def test_force_deletes_the_unlanded_branch_and_says_so(repo_unlanded):
    root = repo_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False, force=True, reasons={"task": "attempts_exhausted"})
    assert any(l == "deleted branch agents/x-5/task (forced)" for l in lines)
    assert "agents/x-5/task" not in cleanup.git_branches(root)


def test_dry_run_prints_the_kept_line_and_deletes_nothing(repo_unlanded):
    root = repo_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan)
    assert any(l == "kept draft branch agents/x-5/task: unlanded" for l in lines)
    assert "agents/x-5/task" in cleanup.git_branches(root)


@pytest.fixture
def repo_epic_unlanded(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f").write_text("x"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    sp.run(["git", "-C", str(root), "checkout", "-qb", "epic/x/seams--task2"], check=True, env=env)
    (root / "g").write_text("y"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "task work"], check=True, env=env)
    sp.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True, env=env)
    return root


def test_an_unlanded_epic_task_branch_is_kept_as_a_draft(repo_epic_unlanded):
    root = repo_epic_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False)
    assert any(l == "kept draft branch epic/x/seams--task2: unlanded" for l in lines)
    assert "epic/x/seams--task2" in cleanup.git_branches(root)


def test_a_landed_epic_task_branch_is_deleted(repo_epic_unlanded):
    root = repo_epic_unlanded
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    lines = cleanup.apply_cleanup(root, plan, dry_run=False, landed=["task2"])
    assert any(l == "deleted branch epic/x/seams--task2" for l in lines)
    assert "epic/x/seams--task2" not in cleanup.git_branches(root)


def test_cox_runs_clean_reads_the_landed_record_through_the_profiles_workspace_dir(repo_unlanded, tmp_path, capsys):
    task_dir = tmp_path / "workspace/runs/x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({"landed": True}), encoding="utf-8")
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f"workspace_dir: {tmp_path / 'workspace'}\n", encoding="utf-8")
    rc = cli.main(["runs", "clean", "x-5", "--repo", str(repo_unlanded), "--profile", str(profile_path), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deleted branch agents/x-5/task" in out
    assert "agents/x-5/task" not in cleanup.git_branches(repo_unlanded)


def test_cox_runs_clean_keeps_a_refused_tasks_branch_and_names_its_reason(repo_unlanded, tmp_path, capsys):
    task_dir = tmp_path / "workspace/runs/x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({"status": "refused", "reason": "attempts_exhausted"}), encoding="utf-8")
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f"workspace_dir: {tmp_path / 'workspace'}\n", encoding="utf-8")
    rc = cli.main(["runs", "clean", "x-5", "--repo", str(repo_unlanded), "--profile", str(profile_path), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "kept draft branch agents/x-5/task: attempts_exhausted" in out
    assert "agents/x-5/task" in cleanup.git_branches(repo_unlanded)


def test_cox_runs_clean_deletes_a_dropped_tasks_branch_through_the_profiles_workspace_dir(repo_unlanded, tmp_path, capsys):
    task_dir = tmp_path / "workspace/runs/x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({"status": "dropped"}), encoding="utf-8")
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f"workspace_dir: {tmp_path / 'workspace'}\n", encoding="utf-8")
    rc = cli.main(["runs", "clean", "x-5", "--repo", str(repo_unlanded), "--profile", str(profile_path), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deleted branch agents/x-5/task" in out
    assert "agents/x-5/task" not in cleanup.git_branches(repo_unlanded)


@pytest.fixture
def repo_refused(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f").write_text("x"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    for b in ("agents/x-5/task", "epic/x/seams--task", "epic/y/other--elsewhere"):
        sp.run(["git", "-C", str(root), "branch", b], check=True)
    return root


def _clean_refused(root, **kw):
    plan = cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=[], branches=cleanup.git_branches(root), worktree_root=str(root.parent / "wt"))
    return cleanup.apply_cleanup(root, plan, dry_run=False, **kw)


def test_a_refused_tasks_agents_branch_survives_and_is_named_and_its_phase_task_branch_goes(repo_refused):
    lines = _clean_refused(repo_refused, reasons={"task": "attempts_exhausted"})
    assert "kept draft branch agents/x-5/task: attempts_exhausted" in lines
    assert "deleted branch epic/x/seams--task" in lines
    assert not any("kept draft branch epic/x/seams--task" in l for l in lines)
    branches = cleanup.git_branches(repo_refused)
    assert "agents/x-5/task" in branches and "epic/x/seams--task" not in branches


def test_another_initiatives_phase_task_branch_is_not_in_the_plan_and_survives(repo_refused):
    lines = _clean_refused(repo_refused, reasons={"task": "attempts_exhausted"})
    assert not any("epic/y/other--elsewhere" in l for l in lines)
    assert "epic/y/other--elsewhere" in cleanup.git_branches(repo_refused)


def test_force_never_reaches_another_initiatives_branch_even_for_a_task_of_the_same_name(repo_refused):
    sp.run(["git", "-C", str(repo_refused), "branch", "epic/dock/seams--task"], check=True)
    lines = _clean_refused(repo_refused, force=True, landed=["task"], reasons={"task": "unlanded", "elsewhere": "unlanded"})
    assert not any("epic/y/" in l or "epic/dock/" in l for l in lines)
    branches = cleanup.git_branches(repo_refused)
    assert "epic/y/other--elsewhere" in branches and "epic/dock/seams--task" in branches
    assert "epic/x/seams--task" not in branches


def test_a_landed_task_loses_both_branches(repo_refused):
    lines = _clean_refused(repo_refused, landed=["task"], reasons={"task": "unlanded"})
    assert "deleted branch agents/x-5/task" in lines and "deleted branch epic/x/seams--task" in lines
    branches = cleanup.git_branches(repo_refused)
    assert "agents/x-5/task" not in branches and "epic/x/seams--task" not in branches


def test_cox_runs_clean_keeps_a_refused_tasks_agents_branch_and_deletes_its_phase_task_branch(repo_refused, tmp_path, capsys):
    task_dir = tmp_path / "workspace/runs/x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({"status": "refused", "reason": "attempts_exhausted"}), encoding="utf-8")
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f"workspace_dir: {tmp_path / 'workspace'}\n", encoding="utf-8")
    rc = cli.main(["runs", "clean", "x-5", "--repo", str(repo_refused), "--profile", str(profile_path), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "kept draft branch agents/x-5/task: attempts_exhausted" in out
    assert "deleted branch epic/x/seams--task" in out
    branches = cleanup.git_branches(repo_refused)
    assert "agents/x-5/task" in branches and "epic/x/seams--task" not in branches
    assert "epic/y/other--elsewhere" in branches


def test_force_deletes_both_branches_of_a_refused_task(repo_refused):
    lines = _clean_refused(repo_refused, force=True, reasons={"task": "attempts_exhausted"})
    assert "deleted branch agents/x-5/task (forced)" in lines
    branches = cleanup.git_branches(repo_refused)
    assert "agents/x-5/task" not in branches and "epic/x/seams--task" not in branches


def _plan(root, wtroot, **kw):
    return cleanup.plan_cleanup(run_id="x-5", records=RECORDS, worktrees=cleanup.git_worktrees(root), branches=cleanup.git_branches(root), worktree_root=str(wtroot), **kw)


def test_a_live_plan_removes_nothing_even_with_force(repo):
    root, wtroot = repo
    plan = _plan(root, wtroot, live={"pid": 4242, "state": "running"})
    assert plan["worktrees"] == [] and plan["branches"] == [] and plan["root"] is None
    lines = cleanup.apply_cleanup(root, plan, dry_run=False, force=True, landed=["task"])
    assert lines == ["skipped run x-5: pid 4242 is running"]
    assert "agents/x-5/task" in cleanup.git_branches(root) and (wtroot / "x-5/seams").exists()


def test_a_fenced_owner_names_its_pid_in_the_skipped_line(repo):
    root, wtroot = repo
    lines = cleanup.apply_cleanup(root, _plan(root, wtroot, live={"pid": 4242, "state": "fenced"}))
    assert lines == ["skipped run x-5: pid 4242 is fenced"]


def test_a_plan_with_no_live_owner_is_unchanged(repo):
    root, wtroot = repo
    plan = _plan(root, wtroot, live=None)
    assert "skipped" not in plan and plan["worktrees"] == [str(wtroot / "x-5/seams")]
    assert plan == _plan(root, wtroot)


@pytest.mark.parametrize("state,expected", [("running", {"pid": 77, "state": "running"}), ("fenced", {"pid": 77, "state": "fenced"}), ("exited", None)])
def test_live_owner_counts_running_and_fenced_as_live(tmp_path, monkeypatch, state, expected):
    (tmp_path / "x-5.pid").write_text("77\n")
    monkeypatch.setattr(cleanup.epic, "run_state", lambda pid, pidfile, *a, **k: state)
    assert cleanup.live_owner(tmp_path, "x-5") == expected


def test_live_owner_is_none_without_a_readable_pidfile(tmp_path):
    assert cleanup.live_owner(tmp_path, "x-5") is None
    (tmp_path / "x-5.pid").write_text("not-a-pid")
    assert cleanup.live_owner(tmp_path, "x-5") is None
