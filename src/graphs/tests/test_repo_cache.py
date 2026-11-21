"""The worker caches' pure core: where each cache lives, what a lane's environment carries, the argv a mirror and a
clone run, and the decisions about a mirror. No git runs here."""

from __future__ import annotations

from pathlib import Path

from harness import bounded_proc
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


def test_mirror_and_lock_paths_sit_under_the_root():
    root = Path("/c")
    mirror, lock = rc.mirror_dir(root, "https://f/o/app"), rc.lock_file(root, "https://f/o/app")
    assert mirror.parent == lock.parent == root / "git"
    assert mirror.name.endswith(".git") and lock.name.endswith(".lock") and mirror.stem == lock.stem


def test_mirror_step():
    assert rc.mirror_step(exists=False, bare=False) == "create"
    assert rc.mirror_step(exists=True, bare=False) == "rebuild"
    assert rc.mirror_step(exists=True, bare=True) == "refresh"


def test_the_mirror_fetches_branches_and_tags_forced_and_prunes_with_gc_inside_the_lock():
    argv = rc.mirror_fetch_argv(Path("/c/git/m.git"), "https://f/o/app")
    assert argv[:3] == ["git", "-C", "/c/git/m.git"]
    assert argv[-3:] == ["https://f/o/app", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    assert "--prune" in argv and "gc.autoDetach=false" in argv
    assert not any("refs/pull" in part for part in argv)


def test_the_mirror_fetch_and_the_clone_get_the_slow_remote_bound():
    # bounded_proc reads the subcommand past `-C` and `-c` only: the argv must name `fetch` where it looks.
    fetch = rc.mirror_fetch_argv(Path("/c/git/m.git"), "https://f/o/app")
    assert bounded_proc.git_subcommand(fetch) == "fetch"
    assert bounded_proc.default_timeout(fetch) == bounded_proc.GIT_SLOW_TIMEOUT_SECONDS
    clone = rc.clone_argv("u", Path("/d"), Path("/m"), Path("/t"))
    assert bounded_proc.default_timeout(clone) == bounded_proc.GIT_SLOW_TIMEOUT_SECONDS


def test_clone_argv_borrows_only_when_given_a_reference():
    assert rc.clone_argv("u", Path("/d"), Path("/m"), Path("/t")) == [
        "git", "clone", "--quiet", "--no-checkout", "--no-local", "--template", "/t", "--reference", "/m", "u", "/d"]
    assert rc.clone_argv("u", Path("/d"), None, Path("/t")) == [
        "git", "clone", "--quiet", "--no-checkout", "--no-local", "--template", "/t", "u", "/d"]


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
    assert rc.discard_mirror(borrowed_ok=False, plain_ok=True, mirror_sound=False)
    assert not rc.discard_mirror(borrowed_ok=False, plain_ok=True, mirror_sound=True)  # a local failure: keep it
    assert not rc.discard_mirror(borrowed_ok=False, plain_ok=False, mirror_sound=False)  # the remote is down
    assert not rc.discard_mirror(borrowed_ok=True, plain_ok=True, mirror_sound=False)


def test_clone_state_counts_only_a_finished_clone_as_present():
    # (holds_git, in_progress, origin_is_url, borrowing, head_ok, has_index)
    assert rc.clone_state(False, False, False, False, False, False) == "absent"
    assert rc.clone_state(True, False, True, False, True, True) == "present"
    assert rc.clone_state(True, True, True, True, True, False) == "broken"  # ours, killed between clone and repack
    assert rc.clone_state(True, True, True, False, False, False) == "broken"  # ours, killed inside the clone
    assert rc.clone_state(True, True, False, False, False, False) == "broken"  # ours before git wrote the origin


def test_only_our_mark_lets_a_destination_be_cleared():
    # An operator's `git clone --shared` of the same origin: borrowing, maybe dirty, never marked. Refused.
    assert rc.clone_state(True, False, True, True, True, True) == "foreign"
    assert rc.clone_state(True, False, True, False, True, False) == "foreign"  # not checked out, not ours
    assert rc.clone_state(True, False, False, False, True, True) == "foreign"  # another remote


def test_dead_clones():
    assert rc.dead_clones({"a-1": True, "b-2": False, "a-3": False}) == ["a-3", "b-2"]
    assert rc.dead_clones({}) == []


def test_trace_line_names_both_costs():
    line = rc.trace_line("u", "refreshed", 3, 0, True)
    assert line == "clone u: mirror refreshed, mirror fetched 3 object(s); borrowed from the mirror, received 0 object(s)"
    assert "plain clone" in rc.trace_line("u", "unavailable", 0, 9, False)


def test_a_lanes_own_clone_sits_under_its_run_beside_its_worktrees():
    assert rc.lane_clone_dir(Path("/w"), "ship-p1-3") == Path("/w/ship-p1-3/_repo")


def test_absolute_url_resolves_only_a_relative_path_remote():
    assert rc.absolute_url("../origin.git", Path("/src/app")) == "/src/origin.git"
    for kept in ("/srv/origin.git", "file:///srv/o.git", "https://forge/o/app.git", "git@forge:o/app.git"):
        assert rc.absolute_url(kept, Path("/src/app")) == kept
