"""The lane python rule, at the pure decider with literal inputs."""

from pathlib import Path

from agent_tools import lane_python
from agent_tools.lane_python import LanePython, LaneRefusal

ROOT = Path("/h/installs/coxswain")
VENV_PY = ROOT / ".venv" / "bin" / "python"
RUNNING = Path("/usr/local/bin/python3")


def test_candidates_are_running_then_install_venv():
    assert lane_python.candidates(RUNNING, ROOT) == [RUNNING, VENV_PY]


def test_candidates_are_only_running_without_an_install_root():
    assert lane_python.candidates(RUNNING, None) == [RUNNING]


def test_existing_venv_candidate_puts_its_bin_dir_first_on_the_path():
    got = lane_python.resolve_lane_python(RUNNING, ROOT, "/usr/bin:/bin", {VENV_PY}, host="local")
    assert got == LanePython(ROOT / ".venv" / "bin", "/h/installs/coxswain/.venv/bin:/usr/bin:/bin")


def test_running_interpreter_wins_when_both_exist():
    got = lane_python.resolve_lane_python(RUNNING, ROOT, "/bin", {RUNNING, VENV_PY}, host="local")
    assert got == LanePython(Path("/usr/local/bin"), "/usr/local/bin:/bin")


def test_neither_candidate_present_refuses_listing_both_paths_in_order():
    got = lane_python.resolve_lane_python(RUNNING, ROOT, "/bin", set(), host="box")
    assert got == LaneRefusal(
        "box: python not on the lane PATH (tried /usr/local/bin/python3, /h/installs/coxswain/.venv/bin/python)"
    )


def test_directory_already_on_the_path_is_moved_to_the_front_once():
    got = lane_python.resolve_lane_python(RUNNING, None, "/bin:/usr/local/bin:/usr/bin", {RUNNING}, host="local")
    assert got == LanePython(Path("/usr/local/bin"), "/usr/local/bin:/bin:/usr/bin")
