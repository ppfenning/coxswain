"""A task land skips commits the base already holds, and closes through the carrying PR when it skips them all."""
import subprocess

import pytest

from agent_tools import cli

RECORD = {"run": "r", "task": "t", "phase": "p", "initiative": "i", "status": "approved", "build": {"files_touched": ["a.txt"]}}
PICK = {"kind": "cherry_pick", "branch": "agents/t", "onto": "pr/t", "from": "main"}
REAL_STEP = cli._execute_land_step


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    """Land worktrees live under the temp dir by repo and branch name: give each test its own."""
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))


def git(where, *args):
    return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True).stdout.strip()


def _commit(where, files, subject):
    for name, text in files.items():
        (where / name).write_text(text + "\n")
        git(where, "add", name)
    git(where, "commit", "-qm", subject)
    return git(where, "rev-parse", "HEAD")


def _clone(tmp_path):
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    git(clone, "config", "user.name", "t")
    git(clone, "config", "user.email", "t@t")
    git(clone, "remote", "add", "origin", str(origin))
    _commit(clone, {"base.txt": "base"}, "base")
    return clone


def _stacked(tmp_path, main_subject="lower and more (#41)", upper=True):
    """Branch agents/t holds `lower` (a.txt) and, when `upper`, real work (b.txt). Main holds a.txt inside a
    larger commit, so the patch-ids differ and picking `lower` onto main is empty."""
    clone = _clone(tmp_path)
    git(clone, "checkout", "-qb", "agents/t")
    lower = _commit(clone, {"a.txt": "a"}, "lower")
    if upper:
        _commit(clone, {"b.txt": "b"}, "upper")
    git(clone, "checkout", "-q", "main")
    _commit(clone, {"a.txt": "a", "c.txt": "c"}, main_subject)
    git(clone, "push", "-q", "origin", "main")
    return clone, lower


def _carried(tmp_path, main_subject="task work (#42)"):
    """Branch agents/t holds a.txt; main holds the same patch under `main_subject`."""
    clone = _clone(tmp_path)
    git(clone, "checkout", "-qb", "agents/t")
    _commit(clone, {"a.txt": "a"}, "a")
    git(clone, "checkout", "-q", "main")
    _commit(clone, {"a.txt": "a"}, main_subject)
    git(clone, "push", "-q", "origin", "main")
    return clone


def _steps(tmp_path):
    path = str(tmp_path / "runs" / "r" / "tasks" / "p" / "t.json")
    return [PICK, {"kind": "push", "branch": "pr/t"}, {"kind": "checks"}, {"kind": "pr_create"}, {"kind": "merge"},
            {"kind": "clean", "branch": "agents/t", "worktree_root": str(tmp_path / "wt"), "run": "r", "task": "t"},
            {"kind": "mark_done", "task": "t", "path": path}]


def _walk(monkeypatch, repo, steps):
    """The real cherry_pick and clean steps, every other step recorded and reported ok."""
    ran = []

    def step(_repo, s, _forge=None):
        ran.append(s)
        return REAL_STEP(_repo, s) if s["kind"] in ("cherry_pick", "clean") else (True, "ok")

    monkeypatch.setattr(cli, "_execute_land_step", step)
    rc, reached, pr = cli._land_execute(repo, steps, steps, RECORD, None, "merge", False, None)
    return rc, reached, pr, ran


def test_a_stacked_branch_skips_the_landed_lower_commit_and_picks_the_upper(tmp_path, capsys):
    repo, lower = _stacked(tmp_path)
    ok, detail = cli._execute_land_step(repo, PICK)
    assert (ok, detail) == (True, "cherry-picked 1 commits onto pr/t")
    assert f"land: skipped {lower[:8]} lower: empty pick" in capsys.readouterr().out
    assert git(repo, "show", "pr/t:b.txt") == "b"
    assert git(repo, "log", "--format=%s", "main..pr/t") == "upper"


def test_a_branch_main_holds_by_patch_id_ends_through_the_carrying_pr(tmp_path, monkeypatch, capsys):
    repo = _carried(tmp_path)
    rc, reached, pr, ran = _walk(monkeypatch, repo, _steps(tmp_path))
    out = capsys.readouterr().out
    assert (rc, reached, pr) == (0, ["clean", "mark_done"], "#42")
    assert ran[-1]["pr"] == "#42"
    branches = git(repo, "branch", "--format=%(refname:short)").split()
    assert "pr/t" not in branches and "agents/t" not in branches
    assert "landed via PR #42" in out.splitlines()


def test_the_walk_closes_through_the_carrier_found_after_an_all_skipped_pick(tmp_path, monkeypatch, capsys):
    """The pre-pick check finds nothing here and the check after the all-skipped pick finds PR 42."""
    calls = []

    def carrier(_repo, _step, _record):
        calls.append(1)
        return None if len(calls) == 1 else 42

    monkeypatch.setattr(cli, "_task_already_on_main", carrier)
    repo = _carried(tmp_path)
    rc, reached, pr, ran = _walk(monkeypatch, repo, _steps(tmp_path))
    out = capsys.readouterr().out
    assert (rc, reached, pr) == (0, ["cherry_pick", "clean", "mark_done"], "#42")
    assert ran[-1]["pr"] == "#42"
    assert [s["kind"] for s in ran] == ["cherry_pick", "clean", "mark_done"]
    assert "patch-id match on base" in out
    assert "landed via PR #42" in out.splitlines()
    branches = git(repo, "branch", "--format=%(refname:short)").split()
    assert "pr/t" not in branches and "agents/t" not in branches


