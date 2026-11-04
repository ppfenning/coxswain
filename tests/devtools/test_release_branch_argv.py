"""Release-branch argv builders and ref parsers: one literal call per helper."""
from devtools import release

SHA = "a" * 40
PEELED = "b" * 40


def test_release_branch_push_argv_pushes_the_sha_to_the_remote_branch():
    assert release.release_branch_push_argv("/c", SHA, "release/1.0") == [
        "git", "-C", "/c", "push", "origin", f"{SHA}:refs/heads/release/1.0"]


def test_tag_argv_without_ref_is_unchanged_and_with_ref_tags_it():
    base = ["git", "-C", "/c", "tag", "-a", "v1.0", "-m", "coxswain 1.0"]
    assert release.tag_argv("/c", "1.0") == base
    assert release.tag_argv("/c", "1.0", ref=SHA) == [*base, SHA]


def test_pr_create_argv_adds_base_and_head_only_when_given():
    base = ["gh", "pr", "create", "--title", "t", "--body", "b"]
    assert release.pr_create_argv("t", "b") == base
    assert release.pr_create_argv("t", "b", base="release/1.0", head="release/1.0-bump") == [
        *base, "--base", "release/1.0", "--head", "release/1.0-bump"]


def test_pr_checks_argv_names_the_head_only_when_given():
    assert release.pr_checks_argv() == ["gh", "pr", "checks", "--watch", "--fail-fast"]
    assert release.pr_checks_argv("h") == ["gh", "pr", "checks", "h", "--watch", "--fail-fast"]


def test_pr_merge_argv_defaults_to_squash_and_always_deletes_the_branch():
    assert release.pr_merge_argv() == ["gh", "pr", "merge", "--squash", "--delete-branch"]
    assert release.pr_merge_argv("h", method="merge") == ["gh", "pr", "merge", "h", "--merge", "--delete-branch"]


def test_checkout_tracking_argv_resets_the_local_branch_to_origin():
    assert release.checkout_tracking_argv("/c", "release/1.0") == [
        "git", "-C", "/c", "checkout", "-B", "release/1.0", "origin/release/1.0"]


def test_ls_remote_ref_argv_lists_the_ref_and_its_peeled_form():
    assert release.ls_remote_ref_argv("/c", "refs/tags/v1.0") == [
        "git", "-C", "/c", "ls-remote", "origin", "refs/tags/v1.0", "refs/tags/v1.0^{}"]


def test_parse_ls_remote_ref_reads_a_branch_line():
    assert release.parse_ls_remote_ref(f"{SHA}\trefs/heads/release/1.0\n", "refs/heads/release/1.0") == SHA


def test_parse_ls_remote_ref_prefers_the_peeled_commit_of_an_annotated_tag():
    text = f"{SHA}\trefs/tags/v1.0\n{PEELED}\trefs/tags/v1.0^{{}}\n"
    assert release.parse_ls_remote_ref(text, "refs/tags/v1.0") == PEELED


def test_parse_ls_remote_ref_is_none_when_the_ref_is_absent_or_only_a_longer_name():
    assert release.parse_ls_remote_ref("", "refs/tags/v1") is None
    assert release.parse_ls_remote_ref(f"{SHA}\trefs/tags/v10\n", "refs/tags/v1") is None


def test_is_ancestor_argv_is_the_merge_base_ancestor_test():
    assert release.is_ancestor_argv("/c", "x", "y") == ["git", "-C", "/c", "merge-base", "--is-ancestor", "x", "y"]


def test_show_file_argv_prints_the_file_at_the_ref():
    assert release.show_file_argv("/c", "origin/main", "manifest.toml") == [
        "git", "-C", "/c", "show", "origin/main:manifest.toml"]


def test_merge_base_argv_prints_the_merge_base_of_two_refs():
    assert release.merge_base_argv("/c", "x", "y") == ["git", "-C", "/c", "merge-base", "x", "y"]


def test_branch_names_keep_the_bump_head_distinct_from_the_release_base():
    assert release.release_branch_name("2026.11.0") == "release/2026.11.0"
    assert release.bump_branch_name("2026.11.0") == "release/2026.11.0-bump"
