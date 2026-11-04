from pathlib import Path

import pytest

from agent_tools.chair_land_proc import (
    LandOutcome,
    LandSpec,
    dead_outcome,
    encode_outcome,
    encode_spec,
    land_key,
    parse_spec,
    paths_for,
)
from agent_tools.chair_land_sink import ProcessLands
from agent_tools.ci_gate import CiGate


class Fallback:
    def __init__(self, landing: list[dict[str, str]] | None = None, results: list[dict] | None = None) -> None:
        self.gates: list[CiGate] = []
        self.submitted: list[dict] = []
        self.stopped = 0
        self._landing = landing or []
        self._results = results or []

    def set_gate(self, gate: CiGate) -> None:
        self.gates.append(gate)

    def pending(self, repo: str) -> bool:
        return any(d["repo"] == repo for d in self._landing)

    def landing(self) -> list[dict[str, str]]:
        return self._landing

    def submit(self, action: dict, work) -> dict:
        self.submitted.append(action)
        return {"action": action, "status": "in_progress", "reason": "thread"}

    def collect(self) -> list[dict]:
        return self._results

    def stop(self) -> None:
        self.stopped += 1


class Rig:
    """Fakes for every edge of a ProcessLands over one state directory."""

    def __init__(self, state_dir: Path, limit: int = 1, host: str = "h") -> None:
        self.state_dir = state_dir
        self.spawned: list[list[str]] = []
        self.live: set[int] = set()
        self.clock = 1000.0
        self.fallback = Fallback()
        self.limit = limit
        self.host = host

    def _spawn(self, argv: list[str]) -> int:
        self.spawned.append(argv)
        pid = 100 + len(self.spawned)
        self.live.add(pid)
        return pid

    def sink(self, alive=None) -> ProcessLands:
        return ProcessLands(
            self.state_dir,
            self.fallback,
            lambda action, code, output: {
                "action": action,
                "status": "done" if code == 0 else "failed",
                "reason": output,
            },
            lambda repo: self.limit,
            self._spawn,
            alive or (lambda pid: pid in self.live),
            self.host,
            lambda: self.clock,
        )


def _action(task: str = "t1", repo: str = "r") -> dict:
    return {"kind": "land", "run": "run1", "task_id": task, "repo": repo, "initiative": "ini", "phase": "p1"}


def _no_alive(pid: int) -> bool:
    raise AssertionError(f"alive({pid}) must not be called")


def test_start_writes_a_spec_and_spawns_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    result = rig.sink().start(_action(), ["cox", "runs", "land"])
    spec_path, _ = paths_for(tmp_path, land_key(_action()))
    spec = parse_spec(spec_path.read_text())
    assert result["status"] == "in_progress"
    assert len(rig.spawned) == 1
    assert rig.spawned[0][1:] == ["-m", "agent_tools.chair_land_proc", str(spec_path)]
    assert spec is not None
    assert (spec.pid, spec.host, spec.repo, spec.argv, spec.started_at) == (
        101,
        "h",
        "r",
        ["cox", "runs", "land"],
        1000.0,
    )


def test_repeat_start_does_not_spawn_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    sink = rig.sink()
    sink.start(_action(), ["x"])
    again = sink.start(_action(), ["x"])
    assert again["status"] == "in_progress"
    assert len(rig.spawned) == 1


def test_a_third_land_in_one_repo_reports_full_at_a_limit_of_two(tmp_path: Path) -> None:
    rig = Rig(tmp_path, limit=2)
    sink = rig.sink()
    sink.start(_action("t1"), ["x"])
    assert not sink.full("r")
    sink.start(_action("t2"), ["x"])
    assert len(rig.spawned) == 2
    assert sink.full("r")
    assert not sink.full("other")


def test_a_paused_gate_spawns_nothing_and_writes_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "state")
    sink = rig.sink()
    sink.set_gate(CiGate(True, "incident"))
    result = sink.start(_action(), ["x"])
    assert result["status"] == "in_progress"
    assert "held" in result["reason"]
    assert rig.spawned == []
    assert not (tmp_path / "state").exists()
    assert rig.fallback.gates == [CiGate(True, "incident")]


