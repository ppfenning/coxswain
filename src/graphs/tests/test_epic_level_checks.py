"""run_epic runs `landing_areas.level_checks` once per assembled level, under the repo gate lock; task builds never see them."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from harness import check_reuse, epic
from harness.epic import _level_failure_reason, _PhaseRunner
from tests.test_epic_driver import cart, drive, git, repo  # noqa: F401
from tests.test_epic_waves import Gated, by_phase, work

LEVEL_CMD = "printf x >> level-ran.marker"


def level_logs(tmp_path: Path, phase: str) -> list[Path]:
    return sorted((tmp_path / "runs" / "epic-1" / "checks" / phase / "level").glob("*.log"))


class Spy:
    """Wraps the real `run_checks`; records each call's commands, and for level calls enter/exit markers and times."""

    def __init__(self, real) -> None:
        self.real, self.calls, self.markers, self.spans = real, [], [], {}
        self.lock = threading.Lock()

    def __call__(self, worktree, checks, **kw):
        level = str(kw.get("log_prefix", "")).endswith("/level/")
        cmds = [c["cmd"] for c in checks]
        phase = Path(worktree).name
        with self.lock:
            self.calls.append((phase, level, cmds))
            if level:
                self.markers.append(("enter", phase))
        started = time.monotonic()
        if level:
            time.sleep(0.3)
        try:
            return self.real(worktree, checks, **kw)
        finally:
            if level:
                with self.lock:
                    self.markers.append(("exit", phase))
                    self.spans[phase] = (started, time.monotonic())


def spy_on(monkeypatch) -> Spy:
    spy = Spy(epic.run_checks)
    monkeypatch.setattr(epic, "run_checks", spy)
    monkeypatch.setattr(check_reuse, "run_checks", spy)  # the task gate runs through the ledger
    return spy


def test_a_task_build_runs_only_task_checks_even_when_level_checks_is_set(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [{"name": "level", "cmd": LEVEL_CMD}]
    spy = spy_on(monkeypatch)
    task_cmd = cart["landing_areas"]["checks"][0]["cmd"]
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    task_calls = [cmds for _, level, cmds in spy.calls if not level]
    assert task_calls and all(cmds == [task_cmd] for cmds in task_calls)
    assert not result["quarantined"]


def test_a_completed_level_runs_the_level_checks_exactly_once(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [{"name": "level", "cmd": LEVEL_CMD}]
    spy = spy_on(monkeypatch)
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["status"] == "complete"
    assert [(r["name"], r["passed"]) for r in record["level_checks"]] == [("level", True)]
    assert [c for c in spy.calls if c[1]] == [("pa", True, [LEVEL_CMD])]
    assert [p.name for p in level_logs(tmp_path, "pa")] == ["1.log"]


def meet_at_the_gate(monkeypatch, *, locked: bool = True) -> list[str]:
    """Make both levels reach the gate together: each waits at a barrier before asking for the real lock.

    Only the first two arrivals wait. A later one is a phase's merge into the initiative branch, which checks the
    merged tree under the same lock and has no partner to meet."""
    barrier, arrived, real = threading.Barrier(2, timeout=10), [], epic.repo_gate_lock
    count = threading.Lock()

    @contextmanager
    def gate(repo_path):
        with count:
            arrived.append(threading.current_thread().name)
            paired = len(arrived) <= 2
        if paired:
            barrier.wait()
        with real(repo_path) if locked else nullcontext():
            yield

    monkeypatch.setattr(epic, "repo_gate_lock", gate)
    return arrived


def test_two_levels_completing_at_once_run_their_level_checks_serially(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    cart["landing_areas"]["level_checks"] = [{"name": "level", "cmd": LEVEL_CMD}]
    arrived = meet_at_the_gate(monkeypatch)
    spy = spy_on(monkeypatch)
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []))
    result, _ = drive(repo, cart, tmp_path, work=spec, runner=Gated(gate=True))

    by = by_phase(result)
    assert by["pa"]["status"] == by["pb"]["status"] == "complete", (by["pa"].get("reason"), by["pb"].get("reason"))
    # Both level checks, then the merge check of whichever phase merged second: the initiative tip had moved under it.
    assert len(arrived) == 3
    assert {by["pa"]["initiative_merge"]["status"], by["pb"]["initiative_merge"]["status"]} == {"merged"}
    kinds = [kind for kind, _ in spy.markers]
    assert kinds == ["enter", "exit", "enter", "exit"]
    first, second = sorted(spy.spans.values())
    assert first[1] <= second[0]


def test_the_same_meeting_overlaps_when_the_lock_is_not_held(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    """The control for the serial test: with the lock replaced, the two level runs do overlap."""
    cart["landing_areas"]["level_checks"] = [{"name": "level", "cmd": LEVEL_CMD}]
    meet_at_the_gate(monkeypatch, locked=False)
    spy = spy_on(monkeypatch)
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []))
    drive(repo, cart, tmp_path, work=spec, runner=Gated(gate=True))

    assert [kind for kind, _ in spy.markers] == ["enter", "enter", "exit", "exit"]


def test_a_failing_level_check_marks_that_level_and_names_it(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    """pa's tree holds ta-one.txt so its check fails; pb's does not, so it passes. pc needs pa."""
    cart["landing_areas"]["level_checks"] = [
        {"name": "integration", "cmd": "test ! -f ta-one.txt || { echo boom-tail; exit 3; }"}
    ]
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []), ("tj-join", "pc", ["ta-one"]))
    result, _ = drive(repo, cart, tmp_path, work=spec, runner=Gated())

    by = by_phase(result)
    assert by["pa"]["status"] != "complete"
    assert by["pa"]["reason"] == "level pa failed level check integration"
    assert "boom-tail" in by["pa"]["level_checks"][0]["output_tail"]
    assert "review_pr" not in by["pa"]
    assert by["pb"]["status"] == "complete" and by["pb"]["level_checks"][0]["passed"] is True
    # The dependent is blocked, or started early on the approved task and ended partial: never complete either way.
    assert by["pc"]["status"] in ("blocked", "partial")
    assert result["totals"]["phases_complete"] == 1
    assert not result["quarantined"]


def test_a_cartridge_without_level_checks_gives_the_record_it_gave_before(repo, cart, tmp_path, monkeypatch) -> None:  # noqa: F811
    spy = spy_on(monkeypatch)
    result, _ = drive(repo, cart, tmp_path, work=work(("ta-one", "pa", [])), runner=Gated())

    record = by_phase(result)["pa"]
    assert record["status"] == "complete"
    assert "level_checks" not in record
    assert not any(level for _, level, _ in spy.calls)
    assert not (tmp_path / "runs" / "epic-1" / "checks" / "pa" / "level").exists()


def test_the_failure_reason_names_the_level_and_the_first_failing_check() -> None:
    rows = [{"name": "a", "passed": True}, {"name": "b", "passed": False}, {"name": "c", "passed": False}]
    assert _level_failure_reason("pa", rows) == "level pa failed level check b"
    assert _level_failure_reason("pa", rows[:1]) == ""


def test_the_phase_view_carries_gate_check_commands_only_when_the_shared_runner_has_them() -> None:
    class Plain:
        pass

    class Aware:
        def __init__(self) -> None:
            self.gate_check_commands: dict = {}

    gates = {"level": ["make level"]}
    assert not hasattr(_PhaseRunner(Plain(), Path("/wt/a"), None, [], gates), "gate_check_commands")
    assert _PhaseRunner(Aware(), Path("/wt/a"), None, [], gates).gate_check_commands == gates
