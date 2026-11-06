"""The chair carry's `.agent-checks` run with the lane cap: on 20 cores and capacity 5 a check sees 4 xdist workers."""
from __future__ import annotations

from pathlib import Path

from agent_tools import chair_exec

PRINT_WORKERS = 'printf "%s" "$PYTEST_XDIST_AUTO_NUM_WORKERS" > out.txt && exit 1'  # a failing line is listed back


def _edge(tmp_path: Path, capacity: int | None) -> chair_exec._GitEdge:
    edge = chair_exec._GitEdge(tmp_path, tmp_path, "/r", capacity=capacity)
    edge.tmp = str(tmp_path)
    (tmp_path / ".agent-checks").write_text(f"# a comment\n{PRINT_WORKERS}\n", encoding="utf-8")
    return edge


def test_carry_check_sees_four_workers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr(chair_exec.os, "cpu_count", lambda: 20)
    assert _edge(tmp_path, 5).run_checks() == [PRINT_WORKERS]
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "4"


def test_carry_check_without_capacity_inherits_the_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr(chair_exec.os, "cpu_count", lambda: 20)
    assert _edge(tmp_path, None).run_checks() == [PRINT_WORKERS]
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == ""


def test_run_argv_passes_env_to_the_child(tmp_path: Path) -> None:
    code, out = chair_exec.run_argv(["bash", "-c", 'printf "%s" "$X"'], env={"X": "y", "PATH": "/usr/bin:/bin"})
    assert (code, out) == (0, "y")


def test_edge_deps_hands_the_capacity_to_the_carry_edge(tmp_path: Path) -> None:
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "s", 1, run_id=lambda a: "", repo_for=lambda a: "/r", record=lambda a: None,
        host="h", harness_python="python", lane_capacity=5,
    )
    git, _, _ = deps.carry_ports({"kind": "carry"})
    assert git.capacity == 5
