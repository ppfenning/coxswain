from agent_tools.chair_read_branch_merged import branch_merged, is_merged, patch_id

ROOT = "/repo"


def _show(sha: str) -> tuple[str, ...]:
    return ("-C", ROOT, "show", "--format=", "--no-color", "--no-ext-diff", "-U0", sha)


def _log(branch: str) -> tuple[str, ...]:
    return ("-C", ROOT, "rev-list", f"main..{branch}")


def _diff(index: str, line: int) -> str:
    return f"diff --git a/f b/f\nindex {index}\n--- a/f\n+++ b/f\n@@ -{line} +{line} @@\n-old\n+new\n"


def _runner(answers: dict[tuple[str, ...], tuple[int, str]]):
    return lambda argv: answers.get(tuple(argv), (128, ""))


def test_a_sha_on_the_branch_is_merged():
    assert is_merged("a1", "p1", [("b2", "p2"), ("a1", "p9")]) is True


def test_a_different_sha_with_the_same_patch_id_is_merged():
    assert is_merged("a1", "p1", [("b2", "p1")]) is True


def test_a_sha_on_neither_is_not_merged():
    assert is_merged("a1", "p1", [("b2", "p2")]) is False


def test_an_empty_patch_id_never_matches():
    assert is_merged("a1", "", [("b2", "")]) is False


def test_patch_id_ignores_index_lines_and_hunk_positions():
    assert patch_id(_diff("1..2", 3)) == patch_id(_diff("7..8", 40))


def test_a_cherry_picked_commit_is_merged_through_the_runner():
    runner = _runner({
        _show("a1"): (0, _diff("1..2", 3)),
        _log("phase"): (0, "b2\n"),
        _show("b2"): (0, _diff("7..8", 40)),
    })
    assert branch_merged(runner, ROOT, "a1", ["phase"]) is True


def test_a_missing_branch_is_not_merged():
    runner = _runner({_show("a1"): (0, _diff("1..2", 3))})
    assert branch_merged(runner, ROOT, "a1", ["pr/x"]) is False


def test_a_runner_failure_is_not_merged():
    assert branch_merged(lambda argv: (1, ""), ROOT, "a1", ["phase"]) is False


def test_an_empty_commit_or_repo_root_is_not_merged():
    assert branch_merged(lambda argv: (0, "x"), ROOT, "", ["phase"]) is False
    assert branch_merged(lambda argv: (0, "x"), "", "a1", ["phase"]) is False
