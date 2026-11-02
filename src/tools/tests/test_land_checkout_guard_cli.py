import subprocess

import pytest

from agent_tools import cli

RUN = "demo-1"
NOTICE = "land: This is an install checkout: the land moves the code the chair runs from."


class _PastGuard(Exception):
    pass


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(path):
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    return path


def _argv(tmp_path, repo, umbrella, clone, *extra):
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"umbrella_dir: {umbrella}\numbrella_clone_dir: {clone}\n", encoding="utf-8")
    return ["runs", "land", RUN, "--repo", str(repo), "--profile", str(profile), "--runs-dir", str(runs), *extra]


@pytest.fixture(autouse=True)
def _stop_after_guard(monkeypatch):
    def stop(*args, **kwargs):
        raise _PastGuard

    monkeypatch.setattr(cli.base_branch, "base_ref", stop)


@pytest.mark.parametrize("extra", [(), ("--apply",)])
def test_a_detached_checkout_refuses_for_dry_runs_and_apply(tmp_path, capsys, extra):
    repo = _repo(tmp_path / "work")
    _git(repo, "checkout", "-q", "--detach")
    assert cli.main(_argv(tmp_path, repo, tmp_path / "install", tmp_path / "clone", *extra)) == 2
    out = capsys.readouterr().out
    assert "land: refusing, This checkout is on a detached HEAD." in out


def test_the_install_checkout_detached_at_a_tag_refuses_and_names_the_clone(tmp_path, capsys):
    install, clone = _repo(tmp_path / "install"), tmp_path / "clone"
    _git(install, "tag", "v1")
    _git(install, "checkout", "-q", "v1")
    assert cli.main(_argv(tmp_path, install, install, clone)) == 2
    out = capsys.readouterr().out
    assert f"land: refusing, This is a release install checkout on a detached HEAD. Run the land from the working clone {clone}." in out


def test_the_install_checkout_on_a_branch_prints_the_notice_once_and_goes_on(tmp_path, capsys):
    install = _repo(tmp_path / "install")
    with pytest.raises(_PastGuard):
        cli.main(_argv(tmp_path, install, install, tmp_path / "clone"))
    assert capsys.readouterr().out.splitlines().count(NOTICE) == 1


def test_an_ordinary_clone_is_untouched(tmp_path, capsys):
    repo = _repo(tmp_path / "work")
    with pytest.raises(_PastGuard):
        cli.main(_argv(tmp_path, repo, tmp_path / "install", tmp_path / "clone"))
    assert capsys.readouterr().out == ""
