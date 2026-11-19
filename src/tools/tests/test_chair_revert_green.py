from __future__ import annotations

import json

from agent_tools.chair_revert_green import main_green_since

GH = [
    "gh", "run", "list", "--repo", "o/r", "--branch", "main",
    "--json", "headSha,status,conclusion", "--limit", "50",
]  # fmt: skip


def _ancestor(commit: str, sha: str) -> list[str]:
    return ["git", "-C", "/r", "merge-base", "--is-ancestor", commit, sha]


def _run(sha: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"headSha": sha, "status": status, "conclusion": conclusion}


class Fake:
    def __init__(self, replies: dict[tuple[str, ...], tuple[int, str]]) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return self.replies.get(tuple(argv), (1, ""))


def _listed(*runs: dict) -> tuple[int, str]:
    return 0, json.dumps(list(runs))


def test_success_on_same_commit() -> None:
    runner = Fake({tuple(GH): _listed(_run("abc"))})
    assert main_green_since(runner, "/r", "o/r", "abc") is True
    assert runner.calls == [GH]


def test_success_on_descendant() -> None:
    runner = Fake(
        {
            tuple(GH): _listed(_run("older"), _run("newer")),
            tuple(_ancestor("abc", "older")): (1, ""),
            tuple(_ancestor("abc", "newer")): (0, ""),
        }
    )
    assert main_green_since(runner, "/r", "o/r", "abc") is True
    assert runner.calls == [GH, _ancestor("abc", "abc"), _ancestor("abc", "older"), _ancestor("abc", "newer")]


def test_failure_only_list() -> None:
    runner = Fake(
        {tuple(GH): _listed(_run("abc", conclusion="failure"), _run("def", status="in_progress", conclusion=None))}
    )
    assert main_green_since(runner, "/r", "o/r", "abc") is False
    assert runner.calls == [GH]


def test_gh_exit_nonzero() -> None:
    runner = Fake({tuple(GH): (1, json.dumps([_run("abc")]))})
    assert main_green_since(runner, "/r", "o/r", "abc") is False
    assert runner.calls == [GH]


def test_malformed_output() -> None:
    for output in ("not json", json.dumps({"headSha": "abc"}), ""):
        runner = Fake({tuple(GH): (0, output)})
        assert main_green_since(runner, "/r", "o/r", "abc") is False


def test_runner_raising_reads_as_false() -> None:
    def boom(argv: list[str]) -> tuple[int, str]:
        raise OSError("gh missing")

    assert main_green_since(boom, "/r", "o/r", "abc") is False


def test_a_tilde_repo_path_is_expanded_for_the_ancestor_check(monkeypatch) -> None:
    """A land watch carries `~/repos/x`; `git -C` gets no shell, so a literal tilde made every ancestor check fail."""
    monkeypatch.setenv("HOME", "/srv/op")
    ancestor = ["git", "-C", "/srv/op/repos/r", "merge-base", "--is-ancestor", "abc", "newer"]
    itself = ["git", "-C", "/srv/op/repos/r", "merge-base", "--is-ancestor", "abc", "abc"]
    runner = Fake({tuple(GH): _listed(_run("newer")), tuple(itself): (0, ""), tuple(ancestor): (0, "")})
    assert main_green_since(runner, "~/repos/r", "o/r", "abc") is True
    assert runner.calls == [GH, itself, ancestor]


def test_a_commit_git_cannot_find_costs_one_check_not_one_per_green_run() -> None:
    """Every ancestor check of an unknown commit fails (exit 128), so the answer is False after one, as after fifty."""
    runner = Fake({tuple(GH): _listed(*(_run(f"s{n}") for n in range(50))), tuple(_ancestor("abc", "abc")): (128, "")})
    assert main_green_since(runner, "/r", "o/r", "abc") is False
    assert runner.calls == [GH, _ancestor("abc", "abc")]


def test_a_failed_check_of_a_found_commit_still_asks_each_run() -> None:
    """Only the failure to find the commit stops the checks; a check that timed out (124) does not."""
    runner = Fake(
        {
            tuple(GH): _listed(_run("older"), _run("newer")),
            tuple(_ancestor("abc", "abc")): (124, ""),
            tuple(_ancestor("abc", "newer")): (0, ""),
        }
    )
    assert main_green_since(runner, "/r", "o/r", "abc") is True
    assert runner.calls[-1] == _ancestor("abc", "newer")


def test_kept_answers_are_not_asked_again_and_failures_are() -> None:
    """A second call with the same `known` asks gh again, and asks only what failed before: an answer never changes."""
    replies = {
        tuple(GH): _listed(_run("older"), _run("broken"), _run("newer")),
        tuple(_ancestor("abc", "abc")): (0, ""),
        tuple(_ancestor("abc", "older")): (1, ""),
        tuple(_ancestor("abc", "broken")): (124, ""),
    }
    known: dict[tuple[str, str, str], bool] = {}
    first, second = Fake(replies), Fake(replies)

    assert main_green_since(first, "/r", "o/r", "abc", known) is False
    assert main_green_since(second, "/r", "o/r", "abc", known) is False
    assert second.calls == [GH, _ancestor("abc", "broken")]
    assert known == {("/r", "abc", "abc"): True, ("/r", "abc", "older"): False, ("/r", "abc", "newer"): False}
