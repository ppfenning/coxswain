import os
import subprocess
import sys
from pathlib import Path

from agent_tools.chair_decompose_runs import live_decompose_initiatives


def _dead_pid() -> int:
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(done.stdout)


def test_live_pid_gives_its_initiative(tmp_path: Path):
    (tmp_path / "requeue-x-2.pid").write_text(str(os.getpid()))
    assert live_decompose_initiatives(tmp_path) == frozenset({"requeue-x"})


def test_dead_pid_gives_nothing(tmp_path: Path):
    (tmp_path / "idea-1.pid").write_text(str(_dead_pid()))
    assert live_decompose_initiatives(tmp_path) == frozenset()


def test_garbage_pid_file_gives_nothing(tmp_path: Path):
    (tmp_path / "idea-3.pid").write_text("not-a-pid\n")
    assert live_decompose_initiatives(tmp_path) == frozenset()


def test_absent_runs_dir_gives_empty_set(tmp_path: Path):
    assert live_decompose_initiatives(tmp_path / "absent") == frozenset()
