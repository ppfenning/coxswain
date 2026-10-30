"""The base-branch resolver and the callers routed through it, against real throwaway git repos."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from agent_tools import base_branch, cli, forge_github, land, remote_launch, runs_stranded

IDENTITY = ["-c", "user.name=Test Author", "-c", "user.email=author@example.test"]


@pytest.fixture(autouse=True)
def empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer git config, profile or cartridge leaks in: HOME is an empty directory for every test here."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *IDENTITY, *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-m", f"add {name}")


def clone_of_develop(tmp_path: Path, branch: str = "develop") -> Path:
    """A clone whose origin/HEAD names `branch` and whose origin holds no other branch."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", branch, str(seed)], check=True)
    commit(seed, "base.txt")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    return repo


def task_branch(repo: Path, run: str, task: str) -> None:
    git(repo, "checkout", "-q", "-b", f"agents/{run}/{task}")
    commit(repo, "task.txt")
    git(repo, "checkout", "-q", "develop")


def test_resolve_base_takes_flag_then_setting_then_origin_head_then_main() -> None:
    assert base_branch.resolve_base("f", "s", "h") == "f"
    assert base_branch.resolve_base(None, "s", "h") == "s"
    assert base_branch.resolve_base(None, None, "h") == "h"
    assert base_branch.resolve_base(None, None, None) == "main"
    assert base_branch.resolve_base("", " ", "origin/h") == "h"


def test_base_setting_reads_the_repo_entry_before_the_top_level() -> None:
    overlay = {"base": "top", "repos": {"app": {"base": "develop"}}}
    assert base_branch.base_setting(overlay, "app") == "develop"
    assert base_branch.base_setting(overlay, "other") == "top"
    assert base_branch.base_setting(None, "app") is None


def test_base_ref_is_the_bare_name_when_the_repo_has_no_origin(tmp_path: Path) -> None:
    repo = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    assert base_branch.base_ref(str(repo)) == "main"
    assert base_branch.base_ref(str(repo), "trunk") == "trunk"


def test_base_ref_follows_origin_head_and_refuses_a_missing_origin_ref(tmp_path: Path) -> None:
    repo = clone_of_develop(tmp_path)
    assert base_branch.base_ref(str(repo)) == "origin/develop"
    with pytest.raises(base_branch.BaseError, match="origin/main does not resolve"):
        base_branch.base_ref(str(repo), "main")


def test_a_base_setting_in_the_overlay_wins_over_origin_head(tmp_path: Path) -> None:
    repo = clone_of_develop(tmp_path)
    git(repo, "push", "-q", "origin", "develop:release")
    (repo / ".agent").mkdir()
    (repo / ".agent" / "cartridge.yaml").write_text("base: release\n")
    assert base_branch.configured_base(str(repo)) == "release"
    assert base_branch.base_ref(str(repo)) == "origin/release"


def test_a_develop_repo_plans_its_land_and_pr_against_develop(tmp_path: Path) -> None:
    repo = clone_of_develop(tmp_path)
    task_branch(repo, "r1", "t1")
    base = base_branch.base_ref(str(repo)).removeprefix("origin/")
    record = {"run": "r1", "task": "t1", "phase": "p", "initiative": "i", "review": {"verdict": "approve"},
              "proposals": [{"kind": "draft_pr_create", "title": "t1 title"}]}
    branches = cli._land_branches(repo, record, base)
    assert branches["agents/r1/t1"] == ["add task.txt"]
    steps = land.land_plan(record, branches, base, {}, tracker="none")
    assert not [s for s in steps if s["kind"] == "refuse"]
    assert next(s for s in steps if s["kind"] == "cherry_pick")["from"] == "develop"
    assert next(s for s in steps if s["kind"] == "pr_create")["base"] == "develop"
    assert next(s for s in steps if s["kind"] == "merge")["default_branch"] == "develop"
    refused = land.land_plan(record, cli._land_branches(repo, record, "main"), "main", {}, tracker="none")
    assert refused[0]["reason"] == "no branch is exactly one commit ahead of main"


def test_stranded_pins_a_develop_base_in_its_remedy_and_leaves_main_bare(tmp_path: Path) -> None:
    repo = clone_of_develop(tmp_path)
    record = {"run": "r1", "task": "t1", "phase": "p", "initiative": "i", "repo": str(repo),
              "review": {"verdict": "approve"}}
    item = {"id": "t1", "initiative": "i", "phase": "p", "state": "ready", "repo": str(repo)}
    [row] = runs_stranded.stranded([record], [item], base_of=base_branch.base_name)
    assert row["remedy"] == f"cox runs land r1 --task t1 --repo {repo} --base develop"
    [plain] = runs_stranded.stranded([record], [item])
    assert plain["remedy"] == f"cox runs land r1 --task t1 --repo {repo}"


