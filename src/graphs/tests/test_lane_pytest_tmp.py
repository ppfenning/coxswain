from pathlib import Path

from harness.lane_pytest_tmp import lane_scratch_dir, lane_tmp_env, run_scratch_root


def test_the_environment_gets_tmpdir_and_no_pytest_option():
    assert lane_tmp_env(Path("/s")) == {"TMPDIR": "/s"}


def test_the_scratch_dir_is_under_the_run_dir():
    assert lane_scratch_dir(Path("/r"), "p1", "t1") == Path("/r/pytest-tmp/p1--t1")


def test_the_run_scratch_root_is_under_the_temp_root_and_named_for_the_run():
    root = run_scratch_root(Path("/tmp"), Path("/home/u/worktrees/epic-1"))
    assert root.parent == Path("/tmp")
    assert root.name.startswith("coxswain-lane-epic-1-")


def test_one_run_dir_always_gets_one_root():
    run_dir = Path("/home/u/worktrees/epic-1")
    assert run_scratch_root(Path("/tmp"), run_dir) == run_scratch_root(Path("/tmp"), run_dir)


def test_one_run_id_under_two_worktree_roots_gets_two_roots():
    one = run_scratch_root(Path("/tmp"), Path("/home/u/worktrees/epic-1"))
    other = run_scratch_root(Path("/tmp"), Path("/srv/worktrees/epic-1"))
    assert one != other
