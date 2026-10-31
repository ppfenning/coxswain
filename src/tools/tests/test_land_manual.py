"""`land: manual`: no land is proposed, and an approved task finishes once its PR merge shows on origin."""
import json
import subprocess
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_read_stranded, land_manual, runs_stranded, store_cli
from agent_tools.chair_plan_land import plan_finish, plan_lands
from agent_tools.chair_read_approved import with_land_mode

PR = "pr/init--p1"
BUILD = "agents/init-1/t1"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return done.stdout


@pytest.fixture(autouse=True)
def empty_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def identify(repo: Path) -> None:
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")


def make_repo(tmp_path: Path, how: str) -> Path:
    """A clone of a bare origin. The land's PR branch `pr/init--p1` is pushed to origin; the build branch never is.

    `how` is what the forge did to the PR: "none", "merge" (a merge commit), "squash", or "squash-deleted" (squash, then
    the PR branch deleted). It happens on origin only, through a second clone, so this clone's refs stay stale."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    clone = tmp_path / "work" / "repo"
    clone.parent.mkdir()
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    identify(clone)
    git(clone, "checkout", "-q", "-b", "main")
    (clone / "a.txt").write_text("a\n")
    git(clone, "add", "a.txt")
    git(clone, "commit", "-q", "-m", "base")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "remote", "set-head", "origin", "main")
    git(clone, "checkout", "-q", "-b", PR)
    for name in ("b.txt", "c.txt"):
        (clone / name).write_text(name)
        git(clone, "add", name)
        git(clone, "commit", "-q", "-m", f"add {name}")
    git(clone, "push", "-q", "origin", PR)
    git(clone, "branch", BUILD)
    git(clone, "checkout", "-q", "main")
    if how != "none":
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
        identify(other)
        if how == "merge":
            git(other, "merge", "-q", "--no-ff", "-m", "merge PR", f"origin/{PR}")
        else:
            git(other, "merge", "-q", "--squash", f"origin/{PR}")
            git(other, "commit", "-q", "-m", "squash PR")
        git(other, "push", "-q", "origin", "main")
        if how == "squash-deleted":
            git(other, "push", "-q", "origin", "--delete", PR)
    return clone


def approved_task(repo: Path, manual: bool = True) -> dict:
    return {"id": "t1", "initiative": "init", "repo": str(repo), "phase": "p1", "phase_done": True, "needs": [],
            "run": "init-1", "needs_fetch": False, "land_manual": manual}


def facts(*tasks: dict) -> dict:
    return {"approved": list(tasks), "initiatives": [{"landed": set()}]}


def runs_dir_with_record(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    record = runs / "init-1" / "tasks" / "p1" / "t1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"task": "t1", "branch": BUILD}))
    return runs


def deps(runs: Path) -> chair_exec.Deps:
    def run(argv: list[str]) -> tuple[int, str]:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        return done.returncode, done.stdout + done.stderr

    return chair_exec.Deps(run=run, delete_branches=lambda r, p: ([], ""), acquire_lease=lambda *a, **k: "",
                           record=lambda a: None, run_id=lambda a: "", repo_for=lambda a: "", runs_dir=runs)


def finish(repo: Path, runs: Path, monkeypatch) -> tuple[dict, list[tuple]]:
    """The planner's own finish_task action, run through the executor's dispatch. Only the store write is replaced."""
    writes: list[tuple] = []

    def set_state(runs_dir, initiative, task, state, by, expected=None):
        writes.append((initiative, task, state, by, expected))
        return store_cli.StateSet({})

    monkeypatch.setattr(store_cli, "set_state", set_state)
    [action] = plan_finish(facts(approved_task(repo)))
    return chair_exec._execute(action, deps(runs), {}), writes


@pytest.mark.parametrize("how", ["squash", "merge"])
def test_a_pr_merged_only_on_the_remote_marks_the_task_done_after_the_fetch(tmp_path, monkeypatch, how):
    repo = make_repo(tmp_path, how)
    assert PR not in git(repo, "branch", "-r", "--merged", "origin/main").split()  # origin/main is stale until the fetch
    assert not (repo / "b.txt").exists()  # so is local main
    result, writes = finish(repo, runs_dir_with_record(tmp_path), monkeypatch)
    assert result["status"] == "done", result
    assert writes == [("init", "t1", "done", "chair-loop", "approved")]
    assert f"origin/{PR} is merged into origin/main" in result["reason"]


def test_an_unmerged_pr_stays_approved_and_is_rechecked_later_not_every_tick(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "none")
    runs = runs_dir_with_record(tmp_path)
    result, writes = finish(repo, runs, monkeypatch)
    # Dependents follow the store state of their needs, so no done write means they stay unstarted.
    assert result["status"] == "skipped" and writes == []
    assert f"origin/{PR} is not merged into any target" in result["reason"]
    again, _ = finish(repo, runs, monkeypatch)
    assert again["status"] == "skipped" and "checked less than" in again["reason"]


def test_a_deleted_pr_branch_is_reported_with_the_escape_hatch_not_left_silent(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "squash-deleted")
    result, writes = finish(repo, runs_dir_with_record(tmp_path), monkeypatch)
    assert result["status"] == "failed" and writes == []
    assert "no PR branch on origin to check" in result["reason"]
    assert "cox route edit <task> --state done" in result["reason"]


def test_an_unreachable_origin_names_the_fetch_error(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "squash")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result, writes = finish(repo, runs_dir_with_record(tmp_path), monkeypatch)
    assert result["status"] == "failed" and writes == []
    assert "missing.git" in result["reason"]


def test_the_verdict_and_names_are_pure():
    assert land_manual.pr_branches("i", "p", "t", "agents/r/t") == ["pr/i--p", "pr/t", "agents/r/t"]
    assert land_manual.pr_branches("i", "p", "t", None) == ["pr/i--p", "pr/t"]
    assert land_manual.merge_targets("main", "i", "p") == ["main", "epic/i/p"]
    assert land_manual.merged_verdict({"b": ""}, {"b": "main"}) == ("merged", "origin/b is merged into origin/main")
    assert land_manual.merged_verdict({"b": ""}, {"b": None})[0] == "unmerged"
    assert land_manual.merged_verdict({"b": "fatal: no ref"}, {})[0] == "unverifiable"


def test_the_chair_plans_no_land_for_a_manual_repo_and_one_finish(tmp_path):
    assert plan_lands(facts(approved_task(tmp_path, manual=True))) == []
    assert [a["kind"] for a in plan_finish(facts(approved_task(tmp_path, manual=True)))] == ["finish_task"]


def test_the_chair_still_lands_a_default_repo(tmp_path):
    assert [a["kind"] for a in plan_lands(facts(approved_task(tmp_path, manual=False)))] == ["land_phase"]
    assert plan_finish(facts(approved_task(tmp_path, manual=False))) == []


def manual_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".agent").mkdir(parents=True)
    (repo / ".agent" / "cartridge.yaml").write_text("land: manual\n")
    other = tmp_path / "other_repo"
    other.mkdir()
    return repo, other


def test_the_overlay_sets_land_manual(tmp_path):
    repo, other = manual_repo(tmp_path)
    assert land_manual.is_manual(str(repo)) and not land_manual.is_manual(str(other))
    assert land_manual.land_setting({"repos": {"repo": {"land": "Manual"}}}, "repo") == "manual"
    rows = with_land_mode([approved_task(repo), approved_task(other)], land_manual.is_manual)
    assert [r["land_manual"] for r in rows] == [True, False]


def test_the_chairs_stranded_reader_says_the_human_merges_the_pr(tmp_path):
    repo, other = manual_repo(tmp_path)
    approve = {"review": {"verdict": "approve"}, "adversary": {"verdict": "approve"}}

    def remedy(path: Path) -> str:
        record = {"run": "init-1", "task": "t1", "phase": "p1", "branch": BUILD, "repo": str(path), **approve}
        item = {"id": "t1", "initiative": "init", "phase": "p1", "state": "approved", "repo": str(path)}
        [row] = chair_read_stranded.read_stranded([record], [item], lambda r: True, lambda r, b: False)
        return row["remedy"]

    assert remedy(repo) == land_manual.REMEDY and "runs land" not in remedy(repo)
    assert remedy(other).startswith("cox runs land init-1 --task t1")
    record = {"run": "init-1", "task": "t1", "repo": str(repo)}
    assert runs_stranded._remedy(record, None, manual_of=land_manual.is_manual) == land_manual.REMEDY
