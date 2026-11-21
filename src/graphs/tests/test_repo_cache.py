"""The worker caches' pure core: where each cache lives, what a lane's environment carries, the argv a mirror and a
clone run, and the decisions about a mirror. No git runs here."""

from __future__ import annotations

from pathlib import Path

from harness import repo_cache as rc


def test_cache_root_prefers_the_explicit_dir_then_xdg_then_home():
    assert rc.cache_root({"COX_CACHE_DIR": "/c", "XDG_CACHE_HOME": "/x"}, "/h") == Path("/c")
    assert rc.cache_root({"XDG_CACHE_HOME": "/x"}, "/h") == Path("/x/coxswain")
    assert rc.cache_root({"COX_CACHE_DIR": "", "XDG_CACHE_HOME": ""}, "/h") == Path("/h/.cache/coxswain")


def test_one_repository_spelled_three_ways_shares_one_mirror():
    names = {rc.mirror_name(u) for u in ("https://forge/o/app", "https://forge/o/app/", "https://forge/o/app.git")}
    assert len(names) == 1
    assert names.pop().startswith("app-")


def test_distinct_repositories_with_one_name_get_distinct_mirrors():
    assert rc.mirror_name("https://a/o/app.git") != rc.mirror_name("https://b/o/app.git")


def test_a_mirror_name_is_one_safe_path_part():
    name = rc.mirror_name("git@forge:o/we ird:name..git")
    assert "/" not in name and " " not in name and not name.startswith(".")
    assert rc.mirror_name("file:///").startswith("repo-")


def test_mirror_lock_and_uv_cache_paths_sit_under_the_root():
    root = Path("/c")
    mirror, lock = rc.mirror_dir(root, "https://f/o/app"), rc.lock_file(root, "https://f/o/app")
    assert mirror.parent == lock.parent == root / "git"
    assert mirror.name.endswith(".git") and lock.name.endswith(".lock") and mirror.stem == lock.stem
    assert rc.uv_cache(root) == root / "uv"


def test_lane_env_names_the_cache_root_and_shares_a_uv_cache():
    env = rc.lane_env({"PATH": "/bin"}, Path("/c"))
    assert env == {"PATH": "/bin", "COX_CACHE_DIR": "/c", "UV_CACHE_DIR": "/c/uv"}


def test_lane_env_keeps_a_uv_cache_the_operator_chose_and_leaves_its_input_alone():
    given = {"UV_CACHE_DIR": "/mine"}
    assert rc.lane_env(given, Path("/c"))["UV_CACHE_DIR"] == "/mine"
    assert given == {"UV_CACHE_DIR": "/mine"}


def test_lane_env_is_idempotent():
    once = rc.lane_env({"PATH": "/bin"}, Path("/c"))
    assert rc.lane_env(once, Path("/c")) == once


def test_mirror_step():
    assert rc.mirror_step(exists=False, bare=False) == "create"
    assert rc.mirror_step(exists=True, bare=False) == "rebuild"
    assert rc.mirror_step(exists=True, bare=True) == "refresh"


def test_the_mirror_fetches_branches_and_tags_forced_and_prunes_with_gc_inside_the_lock():
    argv = rc.mirror_fetch_argv(Path("/c/git/m.git"), "https://f/o/app")
    assert argv[:3] == ["git", "--git-dir", "/c/git/m.git"]
    assert argv[-3:] == ["https://f/o/app", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    assert "--prune" in argv and "gc.autoDetach=false" in argv
    assert not any("refs/pull" in part for part in argv)


def test_clone_argv_borrows_only_when_given_a_reference():
    assert rc.clone_argv("u", Path("/d"), Path("/m")) == [
        "git", "clone", "--quiet", "--no-checkout", "--no-local", "--reference", "/m", "u", "/d"]
    assert rc.clone_argv("u", Path("/d"), None) == ["git", "clone", "--quiet", "--no-checkout", "--no-local", "u", "/d"]


def test_checkout_argv():
    head = ["git", "-C", "/d", "checkout", "--quiet", "--force"]
    assert rc.checkout_argv(Path("/d"), "lane/x", None) == [*head, "-B", "lane/x", "HEAD"]
    assert rc.checkout_argv(Path("/d"), "lane/x", "main") == [*head, "-B", "lane/x", "main"]
    assert rc.checkout_argv(Path("/d"), None, "v1") == [*head, "--detach", "v1"]
    assert rc.checkout_argv(Path("/d"), None, None) == head


def test_object_count_adds_loose_and_packed_and_ignores_the_rest():
    text = "count: 3\nsize: 12\nin-pack: 40\npacks: 1\nsize-pack: 9\nprune-packable: 0\ngarbage: 0\n"
    assert rc.object_count(text) == 43
    assert rc.object_count("") == 0


def test_a_mirror_is_discarded_only_when_it_failed_and_the_remote_worked():
    assert rc.discard_mirror(borrowed_ok=False, plain_ok=True)
    assert not rc.discard_mirror(borrowed_ok=False, plain_ok=False)
    assert not rc.discard_mirror(borrowed_ok=True, plain_ok=True)


def test_trace_line_names_both_costs():
    line = rc.trace_line("u", "refreshed", 3, 0, True)
    assert line == "clone u: mirror refreshed, mirror fetched 3 object(s); borrowed from the mirror, received 0 object(s)"
    assert "plain clone" in rc.trace_line("u", "unavailable", 0, 9, False)