def test_collect_returns_a_written_outcome_once_and_removes_the_files(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    sink = rig.sink()
    sink.start(_action(), ["x"])
    spec_path, outcome_path = paths_for(tmp_path, land_key(_action()))
    assert sink.collect() == []  # the process is alive and has written nothing
    outcome_path.write_text(encode_outcome(LandOutcome(0, "merged")))
    assert sink.collect() == [{"action": _action(), "status": "done", "reason": "merged"}]
    assert not spec_path.exists()
    assert not outcome_path.exists()
    assert sink.collect() == []
    assert not sink.pending("r")


def test_a_non_zero_outcome_is_returned_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    sink = rig.sink()
    sink.start(_action(), ["x"])
    _, outcome_path = paths_for(tmp_path, land_key(_action()))
    outcome_path.write_text(encode_outcome(LandOutcome(3, "refused")))
    assert [r["status"] for r in sink.collect()] == ["failed"]
    assert sink.collect() == []


def test_a_dead_pid_with_no_outcome_yields_the_dead_outcome_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    sink = rig.sink()
    sink.start(_action(), ["x"])
    rig.live.clear()
    spec = parse_spec(paths_for(tmp_path, land_key(_action()))[0].read_text())
    assert spec is not None
    assert sink.collect() == [{"action": _action(), "status": "failed", "reason": dead_outcome(spec, 101).output}]
    assert sink.collect() == []


def test_a_spec_from_another_host_is_not_judged_dead_here(tmp_path: Path) -> None:
    rig = Rig(tmp_path, host="here")
    spec = LandSpec(land_key(_action()), _action(), ["x"], "r", 60.0, 1000.0, "elsewhere", 555)
    paths_for(tmp_path, spec.key)[0].write_text(encode_spec(spec))
    assert rig.sink(alive=_no_alive).collect() == []


@pytest.mark.parametrize("pid", [0, -1])
def test_a_spec_with_no_recorded_pid_survives_before_its_timeout_and_settles_once_after_it(
    tmp_path: Path, pid: int
) -> None:
    rig = Rig(tmp_path)
    sink = rig.sink(alive=_no_alive)
    spec = LandSpec(land_key(_action()), _action(), ["x"], "r", 100.0, 1000.0, "h", pid)
    spec_path, outcome_path = paths_for(tmp_path, spec.key)
    spec_path.write_text(encode_spec(spec))
    rig.clock = 1100.0  # exactly the timeout is not past it
    assert sink.collect() == []
    assert spec_path.exists()
    assert sink.pending("r")
    rig.clock = 1100.5
    assert sink.collect() == [{"action": _action(), "status": "failed", "reason": dead_outcome(spec, pid).output}]
    assert not spec_path.exists()
    assert not outcome_path.exists()
    assert sink.collect() == []


def test_a_spec_with_no_pid_still_reports_an_outcome_it_has_written(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    spec = LandSpec(land_key(_action()), _action(), ["x"], "r", 100.0, 1000.0, "h", 0)
    spec_path, outcome_path = paths_for(tmp_path, spec.key)
    spec_path.write_text(encode_spec(spec))
    outcome_path.write_text(encode_outcome(LandOutcome(0, "merged")))
    assert [r["reason"] for r in rig.sink(alive=_no_alive).collect()] == ["merged"]


def test_counts_include_a_spec_written_by_another_instance(tmp_path: Path) -> None:
    first = Rig(tmp_path, limit=2)
    first.sink().start(_action("t1"), ["x"])
    second = Rig(tmp_path, limit=2)
    sink = second.sink()
    assert sink.pending("r")
    assert not sink.full("r")
    sink.start(_action("t2"), ["x"])
    assert sink.full("r")
    assert sink.landing() == [{"initiative": "ini", "phase": "p1", "repo": "r"}] * 2


def test_counts_include_the_fallback(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.fallback = Fallback(landing=[{"initiative": "i", "phase": "q", "repo": "r"}])
    sink = rig.sink()
    assert sink.full("r")
    assert sink.landing() == [{"initiative": "i", "phase": "q", "repo": "r"}]


def test_the_fallback_gets_submit_stop_and_its_results_and_its_lands_are_held_here(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    held = [{"initiative": "i", "phase": "q", "repo": "r"}]
    rig.fallback = Fallback(landing=held, results=[{"action": {}, "status": "done", "reason": "thread"}])
    sink = rig.sink()
    sink.start(_action(), ["x"])
    assert sink.submit(_action("t9"), lambda: {})["reason"] == "thread"
    sink.stop()
    assert rig.fallback.submitted == [_action("t9")]
    assert rig.fallback.stopped == 1
    assert sink.held_here() == held
    assert sink.collect() == rig.fallback.collect()
