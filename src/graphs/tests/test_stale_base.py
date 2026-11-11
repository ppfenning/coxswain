from harness.stale_base import (
    WRONG_REPOSITORY_PREFIX,
    is_patch_empty,
    missing_paths,
    stale_base_reason,
    ticket_paths,
    wrong_repository,
    wrong_repository_reason,
)


def test_a_plain_surface_is_named():
    assert ticket_paths(["src/a/mod.py"], "") == ("src/a/mod.py",)


def test_a_new_surface_is_not_named():
    assert ticket_paths(["src/a/mod.py", "src/a/fresh.py (new)"], "") == ("src/a/mod.py",)


def test_a_body_path_with_directory_and_extension_is_named():
    body = "Edit `src/b/other.py` and read README, docs/notes.md."
    assert ticket_paths([], body) == ("docs/notes.md", "src/b/other.py")


def test_a_duplicate_across_surface_and_body_is_dropped():
    assert ticket_paths(["src/a/mod.py"], "see src/a/mod.py") == ("src/a/mod.py",)


def test_absolute_paths_and_bare_names_are_dropped():
    body = "run /srv/repo/src/x.py then setup.py, src/noext and https://example.org/a/b.html"
    assert ticket_paths(["/srv/repo/src/y.py"], body) == ()


def test_a_body_path_marked_new_is_not_named():
    assert ticket_paths([], "add src/c/fresh.py (new) beside src/c/old.py") == ("src/c/old.py",)


def test_missing_paths_lists_the_absent_ones_sorted():
    tree = {"src/a/mod.py", "src/b/other.py"}
    assert missing_paths(["src/z.py", "src/a/mod.py", "src/b/gone.py"], tree) == ("src/b/gone.py", "src/z.py")


def test_a_directory_prefix_of_a_tree_path_is_present():
    assert missing_paths(["src/a", "src/q"], {"src/a/mod.py"}) == ("src/q",)


def test_patch_empty_is_true():
    assert is_patch_empty("node 'build' patch_empty: the scratch tree has no changes to show for it") is True
    assert is_patch_empty("patch_empty") is True


def test_patch_outside_scratch_is_false():
    msg = "node 'build' patch_outside_scratch: the reported patch is not in the scratch tree"
    assert is_patch_empty(msg) is False


def test_an_unrelated_failure_is_not_patch_empty():
    assert is_patch_empty("node 'build' failed in claude: error_max_turns") is False


def test_the_reason_names_every_missing_path_sorted():
    assert stale_base_reason("t1", ["src/z.py", "src/a.py"]) == (
        "t1: the build base predates files the ticket needs: src/a.py, src/z.py"
    )


def test_every_missing_path_in_one_other_repository_gives_its_key():
    assert wrong_repository(("src/ui/a.rs", "themes/x.toml"), "coxswain") == "coxswain-dash"


def test_a_mix_of_foreign_and_own_paths_gives_none():
    assert wrong_repository(("src/ui/a.rs", "src/tools/b.py"), "coxswain") is None


def test_a_path_the_layout_cannot_place_gives_none():
    assert wrong_repository(("src/ui/a.rs", "docs/x.md"), "coxswain") is None


def test_the_wrong_repository_reason_starts_with_the_prefix_and_names_task_repo_and_paths():
    reason = wrong_repository_reason("t1", "coxswain-dash", ["src/ui/z.rs", "src/ui/a.rs"])
    assert reason.startswith(WRONG_REPOSITORY_PREFIX)
    assert reason.startswith("wrong repository: t1 needs src/ui/a.rs, src/ui/z.rs")
    assert "initiative for coxswain-dash" in reason
