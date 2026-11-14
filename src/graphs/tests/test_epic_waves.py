"""run_epic as a lane scheduler: independent phases overlap and a quarantine blocks only its dependents."""

from __future__ import annotations

import threading

import pytest

from runner.protocol import RunnerError
from tests.test_epic_driver import Runner, cart, drive, git, new_file_patch, repo  # noqa: F401

IDS = ("ta-one", "tb-one", "tj-join", "tk-last", "ta-two", "tb-two")


def work(*spec: tuple[str, str, list[str]]) -> dict:
    items = [
        {"id": i, "phase": p, "state": "ready", "needs": n, "surfaces": [], "title": i, "body": f"do {i}"}
        for i, p, n in spec
    ]
    return {"id": "demo-initiative", "title": "demo", "body": "goal", "phases": sorted({s[1] for s in spec}), "items": items}


class Gated(Runner):
    """Builds `ta-one` and `tb-one` wait for each other when `gate` is set; builds of `fail` tasks raise."""

    def __init__(
        self, *, fail: tuple[str, ...] = (), gate: bool = False, hold: tuple[str, str] | None = None, directory: str = ""
    ) -> None:
        super().__init__({t: new_file_patch(f"{directory}{t}.txt") for t in IDS})
        self.fail, self.gate, self.begun, self.both = fail, gate, set(), threading.Event()
        self.hold, self.release = hold, threading.Event()

    def _subject(self, prompt, candidates):
        return next((t for t in IDS if t in prompt), None)

    def run(self, **kw):
        if kw["role"] == "build":
            task = self._subject(kw["prompt"], IDS)
            if task in self.fail:
                raise RunnerError(f"no build scripted for {task}")
            with self.lock:
                self.begun.add(task)
                if {"ta-one", "tb-one"} <= self.begun:
                    self.both.set()
                if self.hold and self.hold[1] in self.begun:
                    self.release.set()
            if self.hold and task == self.hold[0]:
                assert self.release.wait(10), f"{task} was held while {self.hold[1]} never started"
            if self.gate and task in ("ta-one", "tb-one"):
                assert self.both.wait(10), f"{task} never saw the other chain building"
        return super().run(**kw)


def by_phase(result: dict) -> dict:
    return {r["phase"]: r for r in result["phases"]}


@pytest.mark.usefixtures("at_task")
def test_two_chains_build_at_once_and_the_join_is_assembled_from_both(repo, cart, tmp_path) -> None:  # noqa: F811
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []), ("tj-join", "pj", ["ta-one", "tb-one"]))
    result, runner = drive(repo, cart, tmp_path, runner=Gated(gate=True), work=spec)
    by = by_phase(result)
    assert runner.both.is_set()
    assert result["waves"] == [["pa", "pb"], ["pj"]] and result["widest_wave"] == 2
    assert {by["pa"]["lane"], by["pb"]["lane"]} == {1, 2}
    assert by["pj"]["assembled_from"] == ["pa", "pb"] and by["pj"]["status"] == "complete"


def test_a_quarantined_task_blocks_only_its_dependents(repo, cart, tmp_path) -> None:  # noqa: F811
    spec = work(
        ("ta-one", "pa", []), ("ta-two", "pa2", ["ta-one"]), ("tb-one", "pb", []), ("tb-two", "pb2", ["tb-one"])
    )
    result, runner = drive(repo, cart, tmp_path, runner=Gated(fail=("ta-one",)), work=spec)
    by = by_phase(result)
    assert by["pb"]["status"] == "complete" and by["pb2"]["status"] == "complete"
    assert by["pa2"]["status"] == "blocked" and "'pa'" in by["pa2"]["reason"]
    assert "tb-two" in runner.begun and "ta-two" not in runner.begun


def test_two_phases_declaring_the_same_surface_run_one_after_the_other(repo, cart, tmp_path) -> None:  # noqa: F811
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []))
    for item in spec["items"]:
        item["surfaces"] = ["pkg/"]
    (repo / "pkg").mkdir()
    (repo / "pkg" / "keep.txt").write_text("keep\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "pkg", cwd=repo)
    result, runner = drive(repo, cart, tmp_path, runner=Gated(directory="pkg/"), work=spec)
    by = by_phase(result)
    mentions = [n for n, c in enumerate(runner.calls) if "ta-one" in c["prompt"]]
    later = [n for n, c in enumerate(runner.calls) if "tb-one" in c["prompt"]]
    assert by["pa"]["status"] == by["pb"]["status"] == "complete"
    assert "waits for phase 'pa'" in by["pb"]["serialised"] and "serialised" not in by["pa"]
    assert max(mentions) < min(later)


def test_a_later_level_task_starts_while_an_unrelated_earlier_level_task_builds(repo, cart, tmp_path) -> None:  # noqa: F811
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []), ("tb-two", "pb2", ["tb-one"]))
    runner = Gated(hold=("ta-one", "tb-two"))
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=spec)
    by = by_phase(result)
    assert runner.release.is_set()
    assert [by[p]["status"] for p in ("pa", "pb", "pb2")] == ["complete"] * 3
    assert (by["pb2"]["wave"], by["pa"]["wave"]) == (2, 1)


def test_a_usage_ceiling_with_no_headroom_pauses_every_phase_and_keeps_the_record(repo, cart, tmp_path) -> None:  # noqa: F811
    spec = work(("ta-one", "pa", []), ("tb-one", "pb", []), ("tj-join", "pj", ["ta-one", "tb-one"]))
    result, runner = drive(repo, cart, tmp_path, runner=Gated(), work=spec, headroom_usd=lambda: 0.0, est_task_usd=1.0)
    by = by_phase(result)
    assert not runner.begun and len(result["phases"]) == 3
    assert by["pa"]["status"] == by["pb"]["status"] == "paused" and "usage ceiling" in by["pa"]["reason"]
    assert by["pj"]["status"] == "blocked" and "'pa'" in by["pj"]["reason"]


def test_headroom_that_drops_after_the_first_wave_pauses_the_next_phase(repo, cart, tmp_path) -> None:  # noqa: F811
    spec = work(
        ("ta-one", "pa", []), ("tb-one", "pb", []), ("tj-join", "pj", ["ta-one", "tb-one"]), ("tk-last", "pk", ["tj-join"])
    )
    runner = Gated()
    result, _ = drive(
        repo, cart, tmp_path, runner=runner, work=spec,
        headroom_usd=lambda: 5.0 if not runner.begun else 0.5, est_task_usd=1.0,
    )
    by = by_phase(result)
    assert by["pj"]["status"] == "paused" and "usage ceiling" in by["pj"]["reason"]
    assert by["pk"]["status"] == "blocked" and "'pj'" in by["pk"]["reason"]
    assert len(result["phases"]) == 4
    assert result["lane_bound"]["binding"] == "usage ceiling"
