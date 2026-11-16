from pathlib import Path

from harness.lane_pytest_tmp import lane_scratch_dir, lane_tmp_env, run_scratch_root

LONG_RUN = Path("/home/user/worktrees/hardening-h5-edge-and-deploy-cannot-wedge-a-9")
LONG_TASK = "hardening-h5-edge-and-deploy-cannot-wedge-a-unit-env"


def test_the_environment_gets_tmpdir_and_no_pytest_option():
    assert lane_tmp_env(Path("/s")) == {"TMPDIR": "/s"}


def test_the_scratch_dir_is_under_the_run_dir():
    scratch = lane_scratch_dir(Path("/r"), "p1", "t1")
    assert scratch.parent == Path("/r/pytest-tmp")


def test_two_phase_task_pairs_get_two_scratch_dirs_and_one_pair_always_one():
    assert lane_scratch_dir(Path("/r"), "p1", "t1") == lane_scratch_dir(Path("/r"), "p1", "t1")
    assert lane_scratch_dir(Path("/r"), "p1", "t1") != lane_scratch_dir(Path("/r"), "p1", "t2")
    assert lane_scratch_dir(Path("/r"), "p1", "t1") != lane_scratch_dir(Path("/r"), "p2", "t1")


def test_the_run_scratch_root_is_under_the_temp_root():
    root = run_scratch_root(Path("/tmp"), Path("/home/user/worktrees/epic-1"))
    assert root.parent == Path("/tmp")
    assert root.name.startswith("lane-")


def test_one_run_dir_always_gets_one_root():
    run_dir = Path("/home/user/worktrees/epic-1")
    assert run_scratch_root(Path("/tmp"), run_dir) == run_scratch_root(Path("/tmp"), run_dir)


def test_one_run_id_under_two_worktree_roots_gets_two_roots():
    one = run_scratch_root(Path("/tmp"), Path("/home/user/worktrees/epic-1"))
    other = run_scratch_root(Path("/tmp"), Path("/srv/worktrees/epic-1"))
    assert one != other


def test_the_scratch_path_is_short_and_names_no_coxswain_whatever_the_run_and_task_ids():
    """A suite's temp paths sit below this one: a long run id or task id must not lengthen it, and no test of a
    suite that names coxswain may find `coxswain-` in its own temp paths."""
    temp_root = Path("/private/var/folders/xx/0000000000000000000000000000gn/T")
    scratch = lane_scratch_dir(run_scratch_root(temp_root, LONG_RUN), "p1", LONG_TASK)
    assert len(str(scratch)) - len(str(temp_root)) <= 48
    assert "coxswain" not in str(scratch.relative_to(temp_root))