def test_carry_forward_merges_origin_develop_for_a_base_develop_repo(tmp_path: Path) -> None:
    repo = clone_of_develop(tmp_path)
    git(repo, "branch", "epic/i/p")
    (repo / ".agent").mkdir()
    (repo / ".agent" / "cartridge.yaml").write_text("base: develop\n")
    from agent_tools import chair_exec

    seen: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return base_branch.run_git(argv)

    deps = type("D", (), {"run": staticmethod(run)})()
    merged, needs, failed = chair_exec._carry_forward({"kind": "clear"}, deps, str(repo), "i", ["p"])  # type: ignore[arg-type]
    assert (merged, needs, failed) == (["p"], None, None)
    assert any(a[-3:] == ["merge", "--no-edit", "origin/develop"] for a in seen)


def test_the_remote_launch_sync_follows_origin_head_and_falls_back_to_main(tmp_path: Path) -> None:
    develop = clone_of_develop(tmp_path / "a")
    base = remote_launch._chair_base(str(develop))
    assert base == remote_launch.ChairBase("develop", git(develop, "rev-parse", "origin/develop"))
    plain = clone_of_develop(tmp_path / "b", "main")
    git(plain, "remote", "set-head", "origin", "-d")
    base = remote_launch._chair_base(str(plain))
    assert base == remote_launch.ChairBase("main", git(plain, "rev-parse", "origin/main"))


def test_the_remote_launch_sync_prefers_the_repos_base_setting(tmp_path: Path) -> None:
    repo = clone_of_develop(tmp_path)
    git(repo, "push", "-q", "origin", "develop:release")
    (repo / ".agent").mkdir()
    (repo / ".agent" / "cartridge.yaml").write_text("base: release\n")
    base = remote_launch._chair_base(str(repo))
    assert base == remote_launch.ChairBase("release", git(repo, "rev-parse", "origin/release"))


def land_ns(runs_dir: Path, repo: Path, **overrides: object) -> argparse.Namespace:
    fields = {"run_id": "r1", "repo": str(repo), "task": "t1", "phase": None, "label": None, "force": False,
              "worktree_root": str(repo.parent / "wt"), "apply": False, "no_merge": False, "runs_dir": str(runs_dir),
              "profile": None, "gate": "full", "base": None}
    return argparse.Namespace(**{**fields, **overrides})


def steps_of(out: str) -> list[dict]:
    """The dry run's step list: the JSON array after any `land:` notice lines."""
    return json.loads(out[out.index("[\n"):])


def runs_dir_with_task(tmp_path: Path) -> Path:
    tasks = tmp_path / "runs" / "r1" / "tasks" / "p"
    tasks.mkdir(parents=True)
    (tasks / "t1.json").write_text(json.dumps({
        "status": "done", "review": {"verdict": "approve"}, "title": "t1 title",
        "proposals": [{"kind": "draft_pr_create", "title": "t1 title"}],
    }), encoding="utf-8")
    return tmp_path / "runs"


def test_runs_land_on_a_develop_repo_plans_one_commit_onto_develop_and_the_pr_argv_names_develop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = clone_of_develop(tmp_path)
    task_branch(repo, "r1", "t1")
    rc = cli._runs_land(land_ns(runs_dir_with_task(tmp_path), repo))
    steps = steps_of(capsys.readouterr().out)
    assert rc == 0
    assert next(s for s in steps if s["kind"] == "cherry_pick")["from"] == "develop"
    assert next(s for s in steps if s["kind"] == "merge")["default_branch"] == "develop"
    pr = next(s for s in steps if s["kind"] == "pr_create")
    argvs: list[list[str]] = []
    monkeypatch.setattr(forge_github, "_run", lambda argv, **kw: argvs.append(argv) or subprocess.CompletedProcess(argv, 0, "url", ""))
    assert forge_github.open_pr(repo, pr["title"], pr["body"], head=pr["head"], base=pr["base"]) == (True, "url")
    assert argvs[0][argvs[0].index("--base") + 1] == "develop"


def test_runs_land_accepts_base_and_refuses_one_origin_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = clone_of_develop(tmp_path)
    task_branch(repo, "r1", "t1")
    runs_dir = runs_dir_with_task(tmp_path)
    assert cli._runs_land(land_ns(runs_dir, repo, base="develop")) == 0
    assert next(s for s in steps_of(capsys.readouterr().out) if s["kind"] == "pr_create")["base"] == "develop"
    assert cli._runs_land(land_ns(runs_dir, repo, base="main")) == 2
    assert capsys.readouterr().out.startswith("land: refusing, origin/main does not resolve")


def test_runs_land_cli_parses_base() -> None:
    parser = argparse.ArgumentParser()
    command = next(c for c in cli.RUNS_COMMANDS if c.group == "runs" and c.name == "land")
    for arg in command.args:
        parser.add_argument(*arg.flags, **arg.kwargs)
    assert parser.parse_args(["r1", "--repo", "/r", "--base", "develop"]).base == "develop"
