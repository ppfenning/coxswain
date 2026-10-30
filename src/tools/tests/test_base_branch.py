import subprocess

import pytest

from agent_tools.base_branch import BaseMissing, base_ref, branch_name, repo_base, resolve_base


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_order_is_flag_then_setting_then_origin_head_then_main():
    assert resolve_base("f", "s", "h") == "f"
    assert resolve_base(None, "s", "h") == "s"
    assert resolve_base(None, None, "h") == "h"
    assert resolve_base(None, None, None) == "main"


def test_repo_base_reads_path_or_directory_name():
    assert repo_base({"repo_base": {"widget": "develop"}}, "/x/widget") == "develop"
    assert repo_base({}, "/x/widget") is None


def origin_with_develop(tmp_path):
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "develop", str(origin)], check=True)
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


def test_origin_head_names_develop_and_base_ref_is_origin_develop(tmp_path):
    clone = origin_with_develop(tmp_path)
    assert branch_name(str(clone)) == "develop"
    assert base_ref(str(clone)) == "origin/develop"


def test_missing_origin_branch_refuses_with_an_origin(tmp_path):
    clone = origin_with_develop(tmp_path)
    with pytest.raises(BaseMissing, match=f"runs land: origin/typo not found in {clone}"):
        base_ref(str(clone), "typo")


def test_the_remote_launch_sync_follows_origin_head_and_the_base_setting(tmp_path):
    from agent_tools import remote_launch

    clone = origin_with_develop(tmp_path)
    base = remote_launch._chair_base(str(clone))
    assert (base.branch, len(base.sha)) == ("develop", 40)
    assert "origin/typo does not resolve" in remote_launch._chair_base(str(clone), "typo").message


def test_no_origin_returns_the_bare_name(tmp_path):
    repo = tmp_path / "bare"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    assert base_ref(str(repo)) == "main"
    assert base_ref(str(repo), "develop") == "develop"
