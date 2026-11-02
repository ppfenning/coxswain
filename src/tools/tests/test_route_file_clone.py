"""`route file` records the umbrella's working clone, or refuses the install checkout, on both filing paths."""

import pytest

from agent_tools import run_store
from agent_tools.cli import main


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)


def _setup(tmp_path, with_clone, clone_is_checkout=True):
    ws = tmp_path / "workspace"
    (ws / "intake").mkdir(parents=True)
    umbrella, clone, other = (tmp_path / n for n in ("umbrella", "clone", "other"))
    for repo in (umbrella, clone, other):
        (repo / ".git" if clone_is_checkout or repo != clone else repo).mkdir(parents=True)
    settings = f"umbrella_dir: {umbrella}\n" + (f"umbrella_clone_dir: {clone}\n" if with_clone else "")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/h\ncartridges_dir: /opt/c\nprovider_profile: /opt/p.yaml\n{settings}"
    )
    return profile, ws, umbrella, clone, other


def _written(ws):
    return sorted(p for p in ws.rglob("*") if p.is_file())


def _intake(ws, repo):
    path = ws / "intake" / "2026-11-02-fix-thing.md"
    path.write_text(f"---\nid: fix-thing\ntitle: Fix the thing\nrepo: {repo}\n---\n\nDo it.\n")
    return path


def _file(profile, repo):
    return main(["route", "file", "--profile", str(profile), "--repo", str(repo), "--title", "Fix the thing"])


def test_the_umbrella_path_is_recorded_as_the_clone(tmp_path):
    profile, ws, umbrella, clone, _ = _setup(tmp_path, with_clone=True)
    assert _file(profile, umbrella) == 0
    [initiative] = ws.glob("work/*/initiative.md")
    assert f"repo: {clone}\n" in initiative.read_text()
    assert str(umbrella) not in initiative.read_text()


def test_the_umbrella_path_without_a_clone_is_refused_and_writes_nothing(tmp_path, capsys):
    profile, ws, umbrella, _, _ = _setup(tmp_path, with_clone=False)
    assert _file(profile, umbrella) == 2
    out = capsys.readouterr().out
    assert out.startswith("routing: ") and "umbrella_clone_dir" in out
    assert _written(ws) == []


def test_an_intake_naming_the_umbrella_is_filed_against_the_clone(tmp_path):
    profile, ws, umbrella, clone, _ = _setup(tmp_path, with_clone=True)
    intake = _intake(ws, umbrella)
    assert main(["route", "file", "--profile", str(profile), "--from-intake", str(intake)]) == 0
    [initiative] = ws.glob("work/*/initiative.md")
    assert f"repo: {clone}\n" in initiative.read_text()
    assert str(umbrella) not in initiative.read_text()
    [archived] = (ws / "intake" / "done").glob("*.md")
    assert f"repo: {clone}\n" in archived.read_text()
    assert str(umbrella) not in archived.read_text()


def test_a_clone_under_home_is_recorded_in_the_same_form_by_both_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    profile, ws, umbrella, _, _ = _setup(tmp_path, with_clone=True)
    assert main(["route", "file", "--profile", str(profile), "--repo", str(umbrella), "--title", "Other thing"]) == 0
    assert main(["route", "file", "--profile", str(profile), "--from-intake", str(_intake(ws, umbrella))]) == 0
    by_repo = (ws / "work" / "other-thing" / "initiative.md").read_text()
    by_intake = (ws / "work" / "fix-the-thing" / "initiative.md").read_text()
    assert "repo: ~/clone\n" in by_repo
    assert "repo: ~/clone\n" in by_intake
    [archived] = (ws / "intake" / "done").glob("*.md")
    assert "repo: ~/clone\n" in archived.read_text()


def test_a_clone_that_is_not_a_checkout_is_refused_naming_the_clone(tmp_path, capsys):
    profile, ws, umbrella, clone, _ = _setup(tmp_path, with_clone=True, clone_is_checkout=False)
    assert _file(profile, umbrella) == 2
    first = capsys.readouterr().out
    assert main(["route", "file", "--profile", str(profile), "--from-intake", str(_intake(ws, umbrella))]) == 2
    second = capsys.readouterr().out
    assert first == second == f"route file: refusing, {clone} is not a git checkout on this machine\n"


def test_an_intake_naming_the_umbrella_without_a_clone_is_refused_and_kept(tmp_path, capsys):
    profile, ws, umbrella, _, _ = _setup(tmp_path, with_clone=False)
    intake = _intake(ws, umbrella)
    assert main(["route", "file", "--profile", str(profile), "--from-intake", str(intake)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("routing: ") and "umbrella_clone_dir" in out
    assert _written(ws) == [intake]


def test_a_repo_that_is_not_the_umbrella_is_recorded_as_before(tmp_path):
    profile, ws, _, _, other = _setup(tmp_path, with_clone=True)
    assert _file(profile, other) == 0
    [initiative] = ws.glob("work/*/initiative.md")
    assert f"repo: {other}\n" in initiative.read_text()
