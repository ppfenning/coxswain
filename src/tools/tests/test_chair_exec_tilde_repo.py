"""A recorded repository that starts with a tilde is expanded for git that runs here, and left alone on a lane host."""

from pathlib import Path

import pytest

from agent_tools import chair_exec
from agent_tools.chair_exec import Deps, perform
from agent_tools.remote_argv import ssh_argv

RECORDED = "~/repos/app"
HOST_REPO = "repos/app"
SSH = "dev@h1"
PATTERN = "epic/alpha/*"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    return str(tmp_path)


def _deps(calls: list[list[str]], home: str) -> Deps:
    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "run-1",
        repo_for=lambda action: RECORDED,
        ssh_for=lambda host: SSH if host == "h1" else "",
        home=home,
    )


def _clear(**extra: object) -> dict:
    return {"kind": "clear_branches", "initiative": "alpha", "epoch": 1, "repo": RECORDED, **extra}


def test_the_prune_worktree_listing_receives_the_expanded_path(home: str) -> None:
    calls: list[list[str]] = []
    perform([_clear()], _deps(calls, home), lambda: 1, False)
    assert ["git", "-C", f"{home}/repos/app", "worktree", "list", "--porcelain"] in calls
    assert not any(RECORDED in argv for argv in calls)


def test_the_carried_sweep_listing_receives_the_expanded_path(home: str) -> None:
    calls: list[list[str]] = []
    perform([_clear(carry=["build"])], _deps(calls, home), lambda: 1, False)
    listing = ["git", "-C", f"{home}/repos/app", "for-each-ref", "--format=%(refname:short)", f"refs/heads/{PATTERN}"]
    assert listing in calls


def test_the_uncarried_delete_branches_receives_the_expanded_path(home: str, monkeypatch: pytest.MonkeyPatch) -> None:
    argvs: list[list[str]] = []

    def run_argv(argv: list[str], cwd: Path | None = None, timeout: float = 0) -> tuple[int, str]:
        argvs.append(argv)
        return 0, "epic/alpha/x\n"

    monkeypatch.setattr(chair_exec, "run_argv", run_argv)
    deps = chair_exec.edge_deps(
        Path(home),
        Path(home),
        "s",
        1,
        lambda action: "run-1",
        lambda action: RECORDED,
        lambda action: None,
        "h0",
        "python",
    )
    deleted, _ = chair_exec._swept_branches(deps, RECORDED, PATTERN, set())
    assert deleted == ["epic/alpha/x"]
    assert ["git", "-C", f"{home}/repos/app", "branch", "-D", "epic/alpha/x"] in argvs
    assert all(argv[2] == f"{home}/repos/app" for argv in argvs)


def test_an_ssh_wrapped_run_still_receives_the_home_relative_form(home: str) -> None:
    calls: list[list[str]] = []
    perform([_clear(host="h1")], _deps(calls, home), lambda: 1, False)
    assert ["git", "-C", f"{home}/repos/app", "worktree", "list", "--porcelain"] in calls
    assert ssh_argv(SSH, ["git", "-C", HOST_REPO, "worktree", "list", "--porcelain"]) in calls
    assert not any(home in word for argv in calls if argv[0] == "ssh" for word in argv)
