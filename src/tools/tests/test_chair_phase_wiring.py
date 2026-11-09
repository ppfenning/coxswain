from __future__ import annotations

from agent_tools import chair_facts, chair_plan_fill, chair_read_docket, cli, run_store


def _lane(run: str, host: str | None, phases: int = 1) -> run_store.Lane:
    return run_store.Lane(run, host, "t", "t", phases)


def test_a_lane_counts_once_per_running_phase() -> None:
    lanes = [_lane("r1", "host-a", 3), _lane("r2", "host-a", 1)]
    assert cli._live_by_host(lanes, "host-b") == {"host-a": 4}


def test_lanes_with_one_phase_count_as_before() -> None:
    lanes = [_lane("r1", None), _lane("r2", "host-b"), _lane("r3", "host-a")]
    assert cli._live_by_host(lanes, "host-b") == {"": 2, "host-a": 1}


def test_dispatch_counts_and_host_names_follow_the_phase_count() -> None:
    lanes = [_lane("r1", "host-b", 3), _lane("r2", "host-a", 2)]
    assert cli._dispatch_counts(lanes, "host-b", ["host-a"], 1) == {"": 4, "host-a": 2}
    assert cli._live_by_host_name(lanes, "host-b") == {"host-b": 3, "host-a": 2}


def test_a_phase_run_and_a_two_phase_run_fill_three_lanes_and_cut_the_planner_free_count_by_three() -> None:
    lanes = [_lane("p1", "host-a"), _lane("w1", "host-a", 2)]
    counts = cli._dispatch_counts(lanes, "host-b", ["host-a"], 0)
    assert counts["host-a"] == 3

    def free(live: dict[str, int]) -> int:
        dispatch = chair_facts.dispatch_facts({"max_in_flight": 6}, ["host-a"], live, {"host-a": 6})
        return chair_plan_fill.host_free_slots({"dispatch": dispatch})[0][3]  # type: ignore[typeddict-item]

    assert free({"host-a": 0}) - free(counts) == 3


def test_a_pidfile_run_with_no_lease_counts_one(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.pid").write_text("123", encoding="utf-8")
    monkeypatch.setattr("agent_tools.epic.run_live", lambda *args, **kwargs: True)
    assert chair_read_docket._busy_lanes(runs, "2026-11-09T00:00:00Z") == 1


def test_the_docket_busy_count_equals_the_planner_s_with_two_phase_runs_of_one_initiative(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    lanes = [_lane("alpha-p1", None), _lane("alpha-p2", None)]
    monkeypatch.setattr(run_store, "live_lanes", lambda *a, **k: lanes)
    busy = chair_read_docket._busy_lanes(runs, "2026-11-09T00:00:00Z")
    pidfile_live, pidfiled = chair_read_docket.local_runs(runs, "2026-11-09T00:00:00Z")
    counts = cli._dispatch_counts(run_store.remote_lanes(lanes, pidfiled), "host-b", [], pidfile_live)
    assert (busy, counts[""]) == (2, 2)


def test_the_docket_busy_count_counts_a_whole_run_per_phase(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(run_store, "live_lanes", lambda *a, **k: [_lane("alpha", None, 3)])
    assert chair_read_docket._busy_lanes(runs, "2026-11-09T00:00:00Z") == 3


def test_the_live_phase_pairs_reach_the_facts_deps_over_the_docket_initiatives(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.chair_facts, "docket_by_land_at", lambda *a, **k: {"initiatives": [{"id": "alpha"}, {"id": "beta"}]})
    seen: dict = {}

    def fake_read(runs_dir, initiatives, now):
        seen["initiatives"] = list(initiatives)
        return [("alpha", "p1"), ("alpha", "p2")]

    monkeypatch.setattr(cli.chair_read_live, "read_live_phases", fake_read)
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.live_phases() == [("alpha", "p1"), ("alpha", "p2")]  # type: ignore[misc]
    assert seen == {"initiatives": ["alpha", "beta"]}
