"""`cox runs land` and `cox runs stranded` use the repo's integration branch (origin/HEAD, `--base`), not `main`."""

import json
import shlex

from test_land import _record
from test_land_repo_checks import _TASK, _git, _land

from agent_tools import chair_exec, cli, forge_local, land, land_repo_lease, store_cli


def _develop_repo(tmp_path):
    """A clone whose origin/HEAD is develop and which has no `main` at all, with a one-commit task branch."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    (repo / "f").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "develop")
    _git(repo, "remote", "set-head", "origin", "develop")
    _git(repo, "checkout", "-qb", _TASK)
    (repo / "f").write_text("y")
    _git(repo, "commit", "-aqm", "Add seams module")
    _git(repo, "checkout", "-q", "develop")
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"
    task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "github-projects"}', encoding="utf-8")
    return repo


def _steps(capsys):
    out = capsys.readouterr().out
    return json.loads(out[out.index("[") :])


def test_a_develop_repo_plans_its_one_commit_task_onto_origin_develop_and_opens_the_pr_against_develop(
    tmp_path, capsys
):
    repo = _develop_repo(tmp_path)
    _land(tmp_path, repo)
    steps = _steps(capsys)
    assert not any(s["kind"] == "refuse" for s in steps)
    assert next(s for s in steps if s["kind"] == "cherry_pick")["from"] == "origin/develop"
    assert next(s for s in steps if s["kind"] == "pr_create")["base"] == "develop"


def test_apply_lands_the_task_onto_origin_develop_opens_the_pr_against_develop_and_records_its_commit(
    tmp_path, monkeypatch, capsys
):
    repo = _develop_repo(tmp_path)
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "none"}', encoding="utf-8")
    opened = []
    monkeypatch.setattr(
        forge_local,
        "open_pr",
        lambda _repo, title, body, *, head=None, base=None: opened.append((head, base)) or (True, "ok"),
    )
    monkeypatch.setattr(land_repo_lease, "acquire", lambda *_a: store_cli.LeaseGranted(1, "me"))
    monkeypatch.setattr(land_repo_lease, "release", lambda *_a: None)
    monkeypatch.setattr(land_repo_lease, "renew", lambda *_a: store_cli.LeaseGranted(1, "me"))
    rc = _land(tmp_path, repo, "--apply", "--no-claim", "--gate", "full")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert opened == [("pr/seams-task", "develop")]
    assert _git(tmp_path / "origin.git", "log", "-1", "--format=%s", "develop") == "Add seams module"
    commit = chair_exec.land_commit(str(repo))
    assert commit and commit == _git(tmp_path / "origin.git", "rev-parse", "develop")


def test_the_merge_step_names_the_bare_branch_while_the_cut_starts_from_the_remote_ref():
    plan = land.land_plan(_record(), {_TASK: ["Add seams module"]}, "origin/develop")
    assert next(s for s in plan if s["kind"] == "merge")["default_branch"] == "develop"
    assert next(s for s in plan if s["kind"] == "cherry_pick")["from"] == "origin/develop"


def test_a_base_flag_naming_a_branch_origin_lacks_refuses_without_reading_a_local_branch(tmp_path, capsys):
    repo = _develop_repo(tmp_path)
    _git(repo, "branch", "typo")
    assert _land(tmp_path, repo, "--base", "typo") == 2
    assert f"runs land: origin/typo not found in {repo}" in capsys.readouterr().out


def _stranded_world(tmp_path):
    """The develop repo with its origin/HEAD removed, so only the profile can say develop; one approved task, one ready work item."""
    repo = _develop_repo(tmp_path)
    _git(repo, "remote", "set-head", "origin", "-d")
    record = {**_record(), "repo": str(repo)}
    (tmp_path / "runs/epic-x-5/tasks/seams/seams-task.json").write_text(json.dumps(record), encoding="utf-8")
    item = tmp_path / "work/x/seams/seams-task.md"
    item.parent.mkdir(parents=True)
    item.write_text("---\nid: seams-task\nstate: ready\n---\nbody\n", encoding="utf-8")
    (tmp_path / "work/x/initiative.md").write_text(f"---\nid: x\nrepo: {repo}\n---\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f'repo_base: {{"{repo}": "develop"}}\n', encoding="utf-8")
    return repo, profile


def test_stranded_reads_the_profile_repo_base_and_prints_a_remedy_that_lands_the_repo(tmp_path, capsys):
    repo, profile = _stranded_world(tmp_path)
    runs = str(tmp_path / "runs")
    assert cli.main(["runs", "stranded", "--runs-dir", runs, "--profile", str(profile)]) == 0
    remedy = next(line for line in capsys.readouterr().out.splitlines() if "cox runs land" in line)
    assert remedy.endswith(f"--repo {repo} --base develop")
    argv = shlex.split(remedy[remedy.index("cox runs land") :])[1:]
    assert cli.main([*argv, "--runs-dir", runs]) == 0
    assert not any(s["kind"] == "refuse" for s in _steps(capsys))


def test_stranded_without_the_profile_setting_prints_no_base_flag_when_origin_head_is_gone(tmp_path, capsys):
    repo, _ = _stranded_world(tmp_path)
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert cli.main(["runs", "stranded", "--runs-dir", str(tmp_path / "runs"), "--profile", str(empty)]) == 0
    assert "--base" not in capsys.readouterr().out


def test_stranded_says_so_when_the_profile_cannot_be_read(tmp_path, capsys):
    _stranded_world(tmp_path)
    broken = tmp_path / "broken.yaml"
    broken.write_text("repo_base: {not json\n", encoding="utf-8")
    cli.main(["runs", "stranded", "--runs-dir", str(tmp_path / "runs"), "--profile", str(broken)])
    assert "profile unreadable" in capsys.readouterr().err
