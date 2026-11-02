import pytest

from agent_tools.repo_path import git_argv

HOME = "/".join(["", "home", "alice"])
LINUX_OTHER = "/".join(["", "home", "bob"])
MAC_OTHER = "/".join(["", "Users", "carol"])
REPO = "/".join(["repos", "widget"])


def test_git_argv_expands_tilde_repo_under_given_home():
    assert git_argv("~/" + REPO, HOME) == ["git", "-C", HOME + "/" + REPO]


def test_git_argv_leaves_plain_absolute_path_unchanged():
    path = "/".join(["", "srv", "git", "widget"])
    assert git_argv(path, HOME) == ["git", "-C", path]


@pytest.mark.parametrize("other", [LINUX_OTHER, MAC_OTHER])
def test_git_argv_reroots_another_machines_home(other):
    assert git_argv(other + "/" + REPO, HOME) == ["git", "-C", HOME + "/" + REPO]


def test_git_argv_appends_args_after_repository_in_order():
    assert git_argv("~/" + REPO, HOME, "status", "--short") == [
        "git",
        "-C",
        HOME + "/" + REPO,
        "status",
        "--short",
    ]