def test_all_skipped_with_no_pr_number_on_main_stops_with_a_failure(tmp_path, monkeypatch, capsys):
    rc, reached, _pr, _ran = _walk(monkeypatch, _carried(tmp_path, main_subject="task work"), _steps(tmp_path))
    assert (rc, reached) == (1, ["cherry_pick"])
    assert "no pull request number was found" in capsys.readouterr().out


def test_a_branch_with_no_commits_ahead_is_still_refused(tmp_path):
    repo = _clone(tmp_path)
    git(repo, "branch", "agents/t")
    ok, detail = cli._execute_land_step(repo, PICK)
    assert not ok
    assert detail.startswith("expected at least one commit ahead of main on agents/t")


def test_every_pick_empty_reports_all_skipped(tmp_path, capsys):
    repo, lower = _stacked(tmp_path, upper=False)
    ok, detail = cli._execute_land_step(repo, PICK)
    assert (ok, detail) == (True, "skipped all 1 commits, already on main")
    assert f"land: skipped {lower[:8]} lower: empty pick" in capsys.readouterr().out


def test_a_real_conflicting_pick_still_fails(tmp_path):
    repo = _clone(tmp_path)
    git(repo, "checkout", "-qb", "agents/t")
    _commit(repo, {"base.txt": "branch"}, "branch change")
    git(repo, "checkout", "-q", "main")
    _commit(repo, {"base.txt": "main"}, "main change")
    git(repo, "push", "-q", "origin", "main")
    ok, detail = cli._execute_land_step(repo, PICK)
    assert not ok
    assert detail.startswith("cherry-pick of ") and "conflicts in: base.txt" in detail


def test_a_partly_skipped_range_goes_on_to_push_and_pr(tmp_path, monkeypatch, capsys):
    repo, lower = _stacked(tmp_path)
    steps = [s for s in _steps(tmp_path) if s["kind"] != "merge"]
    rc, reached, pr, _ran = _walk(monkeypatch, repo, steps)
    assert (rc, reached) == (0, ["cherry_pick", "push", "checks", "pr_create", "clean", "mark_done"])
    assert f"land: skipped {lower[:8]} lower: empty pick" in capsys.readouterr().out
    assert pr == ""


def test_an_all_skipped_gather_step_does_not_close_the_walk(tmp_path, monkeypatch, capsys):
    repo = _carried(tmp_path)
    git(repo, "checkout", "-qb", "agents/u", "main")
    _commit(repo, {"d.txt": "d"}, "d")
    git(repo, "checkout", "-q", "main")
    steps = [{**PICK, "task": "t"}, {**PICK, "task": "u", "branch": "agents/u"}, *_steps(tmp_path)[1:2], *_steps(tmp_path)[5:]]
    rc, reached, _pr, _ran = _walk(monkeypatch, repo, steps)
    assert (rc, reached) == (0, ["cherry_pick", "cherry_pick", "push", "clean", "mark_done"])
    assert "landed via" not in capsys.readouterr().out
    assert git(repo, "show", "pr/t:d.txt") == "d"


def test_a_commit_authored_empty_is_not_skipped(tmp_path):
    repo = _clone(tmp_path)
    git(repo, "checkout", "-qb", "agents/t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "nothing")
    git(repo, "checkout", "-q", "main")
    git(repo, "push", "-q", "origin", "main")
    ok, detail = cli._execute_land_step(repo, PICK)
    assert not ok
    assert "skipped all" not in detail


def test_an_existing_pr_branch_is_refused_and_kept(tmp_path):
    repo = _carried(tmp_path)
    git(repo, "branch", "pr/t", "main")
    ok, _detail = cli._execute_land_step(repo, PICK)
    assert not ok
    assert "pr/t" in git(repo, "branch", "--format=%(refname:short)").split()


def test_a_gather_step_skips_an_empty_pick_and_goes_on(tmp_path, capsys):
    repo, lower = _stacked(tmp_path)
    ok, detail = cli._execute_land_step(repo, {**PICK, "task": "t"})
    assert (ok, detail) == (True, "cherry-picked 1 commits onto pr/t")
    assert f"land: skipped {lower[:8]} lower: empty pick" in capsys.readouterr().out
    assert git(repo, "show", "pr/t:b.txt") == "b"


def test_a_gather_step_with_every_pick_empty_reports_all_skipped(tmp_path):
    repo, _lower = _stacked(tmp_path, upper=False)
    assert cli._execute_land_step(repo, {**PICK, "task": "t"}) == (True, "skipped all 1 commits, already on main")


def test_a_gather_step_holding_by_patch_id_reports_all_skipped(tmp_path, capsys):
    ok, detail = cli._execute_land_step(_carried(tmp_path), {**PICK, "task": "t"})
    assert (ok, detail) == (True, "skipped all 1 commits, already on main")
    assert "patch-id match on base" in capsys.readouterr().out


def test_the_resume_check_still_cannot_build_a_tree_for_an_empty_pick(tmp_path):
    repo, lower = _stacked(tmp_path)
    tree, why = cli._generated_tree(repo, "pr/t", "main", lower, None)
    assert tree is None
    assert why == f"cherry-pick of {lower[:8]} is empty on main"
